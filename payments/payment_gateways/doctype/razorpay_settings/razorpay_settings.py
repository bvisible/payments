# Copyright (c) 2015, Frappe Technologies and contributors
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
#//// That pass went too far at four sites, all repaired on 2026-09-04: three had
#//// lost the Razorpay API response body upstream logged (swapped for a traceback
#//// taken outside any `except`, so it named our own frame), and `create_order` had
#//// lost upstream's `frappe.throw` — `get_razorpay_order()` fell off the end and
#//// handed razorpay.js a null order instead of an error. Each site carries the
#//// upstream form it replaces.
#//// ═══════════════════════════════════════════════════════════════════════════
"""
# Integrating RazorPay

### Validate Currency

Example:

	from payments.utils import get_payment_gateway_controller

	controller = get_payment_gateway_controller("Razorpay")
	controller().validate_transaction_currency(currency)

### 2. Redirect for payment

Example:

	payment_details = {
		"amount": 600,
		"title": "Payment for bill : 111",
		"description": "payment via cart",
		"reference_doctype": "Payment Request",
		"reference_docname": "PR0001",
		"payer_email": "NuranVerkleij@example.com",
		"payer_name": "Nuran Verkleij",
		"order_id": "111",
		"currency": "INR",
		"payment_gateway": "Razorpay",
		"subscription_details": {
			"plan_id": "plan_12313", # if Required
			"start_date": "2018-08-30",
			"billing_period": "Month" #(Day, Week, Month, Year),
			"billing_frequency": 1,
			"customer_notify": 1,
			"upfront_amount": 1000
		}
	}

	# Redirect the user to this url
	url = controller().get_payment_url(**payment_details)


### 3. On Completion of Payment

Write a method for `on_payment_authorized` in the reference doctype

Example:

	def on_payment_authorized(payment_status):
		# this method will be called when payment is complete


##### Notes:

payment_status - payment gateway will put payment status on callback.
For razorpay payment status is Authorized

"""

import hashlib
import hmac
import json
from urllib.parse import urlencode

import frappe
import razorpay
from frappe import _
from frappe.integrations.utils import (
	create_request_log,
	make_get_request,
	make_post_request,
)
from frappe.model.document import Document
from frappe.utils import call_hook_method, cint, get_timestamp, get_url

from payments.utils import create_payment_gateway


