# //// Neoffice — added file (no upstream equivalent). The Stripe webhook receiver of the
# //// unified hub: raw body, signature check, dedup on the unique `Webhook Event Log`
# //// index, enqueue with a deterministic job_id, answer 200 in under a second, then
# //// drive the Payment Intent FSM from the driver's `handle_webhook`. Upstream's Stripe
# //// integration ends at its hosted checkout page — at the fork point the word
# //// "webhook" appears nowhere in the app but GoCardless.
# //// Commits: e32ecf5 2026-05-13 "feat(payments): Phase 1 — unified payment driver layer (Provider × Channel × Driver)"
# ////          e860ab9 2026-06-22 "fix(stripe-terminal): capture-after-action — read PI id from process_payment_intent"
# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""Stripe webhook endpoint.

Implements the recommended pattern (compass §6):

1. Capture the **raw** request body (Werkzeug caches it once).
2. Verify the Stripe signature against the webhook secret of the provider that
   signed it (:mod:`payments.api.webhook_common`). No valid signature, no row.
3. Deduplicate via the unique index on ``Webhook Event Log.event_id``.
4. Insert the log row with status ``Queued``, ``frappe.db.commit()``.
5. Enqueue a background job with a deterministic ``job_id`` and
   ``deduplicate=True`` so concurrent deliveries don't double-process.
6. Reply ``200 OK`` in under one second — any handler error is recorded in the
   log row but does not surface to Stripe (otherwise we trigger a retry storm).

