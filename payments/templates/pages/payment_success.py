# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and Contributors
# License: MIT. See LICENSE

import frappe

from payments.utils.utils import require_text, safe_redirect_to

no_cache = True


def get_context(context):
	token = frappe.local.form_dict.token
	doctype, docname = frappe.local.form_dict.doctype, frappe.local.form_dict.docname
	# //// Neoffice — `docname` went to `frappe.get_doc` as it came: a filter from a JSON body gave a guest a yes
	# //// or no on any stored value of any DocType. And a text name still told a guest which documents exist
	# //// (200, or an error): a name that resolves to nothing now reads like one that resolves to no message.
	require_text(token, doctype, docname)
	doc = None
	if doctype and docname and frappe.db.exists("DocType", doctype) and frappe.db.exists(doctype, docname):
		doc = frappe.get_doc(doctype, docname)

	context.payment_message = ""
	if hasattr(doc, "get_payment_success_message"):
		context.payment_message = doc.get_payment_success_message()

	# //// Neoffice — see payment-success.html.
	context.redirect_to = safe_redirect_to(frappe.local.form_dict.redirect_to)
