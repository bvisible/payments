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
		self.commit = self._commit.start()
		self.addCleanup(self._commit.stop)
		self.addCleanup(frappe.db.rollback)
		frappe.set_user("Administrator")  # the scene only
		frappe.db.set_global(payrexx_setup.STATUS_KEY, "{}")

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
			patch("requests.get", return_value=MagicMock(text=SITE_ONLY_PAGE, status_code=200)),
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
			patch("requests.get", return_value=MagicMock(text=ONLINE_PAGE, status_code=200)),
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

	def test_a_refused_key_is_rolled_back_not_committed(self):
		rolled = []
		with (
			patch.object(
				payrexx_setup, "test_connection", return_value={"ok": False, "error": "Unauthorized"}
			),
			patch.object(frappe.db, "rollback", side_effect=lambda *a, **k: rolled.append(1)),
			as_role("head_accountant"),
		):
			result = payrexx_setup.connect(instance="_t-shop", api_secret="_t-wrong")
		self.assertFalse(result["ok"])
		self.assertIn("nothing was changed", result["error"])
		self.assertEqual(rolled, [1], "a refused key must roll the transaction back")
		self.commit.assert_not_called()

	def test_an_unreadable_page_keeps_the_last_good_reading(self):
		good = {"checked_at": "x", "methods": ["visa"], "card": True, "twint": False}
		client = _client()
		with (
			patch("payments.drivers.payrexx._common.build_client", return_value=client),
			patch("requests.get", return_value=MagicMock(text="<html></html>", status_code=200)),
		):
			failed = payrexx_setup._probe_online()
		self.assertIn("no payment method", failed["error"])
		merged = payrexx_setup._merge_probe(good, failed)
		self.assertTrue(merged["card"])
		self.assertEqual(merged["checked_at"], "x")
		self.assertTrue(merged["error"])
		with (
			patch("payments.drivers.payrexx._common.build_client", return_value=_client()),
			patch("requests.get", return_value=MagicMock(text=ONLINE_PAGE, status_code=503)),
		):
			self.assertIn("503", payrexx_setup._probe_online()["error"])

	def test_a_test_mode_notice_keeps_the_shop_waiting(self):
		online = {"checked_at": "x", "card": True, "twint": True, "test_banner": True}
		self.assertEqual(payrexx_setup._shop_state(online), "waiting")
		self.assertFalse(payrexx_setup.shop_can_take(online, "card"))
		online["test_banner"] = False
		self.assertEqual(payrexx_setup._shop_state(online), "ok")

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
		frappe.db.set_global(
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
			tills = provider_setup.ensure_till_mode("_T Terminal", {self.company: account})["tills"]

		self.assertTrue(frappe.db.get_value("Mode of Payment", "_T Terminal", "enabled"))
		for till in tills:
			self.assertIn(
				"_T Terminal", [r.mode_of_payment for r in frappe.get_doc("POS Profile", till).payments]
			)

	def test_a_mode_the_merchant_took_off_a_till_stays_off(self):
		till = frappe.db.get_value("POS Profile", {"disabled": 0, "company": self.company}, "name")
		if not till:
			self.skipTest("no till for the default company")
		with as_role("head_accountant"):
			account = provider_setup.ensure_clearing_account(self.company, "_T clearing account")
			first = provider_setup.ensure_till_mode("_T Terminal", {self.company: account})
		self.assertTrue(first["created"])
		self.assertIn(till, first["tills"])
		profile = frappe.get_doc("POS Profile", till)
		profile.set("payments", [r for r in profile.payments if r.mode_of_payment != "_T Terminal"])
		profile.save()
		with as_role("head_accountant"):
			again = provider_setup.ensure_till_mode("_T Terminal", {self.company: account})
		self.assertFalse(again["created"])
		self.assertNotIn(till, again["tills"])

	def test_another_companys_gateway_account_is_never_the_shops(self):
		shop = provider_setup.shop_company()
		other = frappe.db.get_value("Company", {"name": ["!=", shop]}, "name")
		if not other:
			self.skipTest("a single company on this site")
		with as_role("head_accountant"):
			account = provider_setup.ensure_clearing_account(other, "_T clearing account")
			if not account:
				self.skipTest("the other company has no bank account")
			provider_setup.ensure_gateway_account("_T Gateway", other, account)
		self.assertIsNone(provider_setup.shop_gateway_account("_T Gateway"))


class TestTheHistoryNeverHoldsAKey(FrappeTestCase):
	"""Payment Provider tracks its changes; frappe leaves Code fields out of a Version, so the
	credentials never reach the history. This holds that: a change of field type would leak them."""

	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		frappe.set_user("Administrator")

	def test_a_saved_key_never_reaches_the_history(self):
		doc = frappe.get_doc(
			{
				"doctype": "Payment Provider",
				"provider_name": "_t_history",
				"display_label": "T",
				"driver_class": "payments.drivers.payrexx.provider.PayrexxProvider",
				"credentials_json": json.dumps({"instance": "_t", "api_secret": "_t-first-secret"}),
			}
		).insert()
		doc.credentials_json = json.dumps({"instance": "_t", "api_secret": "_t-second-secret"})
		doc.display_label = "T2"
		doc.save()
		for data in frappe.get_all(
			"Version", filters={"ref_doctype": "Payment Provider", "docname": doc.name}, pluck="data"
		):
			self.assertNotIn("secret", data or "")