The actual event handling lives in :func:`process_event`, which is the RQ job
target. It loads the driver of the provider that proved the delivery, reads the
row with its ``parse_verified_event`` method, and applies the resulting transition
to the Payment Intent FSM, for an intent that provider holds.
"""

from __future__ import annotations

import json

import frappe

# Stripe provider records are detected by their driver_class (any class under
# ``payments.drivers.stripe.*``). This avoids hard-coding a single provider name
# — operators can have ``stripe_test`` + ``stripe_live`` cohabiting, each with its
# own secret: see :func:`payments.api.webhook_common.signed_delivery`.
# //// Neoffice — the pattern of the two helpers that stood here, gone with them to webhook_common.
_STRIPE_DRIVERS = "payments.drivers.stripe.%"


@frappe.whitelist(allow_guest=True, methods=["POST"])
def handle() -> str:  # permission-check: ok the caller is the provider, its signature is the proof
	"""Stripe webhook receiver. Returns plain text for Stripe's UI.

	NOTE: ``allow_guest=True`` is required because Stripe does not authenticate
	with a Frappe session; the trust boundary is the signature verification.
	"""
	# 1. Raw body — must not be parsed/re-serialized or signature breaks.
	payload: bytes = frappe.request.get_data() or b""

	# Lazy import to keep module load fast and to allow the driver layer to be
	# patched in tests.
	from payments.api.webhook_common import provider_of, refuse_delivery, signed_delivery

	# 2. + 3. Verify the signature + parse via the driver.
	# //// Neoffice — against every enabled Stripe binding, and with one refusal for all reasons. Only the
	# //// provider saved last was asked, whose secret is not the one of an intent of the other provider; a
	# //// site with no Stripe answered 503 and wrote one Error Log row per anonymous POST, which told a guest
	# //// what it has.
	driver, result = signed_delivery(_STRIPE_DRIVERS, payload, dict(frappe.request.headers or {}))
	if not driver:
		return refuse_delivery("Stripe", result.error_code if result else None)

	# 4. Idempotency — dedup via unique index on Webhook Event Log.event_id.
	if frappe.db.exists("Webhook Event Log", result.event_id):
		# Already received; reply OK without processing again.
		return "ok"

	# 5. Persist + queue.
	log = frappe.get_doc(
		{
			"doctype": "Webhook Event Log",
			"event_id": result.event_id,
			# //// Neoffice — the provider that proved it: the worker reads the row with this provider's
			# //// driver.
			"provider": provider_of(driver),
			"event_type": result.event_type,
			"signature_valid": 1,
			"status": "Queued",
			"raw_payload": _safe_truncate(payload),
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()  # noqa: T201 — explicit commit so the dedup record survives even if the rest fails.

	frappe.enqueue(
		"payments.api.webhook_stripe.process_event",
		queue="short",
		job_id=f"stripe-webhook-{result.event_id}",
		deduplicate=True,
		enqueue_after_commit=True,
		log_name=log.name,
	)
	return "ok"


def _capture_after_action_succeeded(log, driver) -> None:  # noqa: ANN001
	"""Trigger Stripe PaymentIntent.capture after a successful reader action.

	The reader payload doesn't carry the PI metadata directly (Stripe sends
	``reader.action.payment_intent`` as a string id when not expanded, or
	None in some payloads). We extract the PI id from the raw event payload
	and call ``driver.capture_payment``. Idempotent — Stripe will accept the
	same capture call twice within 24h thanks to the idempotency key inside
	``capture_payment``.
	"""
	import json as _json

	try:
		raw = _json.loads(log.raw_payload or "{}")
	except (ValueError, TypeError):
		return

	obj = raw.get("data", {}).get("object", {}) or {}
	action = obj.get("action") or {}
	# Stripe nests the PI id under `action.process_payment_intent.payment_intent`
	# (not directly under `action.payment_intent` as one might expect from the
	# field naming). Also tolerate `action.payment_intent` for forward-compat
	# in case Stripe ever flattens the structure or for other action types.
	pi_id: str | None = None
	process_pi = action.get("process_payment_intent")
	if isinstance(process_pi, dict):
		pi_id = process_pi.get("payment_intent") or process_pi.get("id")
	elif isinstance(process_pi, str) and process_pi:
		pi_id = process_pi
	if not pi_id:
		direct = action.get("payment_intent")
		if isinstance(direct, dict):
			pi_id = direct.get("id")
		elif isinstance(direct, str) and direct:
			pi_id = direct
	if not pi_id:
		return

	try:
		driver.capture_payment(pi_id)
	except Exception as exc:  # noqa: BLE001
		frappe.log_error(
			"Stripe capture after action_succeeded failed",
			f"pi={pi_id} log={log.name}: {exc!r}",
		)


def _safe_truncate(payload: bytes, max_bytes: int = 200_000) -> str:
	"""Decode + truncate the payload to keep the log row reasonable in size."""
	try:
		text = payload.decode("utf-8")
	except UnicodeDecodeError:
		text = payload.decode("utf-8", errors="replace")
	if len(text) > max_bytes:
		text = text[:max_bytes] + "...[truncated]"
	return text


def process_event(log_name: str) -> None:
	"""RQ job — load the log row, dispatch to the driver, drive FSM.

	Any exception here updates the log row with ``Failed`` and an error message,
	but is NOT re-raised: returning ``200 OK`` to Stripe is the contract.
	"""
	log = frappe.get_doc("Webhook Event Log", log_name)
	# //// Neoffice — this worker trusts its row, so a row that does not say it was proven is not read at all.
	if not log.signature_valid:
		log.status = "Failed"
		log.error = "row stored without a valid signature"
		log.save(ignore_permissions=True)
		return
	try:
		raw = json.loads(log.raw_payload or "{}")
	except (ValueError, TypeError) as exc:
		log.status = "Failed"
		log.error = f"raw_payload not valid JSON: {exc}"
		log.save(ignore_permissions=True)
		return

	# Re-resolve the driver (cannot pickle it across processes).
	from payments.api.webhook_common import publish_intent_update, skip_foreign_intent, webhook_drivers

	try:
		driver = webhook_drivers(_STRIPE_DRIVERS, log.provider)[0]
	except Exception as exc:  # noqa: BLE001
		log.status = "Failed"
		log.error = f"resolve_driver failed: {exc!r}"
		log.save(ignore_permissions=True)
		return

	# Re-parse via the driver (it knows how to map provider events → target FSM
	# status). We pass the raw payload back as bytes because the driver's
	# signature has already been verified in `handle()`.
	# NOTE: passing as bytes again preserves the contract.
	from frappe.utils import now_datetime

	try:
		# //// Neoffice — parse_verified_event, not handle_webhook with no headers: see the driver.
		result = driver.parse_verified_event(log.raw_payload.encode("utf-8"))
	except Exception as exc:  # noqa: BLE001
		log.status = "Failed"
		log.error = f"handle_webhook raised: {exc!r}"
		log.save(ignore_permissions=True)
		return

	# Bridge for Stripe Terminal: `terminal.reader.action_succeeded` means the
	# card was authorized on the reader. Since we create PaymentIntents with
	# capture_method="manual" (so cashier can adjust tip after auth), we must
	# explicitly call PaymentIntent.capture here — otherwise the PI stays in
	# `requires_capture` forever and `payment_intent.succeeded` never fires.
	#
	# We resolve the Frappe intent via provider_intent_id (the Stripe PI id is
	# in the event payload even when metadata lookup fails for reader events).
	if result.event_type == "terminal.reader.action_succeeded":
		_capture_after_action_succeeded(log, driver)

	# Apply FSM transition if the event maps to a known Intent.
	if result.intent_name and result.target_status:
		try:
			intent_doc = frappe.get_doc("Payment Intent", result.intent_name)
			# //// Neoffice — nothing compared the provider: the metadata named any intent, of any PSP.
			if intent_doc.provider != log.provider:
				skip_foreign_intent(log, intent_doc)
				return
			intent_doc.transition_to(
				result.target_status,
				event_source="webhook",
				webhook_event_log=log.name,
				error_code=result.error_code,
				error_message=result.error_message,
				payload_excerpt=result.payload_excerpt,
				ignore_invalid=True,  # avoid blowing up on duplicate replays
			)
			log.intent = result.intent_name
			# Push the new state to the frontend (POSNext) via SocketIO.
			# //// Neoffice — to the account that started it, see publish_intent_update.
			publish_intent_update(
				intent_doc,
				{
					"intent_name": result.intent_name,
					"status": intent_doc.status,
					"event_type": result.event_type,
				},
			)
		except frappe.DoesNotExistError:
			log.status = "Skipped"
			log.error = f"Payment Intent {result.intent_name} not found"
			log.processed_at = now_datetime()
			log.save(ignore_permissions=True)
			return

	log.status = "Processed"
	log.processed_at = now_datetime()
	log.save(ignore_permissions=True)
