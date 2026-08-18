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

The uuid can also be **absent**: the back office's *Send test data* button posts a
transaction with ``uuid: null``. Observed on 2026-08-11 — without a fallback every
test delivery of a given status collapses onto one id, and the second looks like a
duplicate. :func:`_derive_event_id` falls back to a payload fingerprint.

**Statuses with no FSM equivalent.** ``chargeback``, ``disputed``, ``insecure``
and ``uncaptured`` do not map onto any of our six states — a chargeback is not a
refund, and pretending otherwise misstates the books. They are recorded and
surfaced to an operator instead of driving a transition.
"""

from __future__ import annotations

import hashlib

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
			event_id=f"payrexx_nontransaction_{_payload_fingerprint(payload)}",
			event_type="non_transaction",
			signature_valid=True,
			payload_excerpt=_safe_truncate(payload, 2_000),
		)

	raw_status = str(tx.status) if tx.status else ""
	target = map_status(raw_status)

	return WebhookResult(
		event_id=_derive_event_id(tx, raw_status, payload),
		event_type=f"transaction.{raw_status}",
		signature_valid=True,
		intent_name=_resolve_intent_name(tx),
		target_status=target,
		error_code=None if target else (raw_status or None),
		payload_excerpt=_safe_truncate(payload, 2_000),
	)


def _resolve_intent_name(tx) -> str | None:  # noqa: ANN001
	"""Find which Payment Intent a delivery belongs to.

	The reference does not arrive in the same field on every channel, which Payrexx
	confirmed on 2026-08-18:

	- **Hosted checkout** round-trips ``referenceId`` — verified against the live
	  account, and it is the intent name exactly.
	- **Terminal (ECR) and Tap to Pay** return the reference as ``invoice.purpose``
	  instead. ``referenceId`` is still present on those deliveries but is **not
	  reserved for the merchant** — TWINT puts its own identifier there — so matching
	  on it would silently attach a payment to the wrong intent, or to none.

	Rather than branch on ``transaction.type`` (whose exact strings we have not
	observed for Tap to Pay), each candidate is checked against the database and the
	first one that names a real Payment Intent wins. That is both channel-agnostic and
	safe: a value that matches nothing cannot be mistaken for a match.

	``purpose`` also carries a human label on the web channel ("Payment PI-…"), so it
	is tried both whole and with a trailing intent name extracted — but only ever
	accepted when it resolves to an existing record.
	"""
	candidates: list[str] = []
	for value in (tx.reference_id, getattr(tx, "purpose", None)):
		if not value:
			continue
		text = str(value).strip()
		if text and text not in candidates:
			candidates.append(text)
		# "Payment PI-2026-00000052" → "PI-2026-00000052". Only a suffix is taken; a
		# label that merely mentions an intent still has to exist to be accepted.
		tail = text.rsplit(" ", 1)[-1] if " " in text else ""
		if tail and tail not in candidates:
			candidates.append(tail)

	for candidate in candidates:
		if frappe.db.exists("Payment Intent", candidate):
			return candidate

	# Nothing matched. Return the first candidate anyway so the log row says what came
	# in — process_event turns that into a "Skipped" with the reference quoted, which is
	# far more useful than a bare "no intent".
	return candidates[0] if candidates else None


def _payload_fingerprint(payload: bytes, length: int = 16) -> str:
	"""A stable fingerprint of a raw payload.

	``hashlib`` rather than the builtin ``hash()``: Python randomises string hashing
	per process unless ``PYTHONHASHSEED`` is fixed, so ``hash()`` would give a
	different id in every worker — and the whole point of the id is that a redelivery
	of the same bytes deduplicates against the first one.
	"""
	return hashlib.sha256(payload).hexdigest()[:length]


def _derive_event_id(tx, raw_status: str, payload: bytes) -> str:  # noqa: ANN001
	"""Build a de-duplication id for a transaction delivery.

	Payrexx sends no event id, so one is derived from the transaction uuid and its
	status: stable across the up-to-10 redeliveries, distinct per genuine state
	change.

	When the uuid is absent the payload fingerprint is used instead. That is not
	hypothetical — the back office's *Send test data* button posts a transaction
	with ``uuid: null``, which would otherwise collapse every test of a given status
	onto one id and make the second delivery look like a duplicate of the first.
	"""
	if tx.uuid:
		return f"payrexx_{tx.uuid}_{raw_status}"
	return f"payrexx_nouuid_{raw_status}_{_payload_fingerprint(payload)}"


def process_event(log_name: str) -> None:
	"""RQ job — apply a verified delivery to the Payment Intent FSM.

	Field names here follow the ``Webhook Event Log`` doctype exactly: the error
	field is ``error`` (not ``error_message``), and ``status`` is a Select limited to
	``Queued`` / ``Processed`` / ``Failed`` / ``Skipped`` — there is no ``Ignored``.
	Writing outside that set leaves the row stuck at ``Queued``, which is precisely
	what happened before this was tested against a real delivery.
	"""
	from frappe.utils import now_datetime

	log = frappe.get_doc("Webhook Event Log", log_name)

	try:
		result = parse_delivery(
			(log.raw_payload or "").encode("utf-8"),
			headers={},
			signing_key=None,  # already verified at ingress; re-checking is pointless
		)

		if not result.intent_name or not frappe.db.exists("Payment Intent", result.intent_name):
			# Not an error: Payrexx also delivers for payments that never went
			# through this app — a link paid from the back office, or a test
			# delivery whose referenceId is empty.
			log.status = "Skipped"
			log.error = _("No matching Payment Intent for reference {0}").format(
				result.intent_name or "-"
			)
			log.processed_at = now_datetime()
			log.save(ignore_permissions=True)
			frappe.db.commit()
			return

		intent = frappe.get_doc("Payment Intent", result.intent_name)
		log.intent = intent.name

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
			log.error = _("Status {0} recorded without an FSM transition").format(
				result.error_code or "unknown"
			)

		log.processed_at = now_datetime()
		log.save(ignore_permissions=True)
		frappe.db.commit()

		extra = {}
		if intent.status == "succeeded" and intent.channel == "payrexx_web":
			redirect_to = _finalize_webshop_sales_order(intent)
			if redirect_to:
				extra["redirect_to"] = redirect_to

		frappe.publish_realtime(
			f"payment.intent.{intent.name}.updated",
			{"intent_name": intent.name, "status": intent.status, **extra},
			after_commit=True,
		)
	except Exception as exc:  # noqa: BLE001 - a job failure must not retry forever
		log.status = "Failed"
		log.error = str(exc)[:2000]
		log.processed_at = now_datetime()
		log.save(ignore_permissions=True)
		frappe.db.commit()
		frappe.log_error("Payrexx webhook processing failed", f"{log_name}: {frappe.get_traceback()}")


def _finalize_webshop_sales_order(intent) -> str | None:  # noqa: ANN001
	"""Create the webshop Sales Order for a succeeded ``payrexx_web`` intent.

	``/payrexx/success`` is the primary path, but it only runs if the shopper comes
	back: closing the tab on the Payrexx page leaves a paid transaction with the
	Payment Request still ``Draft`` and no Sales Order — money in, no order. The
	webhook is the one signal that always arrives, so it finalises too. Same shape as
	the TWINT poller's finalize step and Wallee's
	``_finalize_wallee_web_sales_order`` — with the difference that those two only
	run from the scheduler, so a webhook-driven success is never finalised there.

	Idempotent, which is what makes running both paths safe: on a Payment Request
	already ``Paid``, ``handle_payment_success`` short-circuits, so whichever gets
	there first wins and the other is a no-op.

	Returns the redirect target when the webshop produced one, for the realtime
	message an open checkout tab may be listening to.
	"""
	if intent.reference_doctype != "Payment Request" or not intent.reference_name:
		return None

	try:
		from webshop.controllers.payment_handler import handle_payment_success

		result = handle_payment_success(payment_request_id=intent.reference_name)
	except ImportError:
		return None  # webshop app not installed on this site
	except Exception as exc:  # noqa: BLE001 - the payment stands even if the order fails
		frappe.log_error(
			"Payrexx webhook finalize failed",
			f"intent={intent.name} pr={intent.reference_name}: {exc!r}\n\n{frappe.get_traceback()}",
		)
		return None

	if result and result.get("status") == "success":
		return result.get("redirect_to")

	# Not an exception, so nothing reaches Error Log on its own — but a paid
	# transaction with no order is exactly what someone has to look at.
	frappe.log_error(
		"Payrexx webhook finalize refused",
		f"intent={intent.name} pr={intent.reference_name} result={result!r}",
	)
	return None


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
	"""Which Payrexx provider this delivery belongs to.

	No channel is passed: Payrexx sends one webhook stream for every channel, so the
	provider cannot be narrowed by the one being served. See
	:func:`payments.drivers.payrexx._common.resolve_provider_name` for why the choice
	is not simply "the most recently modified record".
	"""
	from payments.drivers.payrexx._common import resolve_provider_name

	return resolve_provider_name()


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

	_retry_unfinalized_orders(cutoff)


def _retry_unfinalized_orders(cutoff) -> None:  # noqa: ANN001
	"""Retry order creation for paid intents whose Sales Order never materialised.

	Both finalisation paths — the return page and the webhook — can fail after the
	money has moved: webshop raising, a validation refusing the order, the site
	restarting mid-job. The intent is then ``succeeded``, which the sweep above no
	longer looks at since it only scans non-final states. Without this pass such a
	payment stays invisible until a customer complains.

	Costs no Payrexx request: the payment is already known-good, only the local
	document is missing.

	Bounded on both sides. The lower bound (``cutoff``, five minutes) keeps this from
	racing the webhook that is probably finalising right now. The upper bound of 24
	hours keeps it from reaching back through a site's whole history and settling old
	requests in silence — the same guard TWINT needed after it marked two Payment
	Requests from May as Paid on its first run. Past a day, an unfinalised payment is
	a human's call, not a background job's.
	"""
	horizon = frappe.utils.add_to_date(frappe.utils.now_datetime(), hours=-24)
	paid = frappe.get_all(
		"Payment Intent",
		filters={
			"provider": ["like", "payrexx%"],
			"channel": "payrexx_web",
			"status": "succeeded",
			"reference_doctype": "Payment Request",
			"modified": ["between", [horizon, cutoff]],
		},
		fields=["name", "reference_name"],
		order_by="modified desc",
		limit_page_length=40,
	)

	for row in paid:
		if not row.reference_name:
			continue
		pr_state = frappe.db.get_value(
			"Payment Request", row.reference_name, ["status", "docstatus"], as_dict=True
		)
		# docstatus 0 + not Paid is the signature of a finalisation that never ran:
		# handle_payment_success submits the request as part of creating the order.
		# Testing docstatus rather than the status alone matters — a submitted but
		# unpaid request is a different situation, and retrying it every five minutes
		# would just log the same failure forever. Same criterion as Wallee and TWINT.
		if not pr_state or pr_state.docstatus != 0 or pr_state.status == "Paid":
			continue
		try:
			_finalize_webshop_sales_order(frappe.get_doc("Payment Intent", row.name))
			frappe.db.commit()
		except Exception as exc:  # noqa: BLE001
			frappe.log_error("Payrexx finalize retry failed", f"{row.name}: {exc!r}")
