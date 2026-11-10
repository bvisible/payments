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
		frappe.db.set_default(payrexx_setup.STATUS_KEY, "{}")
		frappe.db.set_default(payment_settings.PARKED_KEY, "{}")
		with as_role("head_accountant"):
			payrexx_setup._prepare_erp()

	def shop_accounts(self):
		return [r.payment_gateway_account for r in frappe.get_doc("Webshop Settings").payment_methods]

	def open_online(self, card=True, twint=True):
		status = payrexx_setup._status()
		status["online"] = {"checked_at": "x", "card": card, "twint": twint, "methods": []}
		frappe.db.set_default(payrexx_setup.STATUS_KEY, json.dumps(status))

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
