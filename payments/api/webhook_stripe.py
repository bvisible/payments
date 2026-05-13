# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""Stripe webhook endpoint.

Implements the recommended pattern (compass §6):

1. Capture the **raw** request body (Werkzeug caches it once).
2. Verify the Stripe signature against the configured webhook secret.
3. Deduplicate via the unique index on ``Webhook Event Log.event_id``.
4. Insert the log row with status ``Queued``, ``frappe.db.commit()``.
5. Enqueue a background job with a deterministic ``job_id`` and
   ``deduplicate=True`` so concurrent deliveries don't double-process.
6. Reply ``200 OK`` in under one second — any handler error is recorded in the
   log row but does not surface to Stripe (otherwise we trigger a retry storm).

The actual event handling lives in :func:`process_event`, which is the RQ job
target. It loads the driver corresponding to the provider, calls its
``handle_webhook`` method, and applies the resulting transition to the Payment
Intent FSM.
"""

from __future__ import annotations

import json

import frappe
from frappe import _

# Conventional name of the Payment Provider record for Stripe.
STRIPE_PROVIDER_NAME = "stripe"


@frappe.whitelist(allow_guest=True, methods=["POST"])
def handle() -> str:
	"""Stripe webhook receiver. Returns plain text for Stripe's UI.

	NOTE: ``allow_guest=True`` is required because Stripe does not authenticate
	with a Frappe session; the trust boundary is the signature verification.
	"""
	# 1. Raw body — must not be parsed/re-serialized or signature breaks.
	payload: bytes = frappe.request.get_data() or b""
	signature: str | None = frappe.get_request_header("Stripe-Signature")

	# Lazy import to keep module load fast and to allow the driver layer to be
	# patched in tests.
	from payments.drivers.registry import DriverResolutionError, resolve_driver

	# 2. Resolve the Stripe webhook driver. For Phase 1 we resolve any
	#    (stripe, *) binding because the webhook is provider-scoped not
	#    channel-scoped; we pick the first enabled binding to obtain the secret.
	try:
		driver = _resolve_stripe_webhook_driver()
	except DriverResolutionError as exc:
		# No Stripe provider configured — drop the call but log so ops can react.
		frappe.log_error("Stripe webhook unresolved provider", str(exc))
		frappe.local.response["http_status_code"] = 503
		return "stripe provider not configured"

	# 3. Verify signature + parse via the driver.
	result = driver.handle_webhook(payload, dict(frappe.request.headers or {}))

	if not result.signature_valid:
		frappe.local.response["http_status_code"] = 400
		return "invalid signature"

	# 4. Idempotency — dedup via unique index on Webhook Event Log.event_id.
	if frappe.db.exists("Webhook Event Log", result.event_id):
		# Already received; reply OK without processing again.
		return "ok"

	# 5. Persist + queue.
	log = frappe.get_doc(
		{
			"doctype": "Webhook Event Log",
			"event_id": result.event_id,
			"provider": STRIPE_PROVIDER_NAME,
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


def _resolve_stripe_webhook_driver():
	"""Pick the first enabled Stripe binding and resolve its driver.

	The driver is responsible for signature verification against the secret it
	finds in its ``Provider Channel Settings`` config_json. If multiple Stripe
	bindings exist (web + terminal), they share the same webhook signing secret
	per Stripe account, so any of them can verify.
	"""
	from payments.drivers.registry import resolve_driver

	binding = frappe.db.get_value(
		"Provider Channel Settings",
		{"provider": STRIPE_PROVIDER_NAME, "enabled": 1},
		["channel"],
		as_dict=True,
		order_by="modified desc",
	)
	if not binding:
		from payments.drivers.registry import DriverResolutionError

		raise DriverResolutionError(_("No enabled Stripe binding found"))
	return resolve_driver(STRIPE_PROVIDER_NAME, binding["channel"])


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
	try:
		raw = json.loads(log.raw_payload or "{}")
	except (ValueError, TypeError) as exc:
		log.status = "Failed"
		log.error = f"raw_payload not valid JSON: {exc}"
		log.save(ignore_permissions=True)
		return

	# Re-resolve the driver (cannot pickle it across processes).
	try:
		driver = _resolve_stripe_webhook_driver()
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
		result = driver.handle_webhook(log.raw_payload.encode("utf-8"), headers={})
	except Exception as exc:  # noqa: BLE001
		log.status = "Failed"
		log.error = f"handle_webhook raised: {exc!r}"
		log.save(ignore_permissions=True)
		return

	# Apply FSM transition if the event maps to a known Intent.
	if result.intent_name and result.target_status:
		try:
			intent_doc = frappe.get_doc("Payment Intent", result.intent_name)
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
			frappe.publish_realtime(
				event=f"payment.intent.{result.intent_name}.updated",
				message={
					"intent_name": result.intent_name,
					"status": intent_doc.status,
					"event_type": result.event_type,
				},
				after_commit=True,
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
