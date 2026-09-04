# Copyright (c) 2021, Frappe Technologies Pvt. Ltd. and Contributors
# License: MIT. See LICENSE
import json

import frappe
from frappe import _
from frappe.utils import cint, fmt_money

#//// Neoffice — this file diverges in `get_context` only (c187f68, 2026-08-24
#//// "un montant sans devise, un titre en double, deux libellés anglais"), plus the
#//// `neoffice_amount` helper it adds. Both are marked in place below. The helper's
#//// French locals and comments were rewritten in English on 2026-09-04 (RULE #00);
#//// no behaviour changed with them.
from payments.payment_gateways.doctype.stripe_settings.stripe_settings import (
	get_gateway_controller,
)

no_cache = 1

expected_keys = (
	"amount",
	"title",
	"description",
	"reference_doctype",
	"reference_docname",
	"payer_name",
	"payer_email",
	"currency",
	"payment_gateway",
)


def get_context(context):
	context.no_cache = 1

	# all these keys exist in form_dict
	if not (set(expected_keys) - set(list(frappe.form_dict))):
		for key in expected_keys:
			context[key] = frappe.form_dict[key]

		gateway_controller = get_gateway_controller(
			context.reference_doctype, context.reference_docname, context.payment_gateway
		)
		context.publishable_key = get_api_key(context.reference_docname, gateway_controller)
		context.image = get_header_image(context.reference_docname, gateway_controller)

		#//// Neoffice — the amount carries its CURRENCY. `fmt_money` drops the symbol
		#//// as soon as the global default `hide_currency_symbol` is "Yes" — which it
		#//// is here, and defensibly so on the desk where every column announces its
		#//// own — so the payment page showed a bare "156.00". On the screen where
		#//// someone takes out a card, the unit is not a detail.
		context["amount"] = neoffice_amount(context["amount"], context["currency"])

		#//// Neoffice — the URL's `title` parameter holds the COMPANY name, and writing
		#//// it to `context.title` made it the page title: the theme repeated it as a
		#//// heading and in the breadcrumb, above a card that already said it. Moved to
		#//// `payee`, and the page gets its own name back.
		context["payee"] = context.get("title")
		context["title"] = _("Payment")

		if is_a_subscription(context.reference_doctype, context.reference_docname):
			payment_plan = frappe.db.get_value(
				context.reference_doctype, context.reference_docname, "payment_plan"
			)
			recurrence = frappe.db.get_value("Payment Plan", payment_plan, "recurrence")

			context["amount"] = context["amount"] + " " + _(recurrence)

	else:
		frappe.log_error(
			"Missing keys in form_dict",
			"Expected keys: {}," "Received keys: {}".format(expected_keys, list(frappe.form_dict)),
		)
		frappe.redirect_to_message(
			_("Some information is missing"),
			_("Looks like someone sent you to an incomplete URL. Please ask them to look into it."),
		)
		frappe.local.flags.redirect_location = frappe.local.response.location
		raise frappe.Redirect


#//// Neoffice — added helper (no upstream equivalent): an amount a customer can
#//// read, currency included. `fmt_money` omits the symbol when the global default
#//// `hide_currency_symbol` is "Yes", so the number is formatted on its own and the
#//// symbol put back on the side the Currency record says it belongs.
def neoffice_amount(amount, currency: str = None) -> str:
	"""Format an amount for a payment screen, currency symbol included."""
	#//// Neoffice — the three locals were French (`montant`, `symbole`,
	#//// `a_droite`) until 2026-09-04. Renamed only; the logic is untouched.
	formatted = fmt_money(amount=amount)
	if not currency:
		return formatted
	symbol = frappe.db.get_value("Currency", currency, "symbol", cache=True) or currency
	symbol_on_right = frappe.db.get_value(
		"Currency", currency, "symbol_on_right", cache=True
	)
	return f"{formatted} {_(symbol)}" if symbol_on_right else f"{_(symbol)} {formatted}"


def get_api_key(doc, gateway_controller):
	publishable_key = frappe.db.get_value("Stripe Settings", gateway_controller, "publishable_key")
	if cint(frappe.form_dict.get("use_sandbox")):
		publishable_key = frappe.conf.sandbox_publishable_key

	return publishable_key


def get_header_image(doc, gateway_controller):
	return frappe.db.get_value("Stripe Settings", gateway_controller, "header_img")


@frappe.whitelist(allow_guest=True)
def make_payment(
	stripe_token_id, data, reference_doctype=None, reference_docname=None, payment_gateway=None
):
	data = json.loads(data)

	data.update({"stripe_token_id": stripe_token_id})

	gateway_controller = get_gateway_controller(reference_doctype, reference_docname, payment_gateway)

	if is_a_subscription(reference_doctype, reference_docname):
		reference = frappe.get_doc(reference_doctype, reference_docname)
		data = reference.create_subscription("stripe", gateway_controller, data)
	else:
		data = frappe.get_doc("Stripe Settings", gateway_controller).create_request(data)

	frappe.db.commit()
	return data


def is_a_subscription(reference_doctype, reference_docname):
	if not frappe.get_meta(reference_doctype).has_field("is_a_subscription"):
		return False
	return frappe.db.get_value(reference_doctype, reference_docname, "is_a_subscription")
