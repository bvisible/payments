# Copyright (c) 2017, Frappe Technologies and contributors
# License: MIT. See LICENSE

# //// ═══════════════════════════════════════════════════════════════════════════
# //// Neoffice — every `frappe.log_error(...)` call in this file is ours (7b99cbf,
# //// 2025-02-28 "update error log and stripe version"). Besides that, the file diverges
# //// from upstream in three places, each marked in place: the receipt of a captured
# //// charge (`stripe_receipt`, maintenance#1807), the check that a charge covers the
# //// Payment Request it names before that request is booked as paid, and
# //// `get_gateway_controller`, which no longer tells a guest which documents exist.
# ////
# //// Why: Frappe v15 signs it `log_error(title, message)` and writes the title to
# //// `Error Log.method` — a **Data** field, so cut at 140 characters — while the
# //// body goes to `error` (Code, unbounded). Upstream calls it here with the
# //// traceback as the ONLY argument: the traceback lands in the title and is
# //// truncated, and the swap hack in `frappe/utils/error.py` cannot rescue it
# //// because that only fires when a message is passed too. A constant title also
# //// groups the entries, instead of one Error Log group per distinct message.
# ////
# //// Each call site below carries the upstream form it replaces. Sites marked
# //// TO REVIEW change behaviour, not just the log line — read them before merging.
# //// ═══════════════════════════════════════════════════════════════════════════
from types import MappingProxyType
from urllib.parse import urlencode

import frappe
from frappe import _
from frappe.integrations.utils import create_request_log, make_get_request
from frappe.model.document import Document
# //// Neoffice — `cint` went with the truncated charge amount (maintenance#2296).
from frappe.utils import call_hook_method, flt, get_url

from payments.utils import create_payment_gateway

# //// Neoffice — see create_charge_on_stripe: the receipt of a captured charge (maintenance#1807).
from payments.utils import stripe_receipt

# //// Neoffice — maintenance#2296: see payments/utils/all_or_nothing.py.
from payments.utils.all_or_nothing import all_or_nothing

# //// Neoffice — see get_gateway_controller.
from payments.utils.utils import require_text

currency_wise_minimum_charge_amount = {
	"JPY": 50,
	"MXN": 10,
	"DKK": 2.50,
	"HKD": 4.00,
	"NOK": 3.00,
	"SEK": 3.00,
	"USD": 0.50,
	"AUD": 0.50,
	"BRL": 0.50,
	"CAD": 0.50,
	"CHF": 0.50,
	"EUR": 0.50,
	"GBP": 0.30,
	"NZD": 0.50,
	"SGD": 0.50,
}


