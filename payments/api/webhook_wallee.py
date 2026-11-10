# //// Neoffice — added file (no upstream equivalent). The Wallee webhook receiver, same
# //// shape as the Stripe one: the driver verifies the HMAC-SHA256 signature against
# //// `Wallee Settings.webhook_secret` and builds a deterministic event id from
# //// listenerEntityTechnicalName + entityId + state, because Wallee emits no global
# //// event id. Wallee arrived with the retired `wallee_integration` app (99e929c,
# //// ADR-005); upstream has no Wallee anything.
# //// Commits: 49af519 2026-05-18 "feat(drivers): Wallee Terminal driver (server-driven, webhook + polling fallback)"
# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""Wallee webhook endpoint.

Same shape as :mod:`payments.api.webhook_stripe`:

1. Capture the raw request body.
2. Let the driver verify the HMAC-SHA256 signature against
   ``Wallee Settings.webhook_secret``.
3. Deduplicate via ``Webhook Event Log.event_id`` (the driver builds a
   deterministic id from ``listenerEntityTechnicalName`` + ``entityId`` +
   ``state``, because Wallee does not emit a global event id).
4. Persist + enqueue with ``deduplicate=True``.
5. Reply ``200 OK`` in < 1s.

The RQ worker (``process_event``) re-resolves the driver of the provider that
proved the delivery, re-parses the body, and drives the Payment Intent FSM.
SocketIO is published after a successful transition so POSNext re-renders the
dialog.

