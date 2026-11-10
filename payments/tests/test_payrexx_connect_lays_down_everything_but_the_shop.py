# //// Neoffice — added file (no upstream equivalent). The one-gesture Payrexx setup (2026-11-10):
# //// « Connect » lays down the clearing account, the gateways, the channels and the till mode, reads
# //// the account off a test payment page it deletes at once, and changes nothing in the shop.
"""Payrexx « Connect »: everything the ERP needs is laid down, and the shop is left alone."""

import json
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.roles import as_role
from frappe.tests.utils import FrappeTestCase

from payments.api import payrexx_setup
from payments.setup import provider_setup

ONLINE_PAGE = (
	'<img alt="visa"><img alt="mastercard"><img alt="twint"><img alt="Visa">'
	'<img alt="pre_payment"><div>Secure connection</div>'
)
SITE_ONLY_PAGE = (
	'<div>Some payment methods are in test mode.</div><img alt="pre_payment"><img alt="Mastercard">'
)


class _Gateway:
	id = 4242
	link = "https://shop.payrexx.com/en/?payment=x"


def _client():
	client = MagicMock()
	client.__enter__.return_value = client
	client.__exit__.return_value = False
	client.gateway.create.return_value = _Gateway()
	return client


class TestPayrexxConnect(FrappeTestCase):
	def setUp(self):
		self._commit = patch.object(frappe.db, "commit")
		self._commit.start()
		self.addCleanup(self._commit.stop)
		self.addCleanup(frappe.db.rollback)
		frappe.set_user("Administrator")  # the scene only
		frappe.db.set_default(payrexx_setup.STATUS_KEY, "{}")

	def shop_rows(self):
		if not frappe.db.exists("DocType", "Webshop Settings"):
			return []
		return [r.payment_gateway_account for r in frappe.get_doc("Webshop Settings").payment_methods]

	def test_connect_prepares_the_erp_and_leaves_the_shop_as_it_was(self):
		before = self.shop_rows()
		client = _client()
		with (
			patch.object(
				payrexx_setup,
				"test_connection",
				return_value={
					"ok": True,
					"instance": "_t-shop",
					"providers": ["Payrexx Pay"],
					"payment_methods": ["visa"],
				},
			),
			patch("payments.drivers.payrexx._common.build_client", return_value=client),
			patch("requests.get", return_value=MagicMock(text=SITE_ONLY_PAGE)),
			as_role("head_accountant"),
		):
			result = payrexx_setup.connect(instance="https://_t-shop.payrexx.com/", api_secret="_t-secret")

		self.assertTrue(result["ok"])
		self.assertEqual(self.shop_rows(), before, "nothing may reach the shop on its own")
		states = {row["key"]: row["state"] for row in result["rows"]}
		self.assertEqual(states["accounting"], "ok")
		self.assertEqual(states["till"], "ok")
		self.assertEqual(states["shop"], "waiting")
		self.assertEqual(result["mode"], "mixed")
		client.gateway.delete.assert_called_once_with(4242)

		till_mode = payrexx_setup._status()["till_mode"]
		self.assertTrue(frappe.db.get_value("Mode of Payment", till_mode, "enabled"))
		for spec in payrexx_setup.payment_tiles().values():
			self.assertTrue(
				frappe.db.exists("Payment Gateway", payrexx_setup._gateway_name(spec["suffix"]))
			)

	def test_the_test_page_says_what_a_shopper_is_offered(self):
		client = _client()
		with (
			patch("payments.drivers.payrexx._common.build_client", return_value=client),
			patch("requests.get", return_value=MagicMock(text=ONLINE_PAGE)),
		):
			online = payrexx_setup._probe_online()

		self.assertEqual(online["methods"], ["mastercard", "pre_payment", "twint", "visa"])
		self.assertTrue(online["card"])
		self.assertTrue(online["twint"])
		self.assertFalse(online["test_banner"])
		client.gateway.delete.assert_called_once_with(4242)

	def test_the_test_page_is_deleted_even_when_reading_it_fails(self):
		client = _client()
		with (
			patch("payments.drivers.payrexx._common.build_client", return_value=client),
			patch("requests.get", side_effect=OSError("network down")),
		):
			online = payrexx_setup._probe_online()

		self.assertIn("network down", online["error"])
		client.gateway.delete.assert_called_once_with(4242)

	def test_the_mode_reads_as_the_merchant_must_read_it(self):
		mode = payrexx_setup._mode_state
		pay = {"providers": ["Payrexx Pay"]}
		self.assertEqual(mode(pay, {}), "unknown")
		self.assertEqual(mode(pay, {"checked_at": "x", "card": True, "twint": True}), "live")
		self.assertEqual(mode(pay, {"checked_at": "x", "test_banner": True}), "mixed")
		self.assertEqual(mode({}, {"checked_at": "x", "test_banner": True}), "test")

	def test_the_morning_check_writes_once_when_payrexx_opens_the_shop(self):
		if not frappe.db.exists("Payment Provider", payrexx_setup.PROVIDER):
			self.skipTest("no Payrexx provider on this site")
		frappe.db.set_value("Payment Provider", payrexx_setup.PROVIDER, "enabled", 1)
		frappe.db.set_default(
			payrexx_setup.STATUS_KEY,
			json.dumps({"connected_by": "Administrator", "online": {"checked_at": "x", "card": False}}),
		)
		opened = {"checked_at": "y", "methods": ["visa", "mastercard"], "card": True, "twint": False}
		with (
			patch.object(payrexx_setup, "_probe_online", return_value=opened),
			patch("frappe.sendmail") as sendmail,
		):
			payrexx_setup.daily_online_check()
			payrexx_setup.daily_online_check()

		self.assertEqual(sendmail.call_count, 1)
		self.assertEqual(sendmail.call_args.kwargs["recipients"], ["Administrator"])

	def test_a_terminal_is_not_linked_without_instant_confirmations(self):
		if not frappe.db.exists("Payment Provider", payrexx_setup.PROVIDER):
			self.skipTest("no Payrexx provider on this site")
		frappe.db.set_value(
			"Payment Provider",
			payrexx_setup.PROVIDER,
			"credentials_json",
			json.dumps({"instance": "_t", "api_secret": "_t"}),
		)
		profile = frappe.db.get_value("POS Profile", {"disabled": 0}, "name")
		if not profile:
			self.skipTest("no till on this site")
		with as_role("head_accountant"), self.assertRaises(frappe.ValidationError):
			payrexx_setup.link_terminal(
				serial_number="N860TEST", pairing_code="ABC123", pos_profile=profile
			)


