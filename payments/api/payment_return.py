# //// Neoffice — added file (no upstream equivalent). The calendar file of what a payment bought (an appointment), for the
# //// button « Add to my calendar » of the return page (payments/utils/payment_return.py). Open to guests like the page it
# //// belongs to, and guarded like it: only the browser that holds the intent's token, or a desk user who may read it.
# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""The calendar file of a payment's return page."""

from __future__ import annotations

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit

#: The channels whose return pages hold the intent's token (see ``intent_access``): the file is open to each of them.
_WEB_CHANNELS = ("wallee_web", "payrexx_web")


@frappe.whitelist(allow_guest=True)
@rate_limit(limit=60, seconds=3600)
def calendar(
	payment_intent: str, access_token: str | None = None
):  # permission-check: ok guarded by the intent's own access token
	"""Download what the payment was for as a calendar file (``.ics``); refused like « not found » when the caller holds no right on the intent."""
	from payments.api.intent_access import require_web_access
	from payments.utils.payment_return import calendar_file, summary_of

	intent = None
	for channel in _WEB_CHANNELS:
		try:
			intent = require_web_access(payment_intent, access_token, channel, "read")
			break
		except frappe.PermissionError:
			frappe.clear_messages()
	if intent is None:
		frappe.throw(_("Not permitted"), frappe.PermissionError)

	summary = summary_of(intent)
	if not summary or not summary.get("events") or summary.get("cancelled"):
		frappe.throw(_("Nothing to add to a calendar."), frappe.DoesNotExistError)

	frappe.response["filename"] = "appointment.ics"
	frappe.response["filecontent"] = calendar_file(summary)
	frappe.response["type"] = "download"
	frappe.response["content_type"] = "text/calendar"  # frappe adds the charset itself
