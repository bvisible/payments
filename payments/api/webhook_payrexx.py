# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""Payrexx webhook endpoint — one URL for all three channels.

Payrexx sends a single webhook stream and tags each delivery with
``transaction.type``: ``E-Commerce``, ``POS-Terminal`` or ``Tap to Pay``. One
endpoint therefore drives web, terminal and Tap to Pay state, which is also why we
do not poll: the rate limit is roughly 600 requests per 5 minutes per account, and
a fleet of tills polling would burn through it.

Same shape as :mod:`payments.api.webhook_stripe`:

1. Capture the **raw** body — re-serialising it breaks the signature.
2. Verify ``X-Webhook-Signature``.
3. Deduplicate on ``Webhook Event Log.event_id``.
4. Insert the log row, commit, enqueue with a deterministic ``job_id``.
5. Reply ``200 OK`` in under a second — Payrexx retries up to 10 times over 24
   hours and expects an answer within 20 seconds, so anything slow here turns one
   delivery into ten.

Two Payrexx specifics:

**No event id.** The payload carries none, while our ``Webhook Event Log``
deduplicates on a unique index. One is derived as
``payrexx_{transaction.uuid}_{status}`` — stable across the retries, distinct per
genuine state change. The residual risk is a legitimate re-emission of an
identical status being swallowed as a duplicate; that is the right trade, since
swallowing a redundant event is harmless and processing a retry twice is not.

