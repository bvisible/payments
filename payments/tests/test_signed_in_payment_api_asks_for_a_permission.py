# //// Neoffice — added file (no upstream equivalent). maintenance#1732: the signed-in payment API asks for a right.
"""A signed-in account reaches a payment only with a right to it.

Before maintenance#1732 any signed-in account, a shop customer included, could create, read, cancel or refund any
Payment Intent by its name over HTTP, push it to a reader, read or print its receipt, poll or abandon it through the
phone's calls, mark it paid with the TWINT simulator, and drive the Stripe Terminal setup. The functions the checkouts
call in Python stay unchecked: a guest still pays.
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from payments.api import intent as intent_api
from payments.api import intent_http, mobile_payments, terminal, terminal_receipt
from payments.api import twint as twint_api
from payments.api.intent_access import web_access_token
from payments.payments.page.payment_terminal_wizard import payment_terminal_wizard as wizard
from payments.tests.test_intent_api import _ensure_mock_setup

PORTAL = "intentguard-portal@yopmail.com"
DESK = "intentguard-desk@yopmail.com"
MISSING = "PI-DOES-NOT-EXIST-1732"
DOORS = {
	"payments.api.intent.create_intent": "payments.api.intent_http.create_intent",
	"payments.api.intent.get_intent_status": "payments.api.intent_http.get_intent_status",
	"payments.api.intent.cancel_intent": "payments.api.intent_http.cancel_intent",
	"payments.api.intent.refund_intent": "payments.api.intent_http.refund_intent",
	"payments.api.terminal.attach_intent_to_reader": "payments.api.intent_http.attach_intent_to_reader",
}


def _user(email, roles, user_type):
	if frappe.db.exists("User", email):
		frappe.delete_doc("User", email, ignore_permissions=True, force=True)
	frappe.get_doc(
		{
			"doctype": "User",
			"email": email,
			"first_name": email.split("@")[0],
			"send_welcome_email": 0,
			"roles": [{"role": r} for r in roles if frappe.db.exists("Role", r)],
		}
	).insert(ignore_permissions=True)
	frappe.db.set_value("User", email, "user_type", user_type)
	frappe.clear_cache(user=email)


def _intent(channel):
	"""A bare open intent on a channel, written as a pivot would (no PSP call)."""
	if not frappe.db.exists("Payment Channel", channel):
		frappe.get_doc(
			{"doctype": "Payment Channel", "channel_code": channel, "display_label": channel}
		).insert(ignore_permissions=True)
	return (
		frappe.get_doc(
			{
				"doctype": "Payment Intent",
				"provider": "mock",
				"channel": channel,
				"amount": 500,
				"currency": "CHF",
				"status": "requires_action",
			}
		)
		.insert(ignore_permissions=True)
		.name
	)


def _door_calls(name):
	return [
		(
			"create_intent",
			lambda: intent_http.create_intent(
				provider="mock", channel="terminal", amount=100, currency="CHF"
			),
		),
		("get_intent_status", lambda: intent_http.get_intent_status(name)),
		("cancel_intent", lambda: intent_http.cancel_intent(name)),
		("refund_intent", lambda: intent_http.refund_intent(name)),
		("attach_intent_to_reader", lambda: intent_http.attach_intent_to_reader(name, "any-reader")),
		("print_receipt", lambda: terminal_receipt.print_receipt(name)),
		("preview_text", lambda: terminal_receipt.preview_text(name)),
		("preview_receipt", lambda: terminal_receipt.preview_receipt(name)),
		("mobile_refresh_status", lambda: mobile_payments.mobile_refresh_status(name)),
		("mobile_abandon_payment", lambda: mobile_payments.mobile_abandon_payment(name)),
		("send_receipt", lambda: mobile_payments.send_receipt(name, "intentguard-other@yopmail.com")),
		("mobile_receipt", lambda: mobile_payments.mobile_receipt(name)),
	]


SETUP_CALLS = [
	("list_stripe_locations", lambda: terminal.list_stripe_locations()),
	(
		"create_stripe_location",
		lambda: terminal.create_stripe_location("Shop", "CH", "Rue 1", "Sion", "1950"),
	),
	("ensure_stripe_webhook", lambda: terminal.ensure_stripe_webhook()),
	("register_stripe_reader", lambda: terminal.register_stripe_reader("simulated-wpe", "tml_x")),
	("list_stripe_readers", lambda: terminal.list_stripe_readers()),
	("list_terminal_providers", lambda: wizard.list_terminal_providers()),
	("list_pos_profiles", lambda: wizard.list_pos_profiles()),
	(
		"link_device_to_pos_profile",
		lambda: wizard.link_device_to_pos_profile("any-reader", "Any POS Profile", "Cash"),
	),
	(
		"list_modes_of_payment_for_profile",
		lambda: wizard.list_modes_of_payment_for_profile("Any POS Profile"),
	),
]


class TestSignedInPaymentApiAsksForAPermission(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_mock_setup()

	def setUp(self):
		frappe.set_user("Administrator")
		_user(PORTAL, ["Customer"], "Website User")
		_user(DESK, ["Blogger"], "System User")
		self.till_intent = intent_api.create_intent(
			provider="mock", channel="terminal", amount=500, currency="CHF"
		)["intent_name"]

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def test_the_http_paths_answer_through_the_doors(self):
		for path, door in DOORS.items():
			with self.subTest(path=path):
				self.assertEqual(frappe.override_whitelisted_method(path), door)

	def test_the_python_api_is_no_http_entry(self):
		# A lost override fails closed (not whitelisted), never open.
		for function in (
			intent_api.create_intent,
			intent_api.get_intent_status,
			intent_api.cancel_intent,
			intent_api.refund_intent,
			terminal.attach_intent_to_reader,
		):
			with self.subTest(function=function.__name__):
				self.assertNotIn(function, frappe.allowed_http_methods_for_whitelisted_func)

	def test_a_portal_account_is_refused_on_an_intent_and_on_a_name_that_does_not_exist(self):
		frappe.set_user(PORTAL)
		for name in (self.till_intent, MISSING):
			for label, call in _door_calls(name):
				with self.subTest(call=label, name=name):
					with self.assertRaises(frappe.PermissionError):
						call()

	def test_a_desk_account_without_the_right_is_refused(self):
		frappe.set_user(DESK)
		for label, call in _door_calls(self.till_intent):
			with self.subTest(call=label):
				with self.assertRaises(frappe.PermissionError):
					call()

	def test_the_terminal_setup_answers_only_the_wizards_roles(self):
		for user in (PORTAL, DESK):
			frappe.set_user(user)
			for label, call in SETUP_CALLS:
				with self.subTest(call=label, user=user):
					with self.assertRaises(frappe.PermissionError):
						call()

	def test_the_administrator_still_reads_and_cancels(self):
		self.assertEqual(
			intent_http.get_intent_status(self.till_intent)["intent_name"], self.till_intent
		)
		self.assertEqual(intent_http.cancel_intent(self.till_intent)["status"], "canceled")
		with self.assertRaises(frappe.DoesNotExistError):
			intent_http.get_intent_status(MISSING)

	def test_a_guest_checkout_still_pays_through_the_python_api(self):
		frappe.set_user("Guest")
		created = intent_api.create_intent(
			provider="mock", channel="terminal", amount=700, currency="CHF"
		)
		self.assertEqual(created["status"], "requires_action")
		self.assertEqual(intent_api.cancel_intent(created["intent_name"])["status"], "canceled")

	def test_the_phone_follows_only_what_a_phone_started(self):
		collected = _intent(mobile_payments.CARD_CHANNEL)
		frappe.set_user(DESK)
		mobile_payments._require_phone_intent(collected, "write")
		with self.assertRaises(frappe.PermissionError):
			mobile_payments._require_phone_intent(self.till_intent, "read")
		frappe.set_user(PORTAL)
		with self.assertRaises(frappe.PermissionError):
			mobile_payments._require_phone_intent(collected, "read")
		with self.assertRaises(frappe.PermissionError):
			mobile_payments.connection_token()

	def test_the_twint_simulator_needs_the_dialogs_token(self):
		web = _intent("twint_web")
		had = frappe.local.conf.get("enable_e2e_simulators")
		frappe.local.conf["enable_e2e_simulators"] = 1
		try:
			frappe.set_user(PORTAL)
			for label, call in (
				("no token", lambda: twint_api.simulate_consumer_success(web)),
				("forged token", lambda: twint_api.simulate_consumer_success(web, access_token="0" * 40)),
				(
					"another channel's intent",
					lambda: twint_api.simulate_consumer_success(
						self.till_intent, access_token=web_access_token(self.till_intent)
					),
				),
			):
				with self.subTest(call=label):
					with self.assertRaises(frappe.PermissionError):
						call()
		finally:
			if had is None:
				frappe.local.conf.pop("enable_e2e_simulators", None)
			else:
				frappe.local.conf["enable_e2e_simulators"] = had


class TestTheWalleeReturnPagesServeOnlyTheBrowserSentBack(FrappeTestCase):
	"""/wallee/success and /wallee/failed showed, refreshed and finalised any intent named in their URL."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_mock_setup()

	def setUp(self):
		frappe.set_user("Administrator")
		self.web = _intent("wallee_web")
		self.till = _intent("terminal")
		frappe.set_user("Guest")

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.form_dict.clear()
		frappe.db.rollback()

	def _render(self, page, intent_name, token):
		from unittest.mock import patch

		from payments.www.wallee import success

		frappe.form_dict.clear()
		frappe.form_dict.update({"payment_intent": intent_name, "access_token": token})
		context = frappe._dict()
		with patch.object(success, "_refresh_status") as refresh:
			page.get_context(context)
		return context, refresh

	def test_the_return_urls_carry_the_intents_token(self):
		from urllib.parse import parse_qs, urlsplit

		from payments.drivers.wallee.web_driver import _with_access_token

		shop = _with_access_token(
			"https://shop.example/wallee/success?payment_request=ACC-PRQ-1", "PI-1"
		)
		query = parse_qs(urlsplit(shop).query)
		self.assertEqual(query["payment_request"], ["ACC-PRQ-1"])
		self.assertEqual(query["payment_intent"], ["PI-1"])
		self.assertEqual(query["access_token"], [web_access_token("PI-1")])
		own_page = "https://shop.example/guest/order/ok?token=abc"
		self.assertEqual(_with_access_token(own_page, "PI-1"), own_page)

	def test_a_guest_naming_an_intent_learns_nothing_and_moves_nothing(self):
		from payments.www.wallee import failed, success

		for page in (success, failed):
			for name, token in (
				(self.web, None),
				(self.web, "0" * 40),
				(self.till, web_access_token(self.till)),
				(MISSING, web_access_token(MISSING)),
			):
				with self.subTest(page=page.__name__, name=name, token=token):
					context, refresh = self._render(page, name, token)
					self.assertIsNone(context.intent)
					refresh.assert_not_called()

	def test_the_buyer_sent_back_with_its_token_sees_its_payment(self):
		from payments.www.wallee import failed, success

		for page in (success, failed):
			with self.subTest(page=page.__name__):
				context, refresh = self._render(page, self.web, web_access_token(self.web))
				self.assertEqual(context.intent.name, self.web)
				refresh.assert_called_once()