class RazorpaySettings(Document):
	supported_currencies = (
		"AED",
		"ALL",
		"AMD",
		"ARS",
		"AUD",
		"AWG",
		"AZN",
		"BAM",
		"BBD",
		"BDT",
		"BGN",
		"BHD",
		"BIF",
		"BMD",
		"BND",
		"BOB",
		"BRL",
		"BSD",
		"BTN",
		"BWP",
		"BZD",
		"CAD",
		"CHF",
		"CLP",
		"CNY",
		"COP",
		"CRC",
		"CUP",
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
		"GBP",
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
		"IQD",
		"ISK",
		"JMD",
		"JOD",
		"JPY",
		"KES",
		"KGS",
		"KHR",
		"KMF",
		"KRW",
		"KWD",
		"KYD",
		"KZT",
		"LAK",
		"LKR",
		"LRD",
		"LSL",
		"MAD",
		"MDL",
		"MGA",
		"MKD",
		"MMK",
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
		"OMR",
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
		"SCR",
		"SEK",
		"SGD",
		"SLL",
		"SOS",
		"SSP",
		"SVC",
		"SZL",
		"THB",
		"TND",
		"TRY",
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
		"XAF",
		"XCD",
		"XOF",
		"XPF",
		"YER",
		"ZAR",
		"ZMW",
	)

	def init_client(self):
		if self.api_key:
			secret = self.get_password(fieldname="api_secret", raise_exception=False)
			self.client = razorpay.Client(auth=(self.api_key, secret))

	def validate(self):
		create_payment_gateway("Razorpay")
		call_hook_method("payment_gateway_enabled", gateway="Razorpay")
		if not self.flags.ignore_mandatory:
			self.validate_razorpay_credentails()

	def validate_razorpay_credentails(self):
		if self.api_key and self.api_secret:
			try:
				make_get_request(
					url="https://api.razorpay.com/v1/payments",
					auth=(
						self.api_key,
						self.get_password(fieldname="api_secret", raise_exception=False),
					),
				)
			except Exception:
				frappe.throw(_("Seems API Key or API Secret is wrong !!!"))

	def validate_transaction_currency(self, currency):
		if currency not in self.supported_currencies:
			frappe.throw(
				_(
					"Please select another payment method. Razorpay does not support transactions in currency '{0}'"
				).format(currency)
			)

	def setup_addon(self, settings, **kwargs):
		"""
		Addon template:
		{
		        "item": {
		                "name": row.upgrade_type,
		                "amount": row.amount,
		                "currency": currency,
		                "description": "add-on description"
		        },
		        "quantity": 1 (The total amount is calculated as item.amount * quantity)
		}
		"""
		url = "https://api.razorpay.com/v1/subscriptions/{}/addons".format(kwargs.get("subscription_id"))

		try:
			if not frappe.conf.converted_rupee_to_paisa:
				convert_rupee_to_paisa(**kwargs)

			for addon in kwargs.get("addons"):
				resp = make_post_request(
					url,
					auth=(settings.api_key, settings.api_secret),
					data=json.dumps(addon),
					headers={"content-type": "application/json"},
				)
				if not resp.get("id"):
					#//// Neoffice — the pair is upstream's, written positionally. Upstream:
					#//// `frappe.log_error(message=str(resp), title="Razorpay Failed while creating
					#//// subscription")`; v15 signs it `log_error(title, message)`, so spelling it out
					#//// removes any doubt about which argument is the one cut at 140 characters by
					#//// `Error Log.method` (Data). The message stays the Razorpay response body: no
					#//// exception is in flight on this branch (`if`/`else`, not `except`), so a
					#//// traceback would only name our own frame while the body is the only thing that
					#//// says WHY Razorpay refused. 7b99cbf swapped the two; restored 2026-09-04.
					frappe.log_error("Razorpay Failed while creating subscription", str(resp))
		except Exception:
			#//// Neoffice — upstream: bare `frappe.log_error()`. That works (the title falls
			#//// back to "Error" and the traceback is derived) but every failure in the file
			#//// landed in one nameless group. Named so the groups mean something.
			frappe.log_error("Error in Razorpay payment processing", frappe.get_traceback())
			# failed
			pass

	def setup_subscription(self, settings, **kwargs):
		start_date = (
			get_timestamp(kwargs.get("subscription_details").get("start_date"))
			if kwargs.get("subscription_details").get("start_date")
			else None
		)

		subscription_details = {
			"plan_id": kwargs.get("subscription_details").get("plan_id"),
			"total_count": kwargs.get("subscription_details").get("billing_frequency"),
			"customer_notify": kwargs.get("subscription_details").get("customer_notify"),
		}

		if start_date:
			subscription_details["start_at"] = cint(start_date)

		if kwargs.get("addons"):
			convert_rupee_to_paisa(**kwargs)
			subscription_details.update({"addons": kwargs.get("addons")})

		try:
			resp = make_post_request(
				"https://api.razorpay.com/v1/subscriptions",
				auth=(settings.api_key, settings.api_secret),
				data=json.dumps(subscription_details),
				headers={"content-type": "application/json"},
			)

			if resp.get("status") == "created":
				kwargs["subscription_id"] = resp.get("id")
				frappe.flags.status = "created"
				return kwargs
			else:
				#//// Neoffice — the pair is upstream's, written positionally. Upstream:
				#//// `frappe.log_error(message=str(resp), title="Razorpay Failed while creating
				#//// subscription")`; v15 signs it `log_error(title, message)`, so spelling it out
				#//// removes any doubt about which argument is the one cut at 140 characters by
				#//// `Error Log.method` (Data). The message stays the Razorpay response body: no
				#//// exception is in flight on this branch (`if`/`else`, not `except`), so a
				#//// traceback would only name our own frame while the body is the only thing that
				#//// says WHY Razorpay refused. 7b99cbf swapped the two; restored 2026-09-04.
				frappe.log_error("Razorpay Failed while creating subscription", str(resp))

		except Exception:
			#//// Neoffice — upstream: bare `frappe.log_error()`. That works (the title falls
			#//// back to "Error" and the traceback is derived) but every failure in the file
			#//// landed in one nameless group. Named so the groups mean something.
			frappe.log_error("Error in Razorpay payment processing", frappe.get_traceback())

	def prepare_subscription_details(self, settings, **kwargs):
		if not kwargs.get("subscription_id"):
			kwargs = self.setup_subscription(settings, **kwargs)

		if frappe.flags.status != "created":
			kwargs["subscription_id"] = None

		return kwargs

	def get_payment_url(self, **kwargs):
		integration_request = create_request_log(kwargs, service_name="Razorpay")
		return get_url(f"./razorpay_checkout?token={integration_request.name}")

	def create_order(self, **kwargs):
		# Creating Orders https://razorpay.com/docs/api/orders/

		# convert rupees to paisa
		kwargs["amount"] *= 100

		# Create integration log
		integration_request = create_request_log(kwargs, service_name="Razorpay")

		# Setup payment options
		payment_options = {
			"amount": kwargs.get("amount"),
			"currency": kwargs.get("currency", "INR"),
			"receipt": kwargs.get("receipt"),
			"payment_capture": kwargs.get("payment_capture"),
		}
		if self.api_key and self.api_secret:
			try:
				order = make_post_request(
					"https://api.razorpay.com/v1/orders",
					auth=(
						self.api_key,
						self.get_password(fieldname="api_secret", raise_exception=False),
					),
					data=payment_options,
				)
				order["integration_request"] = integration_request.name
				return order  # Order returned to be consumed by razorpay.js
			except Exception:
				#//// Neoffice — only the log line diverges. Upstream ran TWO statements here:
				#//// `frappe.log(frappe.get_traceback())` then the throw below. We replace the
				#//// first: `frappe.log` merely appends to `debug_log`, it never writes an Error
				#//// Log, so the traceback was lost on a real production failure. A traceback is
				#//// legitimate here — unlike the branches above, an exception IS in flight.
				#//// The throw is upstream's and must stay: without it `get_razorpay_order()` falls
				#//// off the end and hands razorpay.js a null order instead of an error, which is
				#//// exactly what dropping it in 7b99cbf caused. Restored 2026-09-04.
				frappe.log_error("Error in Razorpay payment processing", frappe.get_traceback())
				frappe.throw(_("Could not create razorpay order"))

	def create_request(self, data):
		self.data = frappe._dict(data)

		try:
			self.integration_request = frappe.get_doc("Integration Request", self.data.token)
			self.integration_request.update_status(self.data, "Queued")
			return self.authorize_payment()

		except Exception:
			#//// Neoffice — upstream: `frappe.log_error(frappe.get_traceback())` (traceback as
			#//// title, truncated at 140). See the file header.
			frappe.log_error("Error in Razorpay payment processing", frappe.get_traceback())
			return {
				"redirect_to": frappe.redirect_to_message(
					_("Server Error"),
					_(
						"Seems issue with server's razorpay config. Don't worry, in case of failure amount will get refunded to your account."
					),
				),
				"status": 401,
			}

	def authorize_payment(self):
		"""
		An authorization is performed when user’s payment details are successfully authenticated by the bank.
		The money is deducted from the customer’s account, but will not be transferred to the merchant’s account
		until it is explicitly captured by merchant.
		"""
		data = json.loads(self.integration_request.data)
		settings = self.get_settings(data)

		try:
			resp = make_get_request(
				f"https://api.razorpay.com/v1/payments/{self.data.razorpay_payment_id}",
				auth=(settings.api_key, settings.api_secret),
			)

			if resp.get("status") == "authorized":
				self.integration_request.update_status(data, "Authorized")
				self.flags.status_changed_to = "Authorized"

			elif resp.get("status") == "captured":
				self.integration_request.update_status(data, "Completed")
				self.flags.status_changed_to = "Completed"

			elif data.get("subscription_id"):
				if resp.get("status") == "refunded":
					# if subscription start date is in future then
					# razorpay refunds the amount after authorizing the card details
					# thus changing status to Verified

					self.integration_request.update_status(data, "Completed")
					self.flags.status_changed_to = "Verified"

			else:
				#//// Neoffice — the pair is upstream's, written positionally (v15 signs it
				#//// `log_error(title, message)`). Upstream: `frappe.log_error(message=str(resp),
				#//// title="Razorpay Payment not authorized")`. The title is kept verbatim because
				#//// it is what makes the entry findable, and the message stays the response body:
				#//// no exception is in flight here, so a traceback would name our own frame and
				#//// say nothing about the refusal. 7b99cbf swapped both; restored 2026-09-04.
				frappe.log_error("Razorpay Payment not authorized", str(resp))

		except Exception:
			#//// Neoffice — upstream: bare `frappe.log_error()`. That works (the title falls
			#//// back to "Error" and the traceback is derived) but every failure in the file
			#//// landed in one nameless group. Named so the groups mean something.
			frappe.log_error("Error in Razorpay payment processing", frappe.get_traceback())

		status = frappe.flags.integration_request.status_code

		redirect_to = data.get("redirect_to") or None
		redirect_message = data.get("redirect_message") or None
		if self.flags.status_changed_to in ("Authorized", "Verified", "Completed"):
			if self.data.reference_doctype and self.data.reference_docname:
				custom_redirect_to = None
				try:
					frappe.flags.data = data
					custom_redirect_to = frappe.get_doc(
						self.data.reference_doctype, self.data.reference_docname
					).run_method("on_payment_authorized", self.flags.status_changed_to)

				except Exception:
					#//// Neoffice — upstream: `frappe.log_error(frappe.get_traceback())` (traceback as
					#//// title, truncated at 140). See the file header.
					frappe.log_error("Error in Razorpay success page redirect", frappe.get_traceback())

				if custom_redirect_to:
					redirect_to = custom_redirect_to

			redirect_url = "payment-success?doctype={}&docname={}".format(
				self.data.reference_doctype, self.data.reference_docname
			)
		else:
			redirect_url = "payment-failed"

		if redirect_to:
			redirect_url += "&" + urlencode({"redirect_to": redirect_to})
		if redirect_message:
			redirect_url += "&" + urlencode({"redirect_message": redirect_message})

		return {"redirect_to": redirect_url, "status": status}

	def get_settings(self, data):
		settings = frappe._dict(
			{
				"api_key": self.api_key,
				"api_secret": self.get_password(fieldname="api_secret", raise_exception=False),
			}
		)

		if cint(data.get("notes", {}).get("use_sandbox")) or data.get("use_sandbox"):
			settings.update(
				{
					"api_key": frappe.conf.sandbox_api_key,
					"api_secret": frappe.conf.sandbox_api_secret,
				}
			)

		return settings

	def cancel_subscription(self, subscription_id):
		settings = self.get_settings({})

		try:
			resp = make_post_request(
				f"https://api.razorpay.com/v1/subscriptions/{subscription_id}/cancel",
				auth=(settings.api_key, settings.api_secret),
			)
		except Exception:
			#//// Neoffice — upstream: `frappe.log_error(frappe.get_traceback())` (traceback as
			#//// title, truncated at 140). See the file header.
			frappe.log_error("Error in Razorpay payment processing", frappe.get_traceback())

	def verify_signature(self, body, signature, key):
		key = bytes(key, "utf-8")
		body = bytes(body, "utf-8")

		dig = hmac.new(key=key, msg=body, digestmod=hashlib.sha256)

		generated_signature = dig.hexdigest()
		result = hmac.compare_digest(generated_signature, signature)

		if not result:
			frappe.throw(_("Razorpay Signature Verification Failed"), exc=frappe.PermissionError)

		return result

	@frappe.whitelist()
	def clear(self):
		self.api_key = self.api_secret = None
		self.redirect_url = None
		self.flags.ignore_mandatory = True
		self.save()


