# //// Neoffice — added file (no upstream equivalent). What the three webhook receivers share since the
# //// audit of this app (Stripe, Wallee, Payrexx). They read only the provider saved last, told a guest
# //// through a 503 which providers exist, wrote one Error Log row per anonymous POST and sent each status to
# //// every desk account. A delivery now proves itself against the provider that holds its intent, and one
# //// that proves nothing gets one answer and leaves no row of its own.
# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""What the webhook receivers share.

How each provider proves a delivery:

- **Stripe**: ``Stripe-Signature``, an HMAC-SHA256 of the timestamp and the raw body under the webhook secret
  of the provider or of its binding, read by the Stripe SDK with its time tolerance.
- **Wallee**: ``X-Signature``, an HMAC-SHA256 of the raw body under ``Wallee Settings.webhook_secret``.
- **Payrexx**: ``X-Webhook-Signature``, an HMAC-SHA256 of the raw body under ``webhook_signing_key``.
- **TWINT** pushes nothing: the poller asks the bridge. The Stripe web driver is a stub with no webhook.

No secret, no proof: a provider that holds none refuses every delivery. The worker that applies a stored
delivery does not check again, since it has no headers to check: what stands between a request and that
worker is the row, and only a proven delivery writes one.
"""

from __future__ import annotations

from typing import Any

import frappe

_LOG_EVERY_SECONDS = 3600


def log_once_an_hour(title: str, message: str) -> None:
	"""One Error Log row per title and per hour: the operator hears of it, a guest fills no table."""
	key = f"payments_refused_delivery:{title}"
	if frappe.cache.get_value(key):
		return
	frappe.cache.set_value(key, 1, expires_in_sec=_LOG_EVERY_SECONDS)
	frappe.log_error(title[:140], message)


def refuse_delivery(psp: str, reason: str | None) -> str:
	"""Answer a delivery that proves nothing, whatever the reason.

	One status and one text: a missing provider, a missing secret and a wrong signature must not read
	differently to a guest. The reason goes to the operator instead.
	"""
	reason = reason or "no enabled provider"
	log_once_an_hour(
		f"{psp} webhook refused: {reason}", f"Delivery refused ({reason}). Nothing was stored."
	)
	frappe.local.response["http_status_code"] = 400
	return "invalid signature"


def provider_of(driver) -> str:  # noqa: ANN001
	return driver.provider.provider_doc.name


def webhook_drivers(driver_class_like: str, provider: str | None = None) -> list:
	"""A driver per enabled binding of the enabled providers of one PSP, the provider saved last first.

	Only the drivers that can read a webhook: the Stripe web stub raises NotImplementedError on it.
	"""
	from payments.drivers.registry import DriverResolutionError, resolve_driver

	filters: dict[str, Any] = {"driver_class": ["like", driver_class_like], "enabled": 1}
	if provider:
		filters["name"] = provider

	drivers = []
	providers = frappe.get_all(
		"Payment Provider", filters=filters, order_by="modified desc", pluck="name"
	)
	for name in providers:
		channels = frappe.get_all(
			"Provider Channel Settings",
			filters={"provider": name, "enabled": 1},
			order_by="modified desc",
			pluck="channel",
		)
		for channel in channels:
			try:
				driver = resolve_driver(name, channel)
			except (DriverResolutionError, frappe.DoesNotExistError):
				continue
			if hasattr(driver, "parse_verified_event"):
				drivers.append(driver)
	return drivers


def signed_delivery(driver_class_like: str, payload: bytes, headers: dict[str, str]) -> tuple:
	"""``(driver, result)`` of the binding whose secret signed this delivery, or ``(None, the last refusal)``.

	Every enabled binding is asked: a test and a live provider, or two bindings of one provider, each hold a
	secret of their own, and a delivery is checked against the secret of whoever holds its intent. Two records
	of one account share a secret, so the holder of the intent is preferred; a delivery proven only by another
	provider stays that provider's, and the worker does not let it move the intent.
	"""
	signed = None
	refused = None
	for driver in webhook_drivers(driver_class_like):
		try:
			result = driver.handle_webhook(payload, headers)
		except Exception as exc:  # noqa: BLE001 — a provider that cannot answer proves nothing
			log_once_an_hour(f"Webhook unreadable for {provider_of(driver)}", repr(exc))
			continue
		if not result.signature_valid:
			refused = result
			continue
		holder = result.intent_name and frappe.db.get_value(
			"Payment Intent", result.intent_name, "provider"
		)
		if not holder or holder == provider_of(driver):
			return driver, result
		signed = signed or (driver, result)
	return signed or (None, refused)


# //// Neoffice — maintenance#2296: the rows a job has not finished. A retry of their delivery queues them again.
UNFINISHED = ("Queued", "Failed")


def queue_event(method: str, job_prefix: str, log_name: str) -> None:
	"""Queue the job that applies a stored delivery, once at a time (its id is the row's)."""
	frappe.enqueue(
		method,
		queue="short",
		job_id=f"{job_prefix}-{log_name}",
		deduplicate=True,
		enqueue_after_commit=True,
		log_name=log_name,
	)


def received_before(event_id: str, method: str, job_prefix: str) -> bool:
	"""True when this delivery has a row already. A row its job never finished is queued again.

	The dedup answered « ok » to any row it had, whatever became of it (maintenance#2296). The row is committed
	before its job is queued: a queue that was down (the provider got a 500 and retries), a job lost or one that
	died left it « Queued » for good, and every retry was told « ok » with nothing done; Stripe Terminal has no
	poller to make up for it. The job is idempotent: a move to the status an intent has is no move, a booking is
	stamped once, a capture carries its key.
	"""
	status = frappe.db.get_value("Webhook Event Log", event_id, "status")
	if status is None:
		return False
	if status in UNFINISHED:
		queue_event(method, job_prefix, event_id)
	return True


def record_delivery(values: dict):  # noqa: ANN201 - a Webhook Event Log, or None
	"""The delivery's row; None when a twin delivery of the same event wrote it first.

	A provider may send one event twice at once (Wallee, 2026-11-10: two deliveries one second apart). Both
	passed received_before, the second insert hit the unique name and answered 409, so the provider counted a
	delivered event as failed and sent it again. The twin is the same event: the caller answers « ok ».
	"""
	try:
		return frappe.get_doc({"doctype": "Webhook Event Log", **values}).insert(ignore_permissions=True)
	except frappe.DuplicateEntryError:
		frappe.db.rollback()
		return None


def fail_the_row(log, exc: Exception) -> None:  # noqa: ANN001
	"""A job could not apply a proven delivery: what it wrote is undone, and its row says why.

	The job caught only a missing intent, so any other error (two moves of one intent at once, a booking that
	failed) rolled it back with its row left « Queued » and no trace of why (maintenance#2296). A retry of the
	delivery queues a « Failed » row again (received_before).
	"""
	frappe.db.rollback()
	log.reload()
	log.status = "Failed"
	log.error = repr(exc)[:2000]
	log.processed_at = frappe.utils.now_datetime()
	log.save(ignore_permissions=True)
	frappe.log_error("Webhook delivery not applied", f"{log.name}: {frappe.get_traceback()}")


def skip_foreign_intent(log, intent_doc) -> None:  # noqa: ANN001
	"""A proven delivery names an intent another provider holds: it is not that provider's word."""
	log.status = "Skipped"
	log.error = (
		f"Payment Intent {intent_doc.name} is held by {intent_doc.provider}, not by {log.provider}"
	)
	log.processed_at = frappe.utils.now_datetime()
	log.save(ignore_permissions=True)


def publish_intent_update(intent_doc, message: dict[str, Any]) -> None:  # noqa: ANN001
	"""Tell the account that started a payment how it went, and nobody else.

	With no room, Frappe sends an event to every desk account. What this one carries (an intent, its status,
	the thank-you address with its Sales Order) says when a shop sells and how often. The till and the desk
	screens that listen to it are the account that created the intent, and each of them also polls.

	A guest's intent is published to nobody. ``user:Guest`` is a room every anonymous visitor sits in, and a
	guest never received this event anyway: a guest is in no desk room, its page polls with its token.
	"""
	if intent_doc.owner == "Guest":
		return
	frappe.publish_realtime(
		event=f"payment.intent.{intent_doc.name}.updated",
		message=message,
		user=intent_doc.owner,
		after_commit=True,
	)


# //// Neoffice — maintenance#2296 (audit Q11): a row its job never finished, of an event its provider never sends
# //// again, is taken up here. received_before queues such a row again when the provider repeats the event; Stripe
# //// repeats only what it was not answered « ok » for, and it was: the row is committed before its job is queued, so
# //// a job lost (a restart at the wrong moment, a queue emptied) or one that failed left a card Stripe charged with no
# //// payment in the ERP, the till showing the sale unpaid and the cashier offering to take it again. The polls of
# //// Wallee and Payrexx make up for their providers; nothing did for Stripe. A sweep every five minutes queues such a
# //// row again, five times at most, then tells the managers, once. The job is idempotent (see received_before): a
# //// replay moves no intent twice, writes no second Payment Event and books no second payment.
RETRY_AFTER_MINUTES = 10
MAX_RETRIES = 5
# A row older than this is never replayed by the sweep (someone may have settled the sale by hand since): it is
# reported, once, as long as it is younger than ALERT_WITHIN_DAYS
REPLAY_WITHIN_DAYS = 3
ALERT_WITHIN_DAYS = 30
# Who hears of a payment the ERP could not record: who can check it with the provider and book or refund it
ALERT_ROLES = ("Accounts Manager", "System Manager")


def retry_unfinished_events(
	driver_class_like: str, method: str, job_prefix: str, psp: str, describe=None
) -> dict:
	"""Queue again the rows of one PSP left « Queued » or « Failed » for RETRY_AFTER_MINUTES, MAX_RETRIES times at
	most; report the rows past that, once. Answers the names queued and the names reported.

	`describe(row)` gives a line of the report for a row (its amount, its intent): the PSP reads its own payload.
	"""
	from frappe.utils import add_to_date, cint, now_datetime

	now = now_datetime()
	providers = frappe.get_all(
		"Payment Provider", filters={"driver_class": ["like", driver_class_like]}, pluck="name"
	)
	if not providers:
		return {"queued": [], "reported": []}
	rows = frappe.get_all(
		"Webhook Event Log",
		filters={
			"provider": ["in", providers],
			"status": ["in", list(UNFINISHED)],
			"modified": ["<", add_to_date(now, minutes=-RETRY_AFTER_MINUTES)],
			"creation": [">", add_to_date(now, days=-ALERT_WITHIN_DAYS)],
			"alerted_at": ["is", "not set"],
		},
		fields=["name", "event_type", "status", "retries", "creation", "provider", "raw_payload"],
		order_by="creation asc",
		limit_page_length=200,
	)
	replay_after = add_to_date(now, days=-REPLAY_WITHIN_DAYS)
	queued, lost = [], []
	for row in rows:
		if cint(row.retries) < MAX_RETRIES and row.creation > replay_after:
			# Its `modified` moves with the count: the next try waits RETRY_AFTER_MINUTES again
			frappe.db.set_value("Webhook Event Log", row.name, "retries", cint(row.retries) + 1)
			queue_event(method, job_prefix, row.name)
			queued.append(row.name)
		else:
			lost.append(row)
	if lost:
		_report_unrecorded(psp, lost, describe)
		for row in lost:
			frappe.db.set_value("Webhook Event Log", row.name, "alerted_at", now, update_modified=False)
	frappe.db.commit()  # noqa: T201 — the counts and the marks stand, and the queued jobs leave
	return {"queued": queued, "reported": [row.name for row in lost]}


def _report_unrecorded(psp: str, rows: list, describe=None) -> None:
	"""Tell the managers, once, of the events of `psp` the ERP could not record: an Error Log row and a notification."""
	from frappe import _
	from frappe.utils import format_datetime

	lines = []
	for row in rows:
		said = ""
		if describe:
			try:
				said = describe(row) or ""
			except Exception:  # noqa: BLE001 — a payload that cannot be read is still reported
				said = ""
		lines.append(
			f"- {row.event_type or '?'} — {format_datetime(row.creation)} — {row.name}"
			+ (f" — {said}" if said else "")
		)
	message = _(
		"{0} sent {1} event(s) that the ERP could not record, even after {2} tries. A 'succeeded' event is a"
		" customer charged at {0} with no payment recorded in the ERP.\n\n{3}\n\n"
		"To do: check each line in the {0} dashboard, then record the payment, or refund the customer if the sale"
		" was taken again."
	).format(psp, len(rows), MAX_RETRIES, "\n".join(lines))
	subject = _("Paid at {0}, not recorded: {1} event(s)").format(psp, len(rows))
	frappe.log_error(subject[:140], message)
	try:
		users = frappe.get_all(
			"Has Role",
			filters={"role": ["in", list(ALERT_ROLES)], "parenttype": "User"},
			pluck="parent",
			distinct=True,
		)
		for user in sorted(set(users) - {"Administrator", "Guest"}):
			if not frappe.db.get_value("User", user, "enabled"):
				continue
			note = frappe.new_doc("Notification Log")
			note.subject = subject
			note.email_content = message.replace("\n", "<br>")
			note.for_user = user
			note.type = "Alert"
			note.document_type = "Webhook Event Log"
			note.insert(ignore_permissions=True)
	except Exception:  # noqa: BLE001 — the Error Log row stands even when a notification cannot be written
		frappe.log_error("Unrecorded payment events: notification failed", frappe.get_traceback())
