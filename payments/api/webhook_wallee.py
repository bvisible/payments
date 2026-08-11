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

The RQ worker (``process_event``) re-resolves the driver, re-parses the body,
and drives the Payment Intent FSM. SocketIO is published after a successful
transition so POSNext re-renders the dialog.

Convention: the Wallee Payment Provider record is conventionally named
``wallee_test`` or ``wallee_live``. The webhook handler looks up the first
enabled binding whose driver class belongs to ``payments.drivers.wallee``.
"""

from __future__ import annotations

import json

import frappe
from frappe import _


@frappe.whitelist(allow_guest=True, methods=["POST"])
def handle() -> str:
	"""Wallee webhook receiver. Returns plain text.

	NOTE: ``allow_guest=True`` is required — Wallee does not authenticate with a
	Frappe session; the trust boundary is the signature verification done in the
	driver's ``handle_webhook``.
	"""
	payload: bytes = frappe.request.get_data() or b""

	from payments.drivers.registry import DriverResolutionError

	try:
		driver = _resolve_wallee_webhook_driver()
	except DriverResolutionError as exc:
		frappe.log_error("Wallee webhook unresolved provider", str(exc))
		frappe.local.response["http_status_code"] = 503
		return "wallee provider not configured"

	result = driver.handle_webhook(payload, dict(frappe.request.headers or {}))

	if not result.signature_valid:
		frappe.local.response["http_status_code"] = 400
		return f"invalid signature: {result.error_code or 'unknown'}"

	# Idempotency — dedup via unique index on Webhook Event Log.event_id.
	if frappe.db.exists("Webhook Event Log", result.event_id):
		return "ok"

	# Persist + queue.
	provider_name = _wallee_provider_name()
	log = frappe.get_doc(
		{
			"doctype": "Webhook Event Log",
			"event_id": result.event_id,
			"provider": provider_name,
			"event_type": result.event_type,
			"signature_valid": 1,
			"status": "Queued",
			"raw_payload": _safe_truncate(payload),
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()  # noqa: T201 — explicit commit so the dedup row survives.

	frappe.enqueue(
		"payments.api.webhook_wallee.process_event",
		queue="short",
		job_id=f"wallee-webhook-{result.event_id}",
		deduplicate=True,
		enqueue_after_commit=True,
		log_name=log.name,
	)
	return "ok"


def process_event(log_name: str) -> None:
	"""RQ job — load the log row, dispatch to the driver, drive FSM.

	Same error-handling contract as the Stripe worker: any handler exception is
	recorded on the log row as ``Failed`` and NOT re-raised (we already replied
	200 to Wallee).
	"""
	log = frappe.get_doc("Webhook Event Log", log_name)
	from frappe.utils import now_datetime

	try:
		_body = json.loads(log.raw_payload or "{}")  # noqa: F841 — parsed for sanity
	except (ValueError, TypeError) as exc:
		log.status = "Failed"
		log.error = f"raw_payload not valid JSON: {exc}"
		log.save(ignore_permissions=True)
		return

	try:
		driver = _resolve_wallee_webhook_driver()
	except Exception as exc:  # noqa: BLE001
		log.status = "Failed"
		log.error = f"resolve_driver failed: {exc!r}"
		log.save(ignore_permissions=True)
		return

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


# ----------------------------------------------------------------------------
# Internals
# ----------------------------------------------------------------------------


def _resolve_wallee_webhook_driver():
	"""Pick the first enabled Wallee terminal binding and resolve its driver.

	Wallee webhook secrets are configured per-instance (in ``Wallee Settings``),
	not per-binding, so any enabled binding can verify the signature.
	"""
	from payments.drivers.registry import DriverResolutionError, resolve_driver

	provider_name = _wallee_provider_name()
	if not provider_name:
		raise DriverResolutionError(_("No enabled Wallee provider found"))

	binding = frappe.db.get_value(
		"Provider Channel Settings",
		{"provider": provider_name, "enabled": 1},
		["channel"],
		as_dict=True,
		order_by="modified desc",
	)
	if not binding:
		raise DriverResolutionError(
			_("No enabled Provider Channel Settings for {0}").format(provider_name)
		)
	return resolve_driver(provider_name, binding["channel"])


def _wallee_provider_name() -> str | None:
	"""First enabled Payment Provider record using a Wallee driver class."""
	row = frappe.db.get_value(
		"Payment Provider",
		{"driver_class": ["like", "payments.drivers.wallee.%"], "enabled": 1},
		"name",
		order_by="modified desc",
	)
	return row


def _safe_truncate(payload: bytes, max_bytes: int = 200_000) -> str:
	try:
		text = payload.decode("utf-8")
	except UnicodeDecodeError:
		text = payload.decode("utf-8", errors="replace")
	if len(text) > max_bytes:
		text = text[:max_bytes] + "...[truncated]"
	return text