class StripeSettings(Document):
	supported_currencies = (
		"AED",
		"ALL",
		"ANG",
		"ARS",
		"AUD",
		"AWG",
		"BBD",
		"BDT",
		"BIF",
		"BMD",
		"BND",
		"BOB",
		"BRL",
		"BSD",
		"BWP",
		"BZD",
		"CAD",
		"CHF",
		"CLP",
		"CNY",
		"COP",
		"CRC",
		"CVE",
		"CZK",
		"DJF",
		"DKK",
		"DOP",
		"DZD",
		"EGP",
		"ETB",
		"EUR",
		"FJD",
		"FKP",
		"GBP",
		"GIP",
		"GMD",
		"GNF",
		"GTQ",
		"GYD",
		"HKD",
		"HNL",
		"HRK",
		"HTG",
		"HUF",
		"IDR",
		"ILS",
		"INR",
		"ISK",
		"JMD",
		"JPY",
		"KES",
		"KHR",
		"KMF",
		"KRW",
		"KYD",
		"KZT",
		"LAK",
		"LBP",
		"LKR",
		"LRD",
		"MAD",
		"MDL",
		"MNT",
		"MOP",
		"MRO",
		"MUR",
		"MVR",
		"MWK",
		"MXN",
		"MYR",
		"NAD",
		"NGN",
		"NIO",
		"NOK",
		"NPR",
		"NZD",
		"PAB",
		"PEN",
		"PGK",
		"PHP",
		"PKR",
		"PLN",
		"PYG",
		"QAR",
		"RUB",
		"SAR",
		"SBD",
		"SCR",
		"SEK",
		"SGD",
		"SHP",
		"SLL",
		"SOS",
		"STD",
		"SVC",
		"SZL",
		"THB",
		"TOP",
		"TTD",
		"TWD",
		"TZS",
		"UAH",
		"UGX",
		"USD",
		"UYU",
		"UZS",
		"VND",
		"VUV",
		"WST",
		"XAF",
		"XOF",
		"XPF",
		"YER",
		"ZAR",
	)

	currency_wise_minimum_charge_amount = MappingProxyType(currency_wise_minimum_charge_amount)

	def on_update(self):
		create_payment_gateway(
			"Stripe-" + self.gateway_name,
			settings="Stripe Settings",
			controller=self.gateway_name,
		)
		call_hook_method("payment_gateway_enabled", gateway="Stripe-" + self.gateway_name)
		if not self.flags.ignore_mandatory:
			self.validate_stripe_credentails()

	def validate_stripe_credentails(self):
		if self.publishable_key and self.secret_key:
			header = {
				"Authorization": "Bearer {}".format(
					self.get_password(fieldname="secret_key", raise_exception=False)
				)
			}
			try:
				make_get_request(url="https://api.stripe.com/v1/charges", headers=header)
			except Exception:
				frappe.throw(_("Seems Publishable Key or Secret Key is wrong !!!"))

	def validate_transaction_currency(self, currency):
		if currency not in self.supported_currencies:
			frappe.throw(
				_(
					"Please select another payment method. Stripe does not support transactions in currency '{0}'"
				).format(currency)
			)

	def validate_minimum_transaction_amount(self, currency, amount):
		if currency in self.currency_wise_minimum_charge_amount:
			if flt(amount) < self.currency_wise_minimum_charge_amount.get(currency, 0.0):
				frappe.throw(
					_("For currency {0}, the minimum transaction amount should be {1}").format(
						currency, self.currency_wise_minimum_charge_amount.get(currency, 0.0)
					)
				)

	def get_payment_url(self, **kwargs):
		return get_url(f"./stripe_checkout?{urlencode(kwargs)}")

	def create_request(self, data):
		import stripe

		self.data = frappe._dict(data)
		stripe.api_key = self.get_password(fieldname="secret_key", raise_exception=False)
		stripe.default_http_client = stripe.http_client.RequestsClient()

		try:
			self.integration_request = create_request_log(self.data, service_name="Stripe")
			return self.create_charge_on_stripe()

		except Exception:
			# //// Neoffice — upstream: `frappe.log_error(frappe.get_traceback())` (traceback as
			# //// title, truncated at 140). See the file header.
			frappe.log_error("Error in Stripe payment processing", frappe.get_traceback())
			return {
				"redirect_to": frappe.redirect_to_message(
					_("Server Error"),
					_(
						"It seems that there is an issue with the server's stripe configuration. In case of failure, the amount will get refunded to your account."
					),
				),
				"status": 401,
			}

	def create_charge_on_stripe(self):
		import stripe

		try:
			charge = stripe.Charge.create(
				# //// Neoffice — maintenance#2296: in cents, rounded; truncated, 4.35 was charged 4.34 (stripe_minor_units).
				amount=stripe_receipt.stripe_minor_units(self.data.amount),
				currency=self.data.currency,
				source=self.data.stripe_token_id,
				description=self.data.description,
				receipt_email=self.data.payer_email,
			)

			if charge.captured is True:
				self.integration_request.db_set("status", "Completed", update_modified=False)
				self.flags.status_changed_to = "Completed"
				# //// Neoffice — the receipt: what STRIPE answered, kept on the charge's Integration Request and bound
				# //// to the Payment Request the tile named. Upstream keeps nothing a payment could be proven by
				# //// (maintenance#1807). The shop concludes a card payment on this receipt, not on who asks.
				stripe_receipt.write(self.integration_request, charge, self.data.get("payment_request_id"))

			else:
				# //// Neoffice — upstream: `frappe.log_error(charge.failure_message, "Stripe
				# //// Payment not completed")` — arguments the other way round. The swap hack only
				# //// fires when the title contains a newline, and a Stripe failure message is one
				# //// line, so the VARIABLE message became the title: a new Error Log group per
				# //// distinct decline, and the constant label buried in the body.
				frappe.log_error("Stripe Payment not completed", charge.failure_message)

		except Exception as exc:
			# //// Neoffice — a declined card is not an application error. Stripe reports a refusal
			# //// by RAISING CardError, so "insufficient funds" and "your card was declined" landed
			# //// in Error Log exactly like a broken API key would: the signature bridge then opened
			# //// a tracker issue for a customer's bank saying no (#167). Three occurrences on a
			# //// live shop in four days, all of them normal payment outcomes. Logging them as
			# //// system errors is how a real Stripe failure stops standing out.
			# //// The refusal is recorded where it belongs — on the Integration Request, which is
			# //// the log of this payment attempt — and the reason is handed to the customer, who
			# //// otherwise landed on a bare "payment-failed" page with nothing to act on. Stripe's
			# //// user_message is written to be shown; the raw exception never is.
			card_error = getattr(getattr(stripe, "error", stripe), "CardError", ())
			if card_error and isinstance(exc, card_error):
				reason = getattr(exc, "user_message", None) or str(exc)
				self.integration_request.db_set(
					"error", reason[:1000], update_modified=False
				)
				self.integration_request.db_set("status", "Failed", update_modified=False)
				self.data.redirect_message = reason
			else:
				# //// upstream: `frappe.log_error(frappe.get_traceback())` (traceback as title,
				# //// truncated at 140). See the file header.
				frappe.log_error("Error in Stripe payment processing", frappe.get_traceback())

		return self.finalize_request()

	def finalize_request(self):
		redirect_to = self.data.get("redirect_to") or None
		redirect_message = self.data.get("redirect_message") or None
		status = self.integration_request.status

		if self.flags.status_changed_to == "Completed":
			if self.data.reference_doctype and self.data.reference_docname:
				custom_redirect_to = None
				try:
					# //// Neoffice — the document named in `data` is the BROWSER's word, and `on_payment_authorized` of a
					# //// Payment Request books the request as paid in full. A charge is no payment of a request unless it
					# //// covers what the request asks (maintenance#1807). Any other reference keeps upstream's behaviour.
					if (
						self.data.reference_doctype == "Payment Request"
						and not stripe_receipt.covers(self.data.reference_docname)
					):
						frappe.log_error(
							"Stripe: a charge does not cover the request it names",
							f"payment_request={self.data.reference_docname} "
							f"integration_request={self.integration_request.name}: the request is left as it is",
						)
					else:
						# //// Neoffice — maintenance#2296: what the hook booked before it failed is undone (all_or_nothing).
						with all_or_nothing("Stripe on_payment_authorized"):
							custom_redirect_to = frappe.get_doc(
								self.data.reference_doctype, self.data.reference_docname
							).run_method("on_payment_authorized", self.flags.status_changed_to)
				except Exception:
					# //// Neoffice — upstream: `frappe.log_error(frappe.get_traceback())` (traceback as
					# //// title, truncated at 140). See the file header.
					frappe.log_error("Error in Stripe success page redirect", frappe.get_traceback())

				if custom_redirect_to:
					redirect_to = custom_redirect_to

				redirect_url = f"payment-success?doctype={self.data.reference_doctype}&docname={self.data.reference_docname}"

			if self.redirect_url:
				redirect_url = self.redirect_url
				redirect_to = None
		else:
			redirect_url = "payment-failed"

		if redirect_to:
			redirect_url += ("&" if "?" in redirect_url else "?") + urlencode({"redirect_to": redirect_to})

		if redirect_message:
			redirect_url += ("&" if "?" in redirect_url else "?") + urlencode(
				{"redirect_message": redirect_message}
			)

		return {"redirect_to": redirect_url, "status": status}


def get_gateway_controller(doctype, docname, payment_gateway=None):
	if not payment_gateway:
		# //// Neoffice — upstream loaded the document whatever it was: a guest told a name that exists (500 with
		# //// AttributeError) from one that does not (403), for any DocType, an e-mail address on User included.
		# //// Only a document that carries a gateway answers now; anything else gets the one refusal.
		require_text(doctype, docname)
		if doctype and docname and frappe.db.exists("DocType", doctype):
			if frappe.get_meta(doctype).has_field("payment_gateway"):
				payment_gateway = frappe.db.get_value(doctype, docname, "payment_gateway")
		if not payment_gateway:
			frappe.throw(_("Not permitted"), frappe.PermissionError)
	return frappe.db.get_value("Payment Gateway", payment_gateway, "gateway_controller")
