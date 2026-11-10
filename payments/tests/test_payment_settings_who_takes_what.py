# //// Neoffice — added file (no upstream equivalent). « Payment settings » (2026-11-10): one provider per
# //// payment means on the shop, chosen by the merchant; a provider that cannot take it yet cannot be
# //// chosen; going back restores the row exactly; a till mode is switched for every till.
"""Who takes what: the shop's tile per payment means, and the till's modes."""

import json
from unittest.mock import patch

import frappe
from frappe.tests.roles import as_role
from frappe.tests.utils import FrappeTestCase

from payments.api import payment_settings, payrexx_setup


class TestPaymentSettings(FrappeTestCase):
	def setUp(self):
		if not frappe.db.exists("DocType", "Webshop Settings"):
			self.skipTest("webshop is not installed")
		if not frappe.db.exists("Payment Provider", payrexx_setup.PROVIDER):
			self.skipTest("no Payrexx provider on this site")
		self._commit = patch.object(frappe.db, "commit")
		self._commit.start()
		self.addCleanup(self._commit.stop)
		self.addCleanup(frappe.db.rollback)
		frappe.set_user("Administrator")  # the scene only
		frappe.db.set_global(payrexx_setup.STATUS_KEY, "{}")
		frappe.db.set_global(payment_settings.PARKED_KEY, "{}")
		frappe.db.set_global(payment_settings.PARKED_TILL_KEY, "{}")
		with as_role("head_accountant"):
			payrexx_setup._prepare_erp()

	def shop_accounts(self):
		return [r.payment_gateway_account for r in frappe.get_doc("Webshop Settings").payment_methods]

	def open_online(self, card=True, twint=True):
		status = payrexx_setup._status()
		status["online"] = {"checked_at": "x", "card": card, "twint": twint, "methods": []}
		frappe.db.set_global(payrexx_setup.STATUS_KEY, json.dumps(status))

	def test_payrexx_cannot_be_chosen_before_it_opens_online_sales(self):
		before = self.shop_accounts()
		with as_role("head_accountant"), self.assertRaises(frappe.ValidationError):
			payment_settings.set_shop_method(method="card", provider="payrexx")
		self.assertEqual(self.shop_accounts(), before)

	def test_choosing_payrexx_for_card_swaps_the_card_tile_and_going_back_restores_it(self):
		self.open_online()
		settings = frappe.get_doc("Webshop Settings")
		stripe = payment_settings._stripe_accounts()
		original = None
		if stripe:
			row = next((r for r in settings.payment_methods if r.payment_gateway_account in stripe), None)
			if not row:
				row = settings.append(
					"payment_methods", {"payment_gateway_account": stripe[0], "render_inline": 0}
				)
				settings.save(ignore_permissions=True)
			original = {
				k: row.get(k) for k in ("payment_gateway_account", "settlement", "only_customer_group")
			}

		with as_role("head_accountant"):
			overview = payment_settings.set_shop_method(method="card", provider="payrexx")

		card_account = payrexx_setup._shop_gateway_account("Payrexx Carte")
		self.assertIn(card_account, self.shop_accounts())
		self.assertEqual(overview["shop"]["card"]["current"], "payrexx")
		if not stripe:
			return
		self.assertFalse(set(stripe) & set(self.shop_accounts()), "one card tile, never two")

		with as_role("head_accountant"):
			payment_settings.set_shop_method(method="card", provider="stripe")

		self.assertNotIn(card_account, self.shop_accounts())
		back = next(
			r
			for r in frappe.get_doc("Webshop Settings").payment_methods
			if r.payment_gateway_account in stripe
		)
		self.assertEqual({k: back.get(k) for k in original}, original)

	def test_none_takes_the_means_off_the_shop(self):
		self.open_online()
		with as_role("head_accountant"):
			payment_settings.set_shop_method(method="twint", provider="payrexx")
			payment_settings.set_shop_method(method="twint", provider="none")
		self.assertNotIn(payrexx_setup._shop_gateway_account("Payrexx TWINT"), self.shop_accounts())

	def test_a_till_mode_is_switched_for_every_till(self):
		mode = payrexx_setup._status()["till_mode"]
		tills = frappe.get_all("POS Profile", filters={"disabled": 0}, pluck="name")
		with as_role("head_accountant"):
			payment_settings.set_till_mode(mode=mode, on=0)
		for till in tills:
			self.assertNotIn(
				mode, [r.mode_of_payment for r in frappe.get_doc("POS Profile", till).payments]
			)
		with as_role("head_accountant"):
			overview = payment_settings.set_till_mode(mode=mode, on=1)
		states = {m["mode"]: m["state"] for m in overview["till"]["card"]}
		self.assertIn(states.get(mode), ("all", "some"))

	def test_the_shop_is_never_left_with_no_way_to_pay(self):
		self.open_online()
		settings = frappe.get_doc("Webshop Settings")
		card = payrexx_setup._shop_gateway_account("Payrexx Carte")
		settings.set("payment_methods", [{"payment_gateway_account": card}])
		settings.save(ignore_permissions=True)
		with as_role("head_accountant"), self.assertRaises(frappe.ValidationError):
			payment_settings.set_shop_method(method="card", provider="none")
		self.assertEqual(self.shop_accounts(), [card], "the refusal leaves the shop as it was")

	def test_a_test_mode_notice_keeps_payrexx_off_the_shop(self):
		status = payrexx_setup._status()
		status["online"] = {"checked_at": "x", "card": True, "twint": True, "test_banner": True}
		frappe.db.set_global(payrexx_setup.STATUS_KEY, json.dumps(status))
		card = next(o for o in payment_settings._shop_options()["card"] if o["key"] == "payrexx")
		self.assertFalse(card["available"])
		self.assertIn("test", card["reason"])

	def test_every_row_of_a_parked_account_comes_back(self):
		self.open_online()
		stripe = payment_settings._stripe_accounts()
		if not stripe:
			self.skipTest("no Stripe account on this site")
		groups = frappe.get_all("Customer Group", filters={"is_group": 0}, pluck="name", limit=2)
		if len(groups) < 2:
			self.skipTest("fewer than two customer groups")
		settings = frappe.get_doc("Webshop Settings")
		settings.set(
			"payment_methods",
			[r for r in settings.payment_methods if r.payment_gateway_account not in stripe],
		)
		for group in groups:
			settings.append(
				"payment_methods", {"payment_gateway_account": stripe[0], "only_customer_group": group}
			)
		settings.save(ignore_permissions=True)

		with as_role("head_accountant"):
			payment_settings.set_shop_method(method="card", provider="payrexx")
			payment_settings.set_shop_method(method="card", provider="stripe")

		back = sorted(
			r.only_customer_group
			for r in frappe.get_doc("Webshop Settings").payment_methods
			if r.payment_gateway_account == stripe[0]
		)
		self.assertEqual(back, sorted(groups))

	def test_a_till_row_comes_back_with_its_settings(self):
		mode = payrexx_setup._status()["till_mode"]
		till = frappe.db.get_value("POS Profile", {"disabled": 0}, "name")
		profile = frappe.get_doc("POS Profile", till)
		row = next((r for r in profile.payments if r.mode_of_payment == mode), None)
		if not row:
			self.skipTest("the till mode is not on this till")
		row.allow_in_returns = 1
		profile.save(ignore_permissions=True)
		with as_role("head_accountant"):
			payment_settings.set_till_mode(mode=mode, on=0)
			payment_settings.set_till_mode(mode=mode, on=1)
		back = next(r for r in frappe.get_doc("POS Profile", till).payments if r.mode_of_payment == mode)
		self.assertEqual(back.allow_in_returns, 1)

	def test_cash_is_not_a_providers_mode(self):
		modes = {m["mode"] for ms in payment_settings._till().values() for m in ms}
		for cash in frappe.get_all("Mode of Payment", filters={"type": "Cash"}, pluck="name"):
			self.assertNotIn(cash, modes)
