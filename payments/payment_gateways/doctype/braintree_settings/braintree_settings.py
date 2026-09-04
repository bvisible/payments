# Copyright (c) 2018, Frappe Technologies and contributors
# License: MIT. See LICENSE

#//// ═══════════════════════════════════════════════════════════════════════════
#//// Neoffice — every `frappe.log_error(...)` call in this file is ours (7b99cbf,
#//// 2025-02-28 "update error log and stripe version"). Nothing else in the file
#//// diverges from upstream.
#////
#//// Why: Frappe v15 signs it `log_error(title, message)` and writes the title to
#//// `Error Log.method` — a **Data** field, so cut at 140 characters — while the
#//// body goes to `error` (Code, unbounded). Upstream calls it here with the
#//// traceback as the ONLY argument: the traceback lands in the title and is
#//// truncated, and the swap hack in `frappe/utils/error.py` cannot rescue it
#//// because that only fires when a message is passed too. A constant title also
#//// groups the entries, instead of one Error Log group per distinct message.
#////
#//// Each call site below carries the upstream form it replaces. Sites marked
#//// TO REVIEW change behaviour, not just the log line — read them before merging.
#//// ═══════════════════════════════════════════════════════════════════════════
from urllib.parse import urlencode

import braintree
import frappe
from frappe import _
from frappe.integrations.utils import create_request_log
from frappe.model.document import Document
from frappe.utils import call_hook_method, get_url

from payments.utils import create_payment_gateway


