# Copyright (c) 2021, Frappe Technologies Pvt. Ltd. and Contributors
# License: MIT. See LICENSE
import json

import frappe
from frappe import _
from frappe.utils import cint, fmt_money

#//// Neoffice — this file diverges in `get_context` only (c187f68, 2026-08-24
#//// "un montant sans devise, un titre en double, deux libellés anglais"), plus the
#//// `neoffice_amount` helper it adds. Both are marked in place below.
#//// TO REVIEW (RULE #00): those in-place markers, the helper's docstring and its
#//// local names (`montant`, `symbole`, `a_droite`) are in French — a defect this
#//// comment-only pass is not allowed to fix. Rename on the next edit of the file.
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

		#//// Neoffice — le montant porte sa DEVISE. `fmt_money` la retire dès que
		#//// le défaut global `hide_currency_symbol` vaut « Yes » (le cas chez
		#//// nous, et défendable au desk où chaque colonne annonce la sienne) :
		#//// la page de paiement n'affichait plus que « 156.00 ». Sur l'écran où
		#//// l'on sort sa carte, l'unité n'est pas un détail.
		context["amount"] = neoffice_amount(context["amount"], context["currency"])

		#//// Neoffice — le paramètre `title` de l'URL porte le nom de la SOCIÉTÉ,
		#//// et l'écrire dans `context.title` le faisait servir de titre de page :
		#//// le thème le répétait en grand titre et dans le fil d'Ariane, au-dessus
		#//// d'une carte qui le disait déjà. On le déplace, et la page reprend son
		#//// vrai nom.
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


def neoffice_amount(amount, currency: str = None) -> str:
	"""#//// Neoffice — un montant lisible par un client : avec sa devise.

	`fmt_money` l'omet quand le défaut global `hide_currency_symbol` vaut
	« Yes ». On formate donc le nombre seul, puis on remet la devise du côté
	que sa fiche Currency indique.
	"""
	montant = fmt_money(amount=amount)
	if not currency:
		return montant
	symbole = frappe.db.get_value("Currency", currency, "symbol", cache=True) or currency
	a_droite = frappe.db.get_value("Currency", currency, "symbol_on_right", cache=True)
	return f"{montant} {_(symbole)}" if a_droite else f"{_(symbole)} {montant}"


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