def capture_payment(is_sandbox=False, sanbox_response=None):
	"""
	Verifies the purchase as complete by the merchant.
	After capture, the amount is transferred to the merchant within T+3 days
	where T is the day on which payment is captured.

	Note: Attempting to capture a payment whose status is not authorized will produce an error.
	"""
	controller = frappe.get_doc("Razorpay Settings")

	for doc in frappe.get_all(
		"Integration Request",
		filters={"status": "Authorized", "integration_request_service": "Razorpay"},
		fields=["name", "data"],
	):
		try:
			if is_sandbox:
				resp = sanbox_response
			else:
				data = json.loads(doc.data)
				settings = controller.get_settings(data)

				resp = make_get_request(
					"https://api.razorpay.com/v1/payments/{}".format(data.get("razorpay_payment_id")),
					auth=(settings.api_key, settings.api_secret),
					data={"amount": data.get("amount")},
				)

				if resp.get("status") == "authorized":
					resp = make_post_request(
						"https://api.razorpay.com/v1/payments/{}/capture".format(data.get("razorpay_payment_id")),
						auth=(settings.api_key, settings.api_secret),
						data={"amount": data.get("amount")},
					)

			if resp.get("status") == "captured":
				frappe.db.set_value("Integration Request", doc.name, "status", "Completed")

		except Exception:
			doc = frappe.get_doc("Integration Request", doc.name)
			doc.status = "Failed"
			doc.error = frappe.get_traceback()
			doc.save()
			frappe.log_error(doc.error, f"{doc.name} Failed")


