import frappe
from frappe.utils import flt
from frappe.website.doctype.web_form.web_form import WebForm

# //// Neoffice — core's `accept`, under its own name: see `accept` below.
from frappe.website.doctype.web_form.web_form import accept as core_accept

from payments.utils import get_payment_gateway_controller


class PaymentWebForm(WebForm):
	def validate(self):
		super().validate()

		if getattr(self, "accept_payment", False):
			self.validate_payment_amount()

	def validate_payment_amount(self):
		if self.amount_based_on_field and not self.amount_field:
			frappe.throw(frappe._("Please select a Amount Field."))
		elif not self.amount_based_on_field and not flt(self.amount) > 0:
			frappe.throw(frappe._("Amount must be greater than 0."))

	def get_payment_gateway_url(self, doc):
		if getattr(self, "accept_payment", False):
			controller = get_payment_gateway_controller(self.payment_gateway)

			title = f"Payment for {doc.doctype} {doc.name}"
			amount = self.amount
			if self.amount_based_on_field:
				amount = doc.get(self.amount_field)

			from decimal import Decimal

			if amount is None or Decimal(amount) <= 0:
				return frappe.utils.get_url(self.success_url or self.route)

			payment_details = {
				"amount": amount,
				"title": title,
				"description": title,
				"reference_doctype": doc.doctype,
				"reference_docname": doc.name,
				"payer_email": frappe.session.user,
				"payer_name": frappe.utils.get_fullname(frappe.session.user),
				"order_id": doc.name,
				"currency": self.currency,
				"redirect_to": frappe.utils.get_url(self.success_url or self.route),
			}

			# Redirect the user to this url
			return controller.get_payment_url(**payment_details)


# //// Neoffice — this override was an old copy of core's `accept` that took the DocType from the request, so
# //// a visitor could insert into, or save through a form's fields, a DocType of their choice, a Single
# //// included. The copy had also lost core's published, key, anonymous and allow_incomplete handling, its
# //// POST rule and its limit. Core's `accept` now does all of that on the form's own `doc_type` (its rate
# //// limit runs inside this call), and only what a paid form adds is left here. A paid form's checks run
# //// after core saved: a refusal raises and the request rolls back, so nothing stays.
# permission-check: ok a public door: core's accept decides who may send what, on the form's own doc_type
@frappe.whitelist(methods=["POST", "PUT"], allow_guest=True)
def accept(
	web_form: str,
	data: str | dict,
	web_form_request_key: str | None = None,
	for_payment: bool | str | None = False,
):
	"""Save the web form through core; a paid form then hands its visitor to the gateway."""
	for_payment = frappe.parse_json(for_payment)
	doc = core_accept(web_form, data, web_form_request_key)

	web_form = frappe.get_doc("Web Form", web_form)
	if for_payment and getattr(web_form, "accept_payment", False):
		web_form.validate_mandatory(doc)
		doc.run_method("validate_payment")

		# this is needed for Payments app
		return web_form.get_payment_gateway_url(doc)

	return doc
