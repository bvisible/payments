# //// Neoffice — added file (no upstream equivalent). Controller of `/wallee/failed`:
# //// resolve the Payment Intent from `payment_intent` (or the legacy `payment_request`),
# //// best-effort refresh its status through the driver, and render the last known
# //// failure reason. Moved here from `www/wallee_failed.py` — Frappe's www resolver maps
# //// `/wallee/failed` to the path hierarchy, so the underscore variant answered 404 to
# //// every buyer coming back from the Wallee hosted page.
# //// Commits: ce478ca 2026-05-28 "fix(wallee-web): /wallee/success and /wallee/failed routes (404 → working)"
# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""``/wallee/failed`` — landing page when the Wallee redirect indicates failure.

//// Neoffice — added file. Its docstring carried a French "ou" until 2026-09-04
//// (RULE #00): everything inside a source file is English.
URL: ``/wallee/failed?payment_intent=PI-2026-XXXXX`` (preferred)
   or ``/wallee/failed?payment_request=ACC-PRQ-XXX`` (legacy compat).
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
