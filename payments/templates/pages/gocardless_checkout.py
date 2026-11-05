# Copyright (c) 2018, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt
# //// Neoffice — the payer comes from the request, and contact details prefill only for who may read the Customer.

import frappe
from frappe import _
from frappe.utils import flt, get_url

from payments.payment_gateways.doctype.gocardless_settings.gocardless_settings import (
	get_gateway_controller,
	gocardless_initialization,
)
# //// Neoffice — the payer comes from the request, and contact details prefill only for who may read the Customer.
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
	if not (set(expected_keys) - set(frappe.form_dict.keys())):
		# //// Neoffice — `reference_docname` goes to `frappe.get_doc`: see `require_text`.
		require_text(*(frappe.form_dict[key] for key in expected_keys))
		for key in expected_keys:
			context[key] = frappe.form_dict[key]

		context["amount"] = flt(context["amount"])

		gateway_controller = get_gateway_controller(context.reference_docname)
		context["header_img"] = frappe.db.get_value(
			"GoCardless Settings", gateway_controller, "header_img"
		)

	else:
		frappe.redirect_to_message(
			_("Some information is missing"),
			_("Looks like someone sent you to an incomplete URL. Please ask them to look into it."),
		)
		frappe.local.flags.redirect_location = frappe.local.response.location
		raise frappe.Redirect


# //// Neoffice — added helper (no upstream equivalent), see `check_mandate`.
def _customer_of(payment_request) -> str | None:
	"""The Customer a Payment Request is addressed to, read from the server's records."""
	if payment_request.party_type == "Customer":
		return payment_request.party
	reference = payment_request.reference_doctype
	if reference and frappe.get_meta(reference).has_field("customer"):
		return frappe.db.get_value(reference, payment_request.reference_name, "customer")


# //// Neoffice — upstream loaded the Customer named by `data.payer_name` as the request gave it, a filter
# //// included: 200 or 403 read any Customer column out, and the matching contact's name and e-mail went to
# //// the gateway. The payer is now the Customer of the Payment Request, and its details prefill the gateway's
# //// form only for an account allowed to read that Customer: a visitor on the link types their own there.
# //// The amount said to them is the request's too. Annotated: unannotated, a JSON body made a filter of the
# //// document name.
# permission-check: ok a public door: a buyer's payment link; payer and amount are read from the request
@frappe.whitelist(allow_guest=True)
def check_mandate(data: str, reference_doctype: str, reference_docname: str):
	client = gocardless_initialization(reference_docname)

	# //// Neoffice — the payer comes from the request, and contact details prefill only for who may read the Customer.
	payment_request = frappe.get_doc("Payment Request", reference_docname)
	customer = _customer_of(payment_request)
	may_read = customer and frappe.has_permission("Customer", "read", doc=customer)
	payer = frappe.get_doc("Customer", customer) if may_read else None

	if not payer:
		prefilled_customer = {}

	# //// Neoffice — the payer comes from the request, and contact details prefill only for who may read the Customer.
	elif payer.customer_type == "Individual" and payer.customer_primary_contact is not None:
		primary_contact = frappe.get_doc("Contact", payer.customer_primary_contact)
		prefilled_customer = {
			"company_name": payer.name,
			"given_name": primary_contact.first_name,
		}
		if primary_contact.last_name is not None:
			prefilled_customer.update({"family_name": primary_contact.last_name})

		if primary_contact.email_id is not None:
			prefilled_customer.update({"email": primary_contact.email_id})
		else:
			prefilled_customer.update({"email": frappe.session.user})

	else:
		prefilled_customer = {"company_name": payer.name, "email": frappe.session.user}

	success_url = get_url(
		"gocardless_confirmation?reference_doctype="
		+ reference_doctype
		+ "&reference_docname="
		+ reference_docname
	)

	try:
		redirect_flow = client.redirect_flows.create(
			params={
				# //// Neoffice — the payer comes from the request, and contact details prefill only for who may read the Customer.
				"description": _("Pay {0} {1}").format(
					payment_request.grand_total, payment_request.currency
				),
				"session_token": frappe.session.user,
				"success_redirect_url": success_url,
				"prefilled_customer": prefilled_customer,
			}
		)

		return {"redirect_to": redirect_flow.redirect_url}

	except Exception as e:
		frappe.log_error("GoCardless Payment Error")
		return {"redirect_to": "payment-failed"}