@frappe.whitelist(allow_guest=True)
def get_api_key():
	controller = frappe.get_doc("Razorpay Settings")
	return controller.api_key


@frappe.whitelist(allow_guest=True)
def get_order(doctype, docname):
	# Order returned to be consumed by razorpay.js
	doc = frappe.get_doc(doctype, docname)
	try:
		# Do not use run_method here as it fails silently
		return doc.get_razorpay_order()
	except AttributeError:
		#//// Neoffice — cosmetic only. Upstream passed `(frappe.get_traceback(),
		#//// _("Controller method get_razorpay_order missing"))`, which the swap hack in
		#//// `frappe/utils/error.py` already fixed at runtime (the title held newlines and
		#//// a message was present, so the two were exchanged). Same result, written out.
		frappe.log_error(_("Controller method get_razorpay_order missing"), frappe.get_traceback())
		frappe.throw(_("Could not create Razorpay order. Please contact Administrator"))


@frappe.whitelist(allow_guest=True)
def order_payment_success(integration_request, params):
	"""Called by razorpay.js on order payment success, the params
	contains razorpay_payment_id, razorpay_order_id, razorpay_signature
	that is updated in the data field of integration request

	Args:
	        integration_request (string): Name for integration request doc
	        params (string): Params to be updated for integration request.
	"""
	params = json.loads(params)
	integration = frappe.get_doc("Integration Request", integration_request)

	# Update integration request
	integration.update_status(params, integration.status)
	integration.reload()

	data = json.loads(integration.data)
	controller = frappe.get_doc("Razorpay Settings")

	# Update payment and integration data for payment controller object
	controller.integration_request = integration
	controller.data = frappe._dict(data)

	# Authorize payment
	controller.authorize_payment()