Convention: the Wallee Payment Provider record is conventionally named
``wallee_test`` or ``wallee_live``. The webhook handler asks every enabled binding
whose driver class belongs to ``payments.drivers.wallee``. A provider with no
``webhook_secret`` refuses: the poller settles its payments.
"""

from __future__ import annotations

import json

import frappe

# //// Neoffice — the pattern of the two helpers that stood below, gone with them to webhook_common.
_WALLEE_DRIVERS = "payments.drivers.wallee.%"
# //// Neoffice — the job and its id, for the first delivery and for a retry (webhook_common.received_before).
_PROCESS_EVENT = "payments.api.webhook_wallee.process_event"
_JOB_PREFIX = "wallee-webhook"


@frappe.whitelist(allow_guest=True, methods=["POST"])
def handle() -> str:  # permission-check: ok the caller is the provider, its signature is the proof
	"""Wallee webhook receiver. Returns plain text.

	NOTE: ``allow_guest=True`` is required — Wallee does not authenticate with a
	Frappe session; the trust boundary is the signature verification done in the
	driver's ``handle_webhook``.
	"""
	payload: bytes = frappe.request.get_data() or b""

	from payments.api.webhook_common import (
		provider_of,
		queue_event,
		received_before,
		record_delivery,
		refuse_delivery,
		signed_delivery,
	)

	# //// Neoffice — against every enabled Wallee provider (each has its Wallee Settings, so its secret), and
	# //// with one refusal for all reasons: a 503 and the error code told a guest what the site has, and each
	# //// call left an Error Log row. Only the provider saved last was asked.
	driver, result = signed_delivery(_WALLEE_DRIVERS, payload, dict(frappe.request.headers or {}))
	if not driver:
		return refuse_delivery("Wallee", result.error_code if result else None)

	# Idempotency — dedup via unique index on Webhook Event Log.event_id.
	# //// Neoffice — maintenance#2296: a row its job never finished is queued again (see received_before).
	if received_before(result.event_id, _PROCESS_EVENT, _JOB_PREFIX):
		return "ok"

	# Persist + queue.
	# //// Neoffice — the provider that proved it: the worker reads the row with this provider's driver.
	provider_name = provider_of(driver)
	# //// Neoffice — a twin delivery of the same event, sent at the same moment, is answered « ok »
	# //// instead of 409 (see record_delivery).
	log = record_delivery(
		{
			"event_id": result.event_id,
			"provider": provider_name,
			"event_type": result.event_type,
			"signature_valid": 1,
			"status": "Queued",
			"raw_payload": _safe_truncate(payload),
		}
	)
	if log is None:
		return "ok"
	frappe.db.commit()  # noqa: T201 — explicit commit so the dedup row survives.

	# //// Neoffice — the same job, through the helper a retry uses too.
	queue_event(_PROCESS_EVENT, _JOB_PREFIX, log.name)
	return "ok"


def process_event(log_name: str) -> None:
	"""RQ job — load the log row, dispatch to the driver, drive FSM.

	Same error-handling contract as the Stripe worker: any handler exception is
	recorded on the log row as ``Failed`` and NOT re-raised (we already replied
	200 to Wallee).
	"""
	log = frappe.get_doc("Webhook Event Log", log_name)
	from frappe.utils import now_datetime

	from payments.api.webhook_common import (
		fail_the_row,
		publish_intent_update,
		skip_foreign_intent,
		webhook_drivers,
	)

	# //// Neoffice — this worker trusts its row, so a row that does not say it was proven is not read at all.
	if not log.signature_valid:
		log.status = "Failed"
		log.error = "row stored without a valid signature"
		log.save(ignore_permissions=True)
		return

	try:
		_body = json.loads(log.raw_payload or "{}")  # noqa: F841 — parsed for sanity
	except (ValueError, TypeError) as exc:
		log.status = "Failed"
		log.error = f"raw_payload not valid JSON: {exc}"
		log.save(ignore_permissions=True)
		return

	try:
		driver = webhook_drivers(_WALLEE_DRIVERS, log.provider)[0]
	except Exception as exc:  # noqa: BLE001
		log.status = "Failed"
		log.error = f"resolve_driver failed: {exc!r}"
		log.save(ignore_permissions=True)
		return

	try:
		# //// Neoffice — parse_verified_event, not handle_webhook with no headers: see the driver.
		result = driver.parse_verified_event(log.raw_payload.encode("utf-8"))
	except Exception as exc:  # noqa: BLE001
		log.status = "Failed"
		log.error = f"handle_webhook raised: {exc!r}"
		log.save(ignore_permissions=True)
		return

	# Apply FSM transition if the event maps to a known Intent.
	if result.intent_name and result.target_status:
		try:
			# //// Neoffice — maintenance#2296: under lock, see webhook_stripe.process_event.
			intent_doc = frappe.get_doc("Payment Intent", result.intent_name, for_update=True)
			# //// Neoffice — an intent is moved by the provider that holds it.
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
				ignore_invalid=True,
			)
			log.intent = result.intent_name

			# A wallee_web success has to produce the Sales Order here as well.
			# /wallee/success does it when the buyer comes back, and the scheduler
			# self-heals stragglers, but that leaves up to five minutes where the
			# money is in and the order does not exist — long enough for a buyer to
			# call and be told nothing was ordered. The webhook is the one signal
			# that always arrives, so it finalises immediately.
			# Idempotent: handle_payment_success short-circuits on a Paid request,
			# so this and the two other paths cannot double up.
			if intent_doc.status == "succeeded" and intent_doc.channel == "wallee_web":
				from payments.drivers.wallee.terminal_driver import (
					_finalize_wallee_web_sales_order,
				)

				_finalize_wallee_web_sales_order(intent_doc)

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
		except Exception as exc:  # noqa: BLE001 — the row says why, the provider got its 200
			# //// Neoffice — maintenance#2296: see webhook_common.fail_the_row.
			fail_the_row(log, exc)
			return

	log.status = "Processed"
	log.processed_at = now_datetime()
	log.save(ignore_permissions=True)


# ----------------------------------------------------------------------------
# Internals
# ----------------------------------------------------------------------------


def _safe_truncate(payload: bytes, max_bytes: int = 200_000) -> str:
	try:
		text = payload.decode("utf-8")
	except UnicodeDecodeError:
		text = payload.decode("utf-8", errors="replace")
	if len(text) > max_bytes:
		text = text[:max_bytes] + "...[truncated]"
	return text
