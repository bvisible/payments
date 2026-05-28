# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""``/wallee/failed`` — landing page when the Wallee redirect indicates failure.

URL : ``/wallee/failed?payment_intent=PI-2026-XXXXX`` (preferred)
   ou ``/wallee/failed?payment_request=ACC-PRQ-XXX`` (legacy compat).
"""

from __future__ import annotations

import frappe
from frappe import _

no_cache = 1


def get_context(context):  # noqa: ANN001
	from payments.www.wallee.success import _find_intent_name, _refresh_status

	intent_name = _find_intent_name(frappe.form_dict)

	context.intent = None
	context.payment_request = None
	context.failure_reason = None

	if not intent_name:
		context.failure_reason = _("Missing payment reference")
		return context

	if not frappe.db.exists("Payment Intent", intent_name):
		context.failure_reason = _("Payment reference not found")
		return context

	intent_doc = frappe.get_doc("Payment Intent", intent_name)
	context.intent = intent_doc

	# Best-effort sync — if Wallee already pushed a failed state, our DB will
	# pick it up. Worst case the page shows the last-known reason.
	try:
		_refresh_status(intent_doc, max_retries=2)
	except Exception as exc:  # noqa: BLE001
		frappe.log_error("Wallee failed page — refresh error", str(exc))

	context.failure_reason = intent_doc.error_message or _("Payment was declined or cancelled")
	return context