@frappe.whitelist(allow_guest=True)
def order_payment_failure(integration_request, params):
	"""Called by razorpay.js on failure

	Args:
	        integration_request (TYPE): Description
	        params (TYPE): error data to be updated
	"""
	#//// Neoffice — upstream: `frappe.log_error(params, "Razorpay Payment Failure")`.
	#//// `params` is the error payload and became the TITLE — truncated at 140, and a
	#//// new Error Log group per payload. TO REVIEW: our constant title is the generic
	#//// one, so the "Payment Failure" distinction upstream carried is lost.
	frappe.log_error("Error in Razorpay payment processing", params)
	params = json.loads(params)
	integration = frappe.get_doc("Integration Request", integration_request)
	integration.update_status(params, integration.status)


def convert_rupee_to_paisa(**kwargs):
	for addon in kwargs.get("addons"):
		addon["item"]["amount"] *= 100

	frappe.conf.converted_rupee_to_paisa = True


@frappe.whitelist(allow_guest=True)
def razorpay_subscription_callback():
	try:
		data = frappe.local.form_dict

		validate_payment_callback(data)

		data.update({"payment_gateway": "Razorpay"})

		doc = frappe.get_doc(
			{
				"data": json.dumps(frappe.local.form_dict),
				"doctype": "Integration Request",
				"request_description": "Subscription Notification",
				"is_remote_request": 1,
				"status": "Queued",
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()

		frappe.enqueue(
			method="payments.payment_gateways.doctype.razorpay_settings.razorpay_settings.handle_subscription_notification",
			queue="long",
			timeout=600,
			is_async=True,
			**{"doctype": "Integration Request", "docname": doc.name},
		)

	except frappe.InvalidStatusError:
		pass
	except Exception as e:
		#//// Neoffice — upstream: `frappe.log(frappe.log_error(title=e))`. Two faults in
		#//// one line: `title=e` hands an Exception object where a string title is
		#//// expected, and `frappe.log()` only appends to `debug_log` (it never writes an
		#//// Error Log) so it merely stringified the doc that log_error had just written.
		#//// TO REVIEW: `e` is now bound but unused in this except clause.
		frappe.log_error("Error in Razorpay webhook handling", frappe.get_traceback())


def validate_payment_callback(data):
	def _throw():
		frappe.throw(_("Invalid Subscription"), exc=frappe.InvalidStatusError)

	subscription_id = data.get("payload").get("subscription").get("entity").get("id")

	if not (subscription_id):
		_throw()

	controller = frappe.get_doc("Razorpay Settings")

	settings = controller.get_settings(data)

	resp = make_get_request(
		f"https://api.razorpay.com/v1/subscriptions/{subscription_id}",
		auth=(settings.api_key, settings.api_secret),
	)

	if resp.get("status") != "active":
		_throw()


def handle_subscription_notification(doctype, docname):
	call_hook_method("handle_subscription_notification", doctype=doctype, docname=docname)