class TestProviderSetup(FrappeTestCase):
	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		frappe.set_user("Administrator")
		self.company = frappe.defaults.get_global_default("company")

	def test_a_clearing_account_goes_beside_the_others_once(self):
		with as_role("head_accountant"):
			first = provider_setup.ensure_clearing_account(self.company, "_T clearing account")
			again = provider_setup.ensure_clearing_account(self.company, "_T clearing account")

		self.assertEqual(first, again)
		account = frappe.db.get_value(
			"Account", first, ["parent_account", "account_type", "is_group"], as_dict=True
		)
		self.assertEqual(account.account_type, "Bank")
		self.assertFalse(account.is_group)
		twint_parent = frappe.db.get_value(
			"Account",
			{"company": self.company, "account_name": ["like", "%Twint%"], "is_group": 0},
			"parent_account",
		)
		if twint_parent:
			self.assertEqual(account.parent_account, twint_parent)

	def test_a_till_mode_reaches_every_till_and_stays_enabled(self):
		with as_role("head_accountant"):
			account = provider_setup.ensure_clearing_account(self.company, "_T clearing account")
			tills = provider_setup.ensure_till_mode("_T Terminal", {self.company: account})

		self.assertTrue(frappe.db.get_value("Mode of Payment", "_T Terminal", "enabled"))
		for till in tills:
			self.assertIn(
				"_T Terminal", [r.mode_of_payment for r in frappe.get_doc("POS Profile", till).payments]
			)
