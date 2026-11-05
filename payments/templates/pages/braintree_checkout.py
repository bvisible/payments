# Copyright (c) 2021, Frappe Technologies Pvt. Ltd. and Contributors
# License: MIT. See LICENSE

import json

import frappe
from frappe import _
from frappe.utils import flt

from payments.payment_gateways.doctype.braintree_settings.braintree_settings import (
	get_client_token,
	get_gateway_controller,
)
# //// Neoffice — the amount, currency and document came from the client: they come from the Payment Request.
from payments.utils.utils import require_text

no_cache = 1

expected_keys = (
	"amount",
	"title",
	"description",
	"reference_doctype",
	"reference_docname",
	"payer_name",
	"payer_email",
	"order_id",
	"currency",
)


def get_context(context):
	context.no_cache = 1

	# all these keys exist in form_dict
	if not (set(expected_keys) - set(list(frappe.form_dict))):
		# //// Neoffice — `reference_docname` goes to `frappe.get_doc`: see `require_text`.
		require_text(*(frappe.form_dict[key] for key in expected_keys))
		for key in expected_keys:
			context[key] = frappe.form_dict[key]

		context.client_token = get_client_token(context.reference_docname)

		# //// Neoffice — the button says what `make_payment` will charge, not what the link says.
		payment_request = _payable_request(context.reference_docname)
		context["amount"] = _amount_due(payment_request)
		context["currency"] = payment_request.currency

		gateway_controller = get_gateway_controller(context.reference_docname)
		context["header_img"] = frappe.db.get_value(
			"Braintree Settings", gateway_controller, "header_img"
		)

	else:
		frappe.redirect_to_message(
			_("Some information is missing"),
			_("Looks like someone sent you to an incomplete URL. Please ask them to look into it."),
		)
		frappe.local.flags.redirect_location = frappe.local.response.location
		raise frappe.Redirect


# //// Neoffice — added helpers (no upstream equivalent), see `make_payment`.
def _payable_request(name: str):
	"""The Payment Request ``name``, refused once there is nothing left to pay on it."""
	payment_request = frappe.get_doc("Payment Request", name)
	if payment_request.docstatus != 1 or payment_request.status in ("Paid", "Cancelled"):
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	return payment_request


def _amount_due(payment_request) -> float:
	return flt(payment_request.outstanding_amount) or flt(payment_request.grand_total)


# //// Neoffice — upstream charged the `amount` the browser posted and booked the document it named in `data`,
# //// which could differ from the request behind the page: one cent paid an invoice in full. The amount, the
# //// currency and the document are now the server's, read from the request this page is for. Annotated too:
# //// unannotated, a JSON body made a filter of `reference_docname`.
# permission-check: ok a public door: a buyer's payment link; what it charges is read from the request
@frappe.whitelist(allow_guest=True)
def make_payment(payload_nonce: str, data: str, reference_doctype: str, reference_docname: str):
	data = json.loads(data)

	data.update({"payload_nonce": payload_nonce})

	# //// Neoffice — the amount, currency and document came from the client: they come from the Payment Request.
	payment_request = _payable_request(reference_docname)
	data.update(
		{
			"amount": str(_amount_due(payment_request)),
			"currency": payment_request.currency,
			"reference_doctype": "Payment Request",
			"reference_docname": payment_request.name,
		}
	)

	gateway_controller = get_gateway_controller(reference_docname)
	data = frappe.get_doc("Braintree Settings", gateway_controller).create_payment_request(data)
	frappe.db.commit()
	return data