**Statuses with no FSM equivalent.** ``chargeback``, ``disputed``, ``insecure``
and ``uncaptured`` do not map onto any of our six states — a chargeback is not a
refund, and pretending otherwise misstates the books. They are recorded and
surfaced to an operator instead of driving a transition.
"""

from __future__ import annotations

import frappe
from frappe import _

from payments.drivers.base import WebhookResult
from payments.drivers.payrexx._common import NEEDS_HUMAN, map_status


@frappe.whitelist(allow_guest=True, methods=["POST"])
def handle() -> str:
	"""Payrexx webhook receiver.

	NOTE: ``allow_guest=True`` is required because Payrexx does not authenticate
	with a Frappe session; the trust boundary is the signature verification.
	"""
	payload: bytes = frappe.request.get_data() or b""
	headers = dict(frappe.request.headers or {})

	provider_name = _payrexx_provider_name()
	if not provider_name:
		frappe.log_error(
			"Payrexx webhook unresolved provider",
			"No enabled Payment Provider with a payments.drivers.payrexx.* driver_class",
		)
		frappe.local.response["http_status_code"] = 503
		return "payrexx provider not configured"

	signing_key = _signing_key(provider_name)
	if not signing_key:
		# Refusing is deliberate: an unverified webhook is attacker-controlled
		# input, and accepting it would let anyone mark an invoice paid.
		frappe.log_error(
			"Payrexx webhook rejected: no signing key",
			f"Payment Provider {provider_name} has no 'webhook_signing_key' in "
			f"credentials_json. Copy it from the Payrexx back office (Webhooks).",
		)
		frappe.local.response["http_status_code"] = 503
		return "webhook signing key not configured"

	result = parse_delivery(payload, headers, signing_key=signing_key)

	if not result.signature_valid:
		frappe.local.response["http_status_code"] = 400
		return "invalid signature"

	if frappe.db.exists("Webhook Event Log", result.event_id):
		return "ok"

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
	frappe.db.commit()  # explicit: the dedup record must survive a later failure

	frappe.enqueue(
		"payments.api.webhook_payrexx.process_event",
		queue="short",
		job_id=f"payrexx-webhook-{result.event_id}",
		deduplicate=True,
		enqueue_after_commit=True,
		log_name=log.name,
	)
	return "ok"


def parse_delivery(
	payload: bytes, headers: dict[str, str], *, signing_key: str | None
) -> WebhookResult:
	"""Verify and parse a delivery into a :class:`WebhookResult`.

	Shared by both Payrexx drivers, since one webhook stream serves every channel.
	Never raises: a malformed or unsigned delivery comes back with
	``signature_valid=False`` so the caller answers ``400`` rather than ``500``.
	"""
	try:
		from payrexx import parse_webhook
	except ImportError:  # pragma: no cover - environment issue
		return WebhookResult(
			event_id="payrexx_unavailable",
			event_type="unknown",
			signature_valid=False,
			error_code="library_missing",
			error_message="the 'payrexx' Python library is not installed on this bench",
		)

	try:
		event = parse_webhook(
			payload, headers=headers, signing_key=signing_key, require_signature=True
		)
	except Exception as exc:  # noqa: BLE001 - signature mismatch or unparseable body
		return WebhookResult(
			event_id="payrexx_invalid",
			event_type="unknown",
			signature_valid=False,
			error_code=type(exc).__name__,
			error_message=str(exc),
		)

	tx = event.transaction
	if tx is None:
		# A subscription or payout delivery: valid, signed, but not something the
		# Payment Intent FSM models. Recorded, not acted upon.
		return WebhookResult(
			event_id=f"payrexx_nontransaction_{abs(hash(payload)) % (10**16)}",
			event_type="non_transaction",
			signature_valid=True,
			payload_excerpt=_safe_truncate(payload, 2_000),
		)

	raw_status = str(tx.status) if tx.status else ""
	target = map_status(raw_status)

	return WebhookResult(
		event_id=event.event_id or f"payrexx_{tx.uuid}_{raw_status}",
		event_type=f"transaction.{raw_status}",
		signature_valid=True,
		# referenceId carries the Payment Intent name — set by both drivers.
		intent_name=tx.reference_id,
		target_status=target,
		error_code=None if target else (raw_status or None),
		payload_excerpt=_safe_truncate(payload, 2_000),
	)


def process_event(log_name: str) -> None:
	"""RQ job — apply a verified delivery to the Payment Intent FSM."""
	log = frappe.get_doc("Webhook Event Log", log_name)

	try:
		result = parse_delivery(
			(log.raw_payload or "").encode("utf-8"),
			headers={},
			signing_key=None,  # already verified at ingress; re-checking is pointless
		)

		if not result.intent_name or not frappe.db.exists("Payment Intent", result.intent_name):
			log.status = "Ignored"
			log.error_message = _("No matching Payment Intent for reference {0}").format(
				result.intent_name or "-"
			)
			log.save(ignore_permissions=True)
			frappe.db.commit()
			return

		intent = frappe.get_doc("Payment Intent", result.intent_name)

		if result.target_status:
			intent.transition_to(
				result.target_status,
				event_source="webhook",
				payload_excerpt=result.payload_excerpt,
				ignore_invalid=True,
			)
			log.status = "Processed"
		else:
			# A status our FSM has no state for. Do not guess.
			_record_needs_human(intent, result)
			log.status = "Processed"
			log.error_message = _("Status {0} recorded without an FSM transition").format(
				result.error_code or "unknown"
			)

		log.save(ignore_permissions=True)
		frappe.db.commit()

		frappe.publish_realtime(
			f"payment.intent.{intent.name}.updated",
			{"intent_name": intent.name, "status": intent.status},
			after_commit=True,
		)
	except Exception as exc:  # noqa: BLE001 - a job failure must not retry forever
		log.status = "Failed"
		log.error_message = str(exc)[:2000]
		log.save(ignore_permissions=True)
		frappe.db.commit()
		frappe.log_error("Payrexx webhook processing failed", f"{log_name}: {frappe.get_traceback()}")


def _record_needs_human(intent, result: WebhookResult) -> None:  # noqa: ANN001
	"""Log a status that must not drive a transition, and alert if it is serious.

	``chargeback`` and ``disputed`` mean money is being taken back and someone has
	to answer for it; ``insecure`` means the money may have moved without the 3-D
	Secure liability shift. None of them is a refund, so the Payment Intent keeps
	its current state and a Payment Event carries the fact.
	"""
	status = result.error_code or "unknown"

	frappe.get_doc(
		{
			"doctype": "Payment Event",
			"payment_intent": intent.name,
			"event_type": f"payrexx.{status}",
			"from_status": intent.status,
			"to_status": intent.status,
			"event_source": "webhook",
			"payload_excerpt": result.payload_excerpt,
		}
	).insert(ignore_permissions=True)

	if status in NEEDS_HUMAN:
		frappe.log_error(
			f"Payrexx {status} on {intent.name}",
			f"Payment Intent {intent.name} received Payrexx status '{status}', which has no "
			f"equivalent in the Payment Intent FSM and was NOT applied. This needs a human: "
			f"a chargeback or dispute is not a refund, and 'insecure' means 3-D Secure was "
			f"unavailable or bypassed.\n\n{result.payload_excerpt}",
		)


def _payrexx_provider_name() -> str | None:
	"""First enabled Payment Provider record with a Payrexx driver class.

	Detected by driver class rather than record name, so ``payrexx_test`` and
	``payrexx_live`` can cohabit.
	"""
	return frappe.db.get_value(
		"Payment Provider",
		{"driver_class": ["like", "payments.drivers.payrexx.%"], "enabled": 1},
		"name",
		order_by="modified desc",
	)


def _signing_key(provider_name: str) -> str | None:
	provider_doc = frappe.get_doc("Payment Provider", provider_name)
	creds = provider_doc.get_credentials() or {}
	value = creds.get("webhook_signing_key")
	return str(value).strip() if value else None


def _safe_truncate(payload: bytes, max_bytes: int = 200_000) -> str:
	"""Decode + truncate so a log row stays a reasonable size."""
	try:
		text = payload.decode("utf-8")
	except UnicodeDecodeError:
		text = payload.decode("utf-8", errors="replace")
	if len(text) > max_bytes:
		text = text[:max_bytes] + "...[truncated]"
	return text


def poll_pending_payrexx_transactions() -> None:
	"""Scheduler fallback for deliveries that never arrived.

	The webhook is the primary channel; this only catches Payment Intents left
	non-final for a while — a webhook lost, or a shopper who closed the tab before
	the return page ran.

	Kept deliberately narrow because of the rate limit (~600 requests / 5 minutes
	per account): only ``payrexx_web`` intents, only those older than five minutes,
	newest first, capped per run. Terminal intents are excluded — reading an ECR
	payment needs its serial and the terminal itself, and a till is watched live by
	the operator anyway.
	"""
	from payments.drivers.registry import resolve_driver

	cutoff = frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=-5)
	pending = frappe.get_all(
		"Payment Intent",
		filters={
			"provider": ["like", "payrexx%"],
			"channel": "payrexx_web",
			"status": ["in", ["requires_action", "processing"]],
			"modified": ["<", cutoff],
		},
		fields=["name", "provider", "channel", "provider_intent_id"],
		order_by="modified desc",
		limit_page_length=40,
	)

	for row in pending:
		if not row.provider_intent_id:
			continue
		try:
			driver = resolve_driver(row.provider, row.channel)
			response = driver.get_status(row.provider_intent_id)
			if response.status in ("succeeded", "failed", "canceled", "refunded"):
				intent = frappe.get_doc("Payment Intent", row.name)
				intent.transition_to(
					response.status,
					event_source="poll",
					payload_excerpt=f"payrexx gateway {row.provider_intent_id}",
					ignore_invalid=True,
				)
				frappe.db.commit()
		except Exception as exc:  # noqa: BLE001 - one bad row must not stop the sweep
			frappe.log_error("Payrexx poll failed", f"{row.name}: {exc!r}")