class BraintreeSettings(Document):
	supported_currencies = [
		"AED",
		"AMD",
		"AOA",
		"ARS",
		"AUD",
		"AWG",
		"AZN",
		"BAM",
		"BBD",
		"BDT",
		"BGN",
		"BIF",
		"BMD",
		"BND",
		"BOB",
		"BRL",
		"BSD",
		"BWP",
		"BYN",
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
		"GEL",
		"GHS",
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
		"KGS",
		"KHR",
		"KMF",
		"KRW",
		"KYD",
		"KZT",
		"LAK",
		"LBP",
		"LKR",
		"LRD",
		"LSL",
		"LTL",
		"MAD",
		"MDL",
		"MKD",
		"MNT",
		"MOP",
		"MUR",
		"MVR",
		"MWK",
		"MXN",
		"MYR",
		"MZN",
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
		"RON",
		"RSD",
		"RUB",
		"RWF",
		"SAR",
		"SBD",
		"SCR",
		"SEK",
		"SGD",
		"SHP",
		"SLL",
		"SOS",
		"SRD",
		"STD",
		"SVC",
		"SYP",
		"SZL",
		"THB",
		"TJS",
		"TOP",
		"TRY",
		"TTD",
		"TWD",
		"TZS",
		"UAH",
		"UGX",
		"USD",
		"UYU",
		"UZS",
		"VEF",
		"VND",
		"VUV",
		"WST",
		"XAF",
		"XCD",
		"XOF",
		"XPF",
		"YER",
		"ZAR",
		"ZMK",
		"ZWD",
	]

	def validate(self):
		if not self.flags.ignore_mandatory:
			self.configure_braintree()

	def on_update(self):
		create_payment_gateway(
			"Braintree-" + self.gateway_name,
			settings="Braintree Settings",
			controller=self.gateway_name,
		)
		call_hook_method("payment_gateway_enabled", gateway="Braintree-" + self.gateway_name)

	def configure_braintree(self):
		if self.use_sandbox:
			environment = "sandbox"
		else:
			environment = "production"

		braintree.Configuration.configure(
			environment=environment,
			merchant_id=self.merchant_id,
			public_key=self.public_key,
			private_key=self.get_password(fieldname="private_key", raise_exception=False),
		)

	def validate_transaction_currency(self, currency):
		if currency not in self.supported_currencies:
			frappe.throw(
				_(
					"Please select another payment method. Stripe does not support transactions in currency '{0}'"
				).format(currency)
			)

	def get_payment_url(self, **kwargs):
		return get_url(f"./braintree_checkout?{urlencode(kwargs)}")

	def create_payment_request(self, data):
		self.data = frappe._dict(data)

		try:
			self.integration_request = create_request_log(self.data, service_name="Braintree")
			return self.create_charge_on_braintree()

		except Exception:
			#//// Neoffice — upstream: `frappe.log_error(frappe.get_traceback())` (traceback as
			#//// title, truncated at 140). See the file header.
			frappe.log_error("Error in Braintree payment request", frappe.get_traceback())
			return {
				"redirect_to": frappe.redirect_to_message(
					_("Server Error"),
					_(
						"There seems to be an issue with the server's braintree configuration. Don't worry, in case of failure, the amount will get refunded to your account."
					),
				),
				"status": 401,
			}

	def create_charge_on_braintree(self):
		self.configure_braintree()

		redirect_to = self.data.get("redirect_to") or None
		redirect_message = self.data.get("redirect_message") or None

		result = braintree.Transaction.sale(
			{
				"amount": self.data.amount,
				"payment_method_nonce": self.data.payload_nonce,
				"options": {"submit_for_settlement": True},
			}
		)

		if result.is_success:
			self.integration_request.db_set("status", "Completed", update_modified=False)
			self.flags.status_changed_to = "Completed"
			self.integration_request.db_set("output", result.transaction.status, update_modified=False)

		elif result.transaction:
			self.integration_request.db_set("status", "Failed", update_modified=False)
			error_log = frappe.log_error(
				"code: "
				+ str(result.transaction.processor_response_code)
				+ " | text: "
				+ str(result.transaction.processor_response_text),
				"Braintree Payment Error",
			)
			self.integration_request.db_set("error", error_log.error, update_modified=False)
		else:
			self.integration_request.db_set("status", "Failed", update_modified=False)
			for error in result.errors.deep_errors:
				error_log = frappe.log_error(
					"code: " + str(error.code) + " | message: " + str(error.message),
					"Braintree Payment Error",
				)
				self.integration_request.db_set("error", error_log.error, update_modified=False)

		if self.flags.status_changed_to == "Completed":
			status = "Completed"
			if self.data.reference_doctype and self.data.reference_docname:
				custom_redirect_to = None
				try:
					custom_redirect_to = frappe.get_doc(
						self.data.reference_doctype, self.data.reference_docname
					).run_method("on_payment_authorized", self.flags.status_changed_to)
					braintree_success_page = frappe.get_hooks("braintree_success_page")
					if braintree_success_page:
						custom_redirect_to = frappe.get_attr(braintree_success_page[-1])(self.data)
				except Exception:
					#//// Neoffice — upstream: `frappe.log_error(frappe.get_traceback())` (traceback as
					#//// title, truncated at 140). See the file header.
					frappe.log_error("Error in Braintree webhook handling", frappe.get_traceback())

				if custom_redirect_to:
					redirect_to = custom_redirect_to

			redirect_url = "payment-success"
		else:
			status = "Error"
			redirect_url = "payment-failed"

		get_parameters = [
			("doctype", self.data.reference_doctype),
			("docname", self.data.reference_docname),
		]

		if redirect_to:
			get_parameters.append(("redirect_to", redirect_to))
		if redirect_message:
			get_parameters.append(("redirect_message", redirect_message))

		redirect_url += "?" + urlencode(get_parameters)
		return {"redirect_to": redirect_url, "status": status}


def get_gateway_controller(doc):
	payment_request = frappe.get_doc("Payment Request", doc)
	return frappe.db.get_value(
		"Payment Gateway", payment_request.payment_gateway, "gateway_controller"
	)


def get_client_token(doc):
	gateway_controller = get_gateway_controller(doc)
	settings = frappe.get_doc("Braintree Settings", gateway_controller)
	settings.configure_braintree()

	return braintree.ClientToken.generate()
