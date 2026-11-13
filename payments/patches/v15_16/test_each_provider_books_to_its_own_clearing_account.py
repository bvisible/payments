# //// Neoffice — added file (no upstream equivalent). See give_each_payment_provider_its_own_clearing_account.py.
import json
import os
from unittest.mock import patch

import frappe
from frappe.tests.roles import as_role
from frappe.tests.utils import FrappeTestCase

from payments.setup import clearing_accounts, provider_setup

GATEWAY = "Payment Gateway Account"
PATCH = "payments.patches.v15_16.give_each_payment_provider_its_own_clearing_account"


def _a_company_with_a_bank_and_no_gateway():
	"""A company whose chart has a bank account and that has no payment gateway yet.

	The one with the most clearing accounts first: a Swiss chart (1030 card, 1036 TWINT…) is
	what the numbering of a new clearing account is about.
	"""
	with_gateway = set(frappe.get_all(GATEWAY, pluck="company"))
	best, most = (None, None), -1
	for company in frappe.get_all("Company", pluck="name", order_by="creation"):
		if company in with_gateway:
			continue
		bank = frappe.db.get_value(
			"Account",
			{"company": company, "account_type": "Bank", "is_group": 0, "disabled": 0},
			"parent_account",
		)
		if not bank:
			continue
		clearing = sum(
			frappe.db.count(
				"Account",
				{
					"company": company,
					"account_type": "Bank",
					"is_group": 0,
					"account_name": ["like", f"%{hint}%"],
				},
			)
			for hint in provider_setup._ANCHOR_HINTS
		)
		if clearing > most:
			best, most = (company, bank), clearing
	return best


class TestEachProviderBooksToItsOwnClearingAccount(FrappeTestCase):
	"""Payrexx, Wallee and Stripe on one account are separated; an account of its own is left alone."""

	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		frappe.set_user("Administrator")
		self.company, self.parent = _a_company_with_a_bank_and_no_gateway()
		if not self.company:
			self.skipTest("no company with a bank account and no gateway on this site")
		self.currency = frappe.get_cached_value("Company", self.company, "default_currency")

	def account(self, name):
		return (
			frappe.get_doc(
				{
					"doctype": "Account",
					"account_name": f"_T {name}",
					"parent_account": self.parent,
					"company": self.company,
					"account_type": "Bank",
					"account_currency": self.currency,
					"is_group": 0,
				}
			)
			.insert()
			.name
		)

	def gateway(self, gateway, account):
		provider_setup.ensure_gateway(gateway)
		return (
			frappe.get_doc(
				{
					"doctype": GATEWAY,
					"payment_gateway": gateway,
					"company": self.company,
					"payment_account": account,
				}
			)
			.insert()
			.name
		)

	def mode(self, mode, account):
		frappe.get_doc(
			{
				"doctype": "Mode of Payment",
				"mode_of_payment": mode,
				"type": "Bank",
				"enabled": 1,
				"accounts": [{"company": self.company, "default_account": account}],
			}
		).insert()
		return mode

	def booked(self, gateway):
		return frappe.db.get_value(GATEWAY, gateway, "payment_account")

	def mode_account(self, mode):
		return frappe.db.get_value(
			"Mode of Payment Account", {"parent": mode, "company": self.company}, "default_account"
		)

	def separate(self):
		return clearing_accounts.separate(only=[self.company])

	def a_setup_state(self, key, till_mode):
		"""A setup page's till mode, put back as it was afterwards (the defaults are cached)."""
		before = frappe.db.get_global(key)
		frappe.db.set_global(key, json.dumps({"till_mode": till_mode}))
		if before is None:
			self.addCleanup(frappe.defaults.clear_default, key, parent="__global")
		else:
			self.addCleanup(frappe.db.set_global, key, before)

	def test_two_providers_on_another_providers_account_are_given_their_own(
		self,
	):  # role-check: ok a patch runs as Administrator inside bench migrate
		stripes = self.account("Stripe card clearing")
		stripe = self.gateway("Stripe", stripes)
		payrexx = self.gateway("Payrexx", stripes)
		wallee = self.gateway("Wallee", stripes)
		entries = frappe.db.count("GL Entry", {"account": stripes})

		self.separate()

		self.assertEqual(self.booked(stripe), stripes)
		for gateway, provider in ((payrexx, "Payrexx"), (wallee, "Wallee")):
			account = self.booked(gateway)
			self.assertNotEqual(account, stripes)
			row = frappe.db.get_value(
				"Account", account, ["account_name", "account_type", "company", "is_group"], as_dict=True
			)
			self.assertEqual(clearing_accounts.providers_named(row.account_name), {provider})
			self.assertEqual((row.account_type, row.company, row.is_group), ("Bank", self.company, 0))
			self.assertTrue(
				frappe.db.exists(
					"Comment", {"reference_doctype": GATEWAY, "reference_name": gateway, "comment_type": "Info"}
				)
			)
		self.assertNotEqual(self.booked(payrexx), self.booked(wallee))
		# Nothing already booked moves
		self.assertEqual(frappe.db.count("GL Entry", {"account": stripes}), entries)

	def test_the_note_on_a_moved_gateway_is_in_the_language_of_the_site(
		self,
	):  # role-check: ok a patch runs as Administrator inside bench migrate
		# bench migrate runs in English whatever the site speaks: the bookkeeper reads the site's language
		lang = frappe.db.get_single_value("System Settings", "language") or "en"
		source = "Clearing account changed from {0} to {1}: each payment provider books to an account of its own."
		if frappe._(source, lang=lang) == source:
			self.skipTest("the note has no translation in the language of this site")
		was = frappe.local.lang
		frappe.local.lang = "en"
		self.addCleanup(setattr, frappe.local, "lang", was)
		stripes = self.account("Stripe card clearing")
		self.gateway("Stripe", stripes)
		payrexx = self.gateway("Payrexx", stripes)

		self.separate()

		note = frappe.db.get_value(
			"Comment", {"reference_doctype": GATEWAY, "reference_name": payrexx}, "content"
		)
		self.assertEqual(note, frappe._(source, lang=lang).format(stripes, self.booked(payrexx)))

	def test_a_provider_on_an_account_of_its_own_is_left_where_it_is(
		self,
	):  # role-check: ok a patch runs as Administrator inside bench migrate
		own = self.account("own clearing")
		payrexx = self.gateway("Payrexx", own)
		stripes = self.account("Stripe card clearing")
		self.gateway("Stripe", stripes)

		reports = self.separate()

		self.assertEqual(self.booked(payrexx), own)
		self.assertFalse([r for r in reports if r["providers"].get("Payrexx", {}).get("moves")])
		self.assertFalse(
			frappe.db.exists("Comment", {"reference_doctype": GATEWAY, "reference_name": payrexx})
		)

	def test_a_company_with_no_bank_account_gets_no_account_and_nothing_moves(
		self,
	):  # role-check: ok a patch runs as Administrator inside bench migrate
		stripes = self.account("Stripe card clearing")
		self.gateway("Stripe", stripes)
		payrexx = self.gateway("Payrexx", stripes)
		accounts = frappe.db.count("Account", {"company": self.company})

		with patch.object(provider_setup, "clearing_slot", return_value=None):
			reports = self.separate()

		self.assertEqual(self.booked(payrexx), stripes)
		self.assertEqual(frappe.db.count("Account", {"company": self.company}), accounts)
		self.assertEqual(reports[0]["blocked"], ["Payrexx"])

	def test_the_till_modes_of_a_provider_follow_it_and_a_bank_terminal_stays(
		self,
	):  # role-check: ok a patch runs as Administrator inside bench migrate
		cards = self.account("card clearing")
		terminal = self.mode("Payrexx _T Terminal", cards)
		bank_terminal = self.mode("_T Card terminal", cards)
		self.a_setup_state("payrexx_setup_status", terminal)
		payrexx = self.gateway("Payrexx", self.account("Stripe card clearing"))

		self.separate()

		self.assertEqual(self.mode_account(terminal), self.booked(payrexx))
		self.assertEqual(
			clearing_accounts.providers_named(
				frappe.db.get_value("Account", self.booked(payrexx), "account_name")
			),
			{"Payrexx"},
		)
		self.assertEqual(self.mode_account(bank_terminal), cards)

	def test_a_second_run_changes_nothing(
		self,
	):  # role-check: ok a patch runs as Administrator inside bench migrate
		stripes = self.account("Stripe card clearing")
		self.gateway("Stripe", stripes)
		payrexx = self.gateway("Payrexx", stripes)
		self.separate()
		after = self.booked(payrexx)
		accounts = frappe.db.count("Account", {"company": self.company})

		self.assertEqual(self.separate(), [])

		self.assertEqual(self.booked(payrexx), after)
		self.assertEqual(frappe.db.count("Account", {"company": self.company}), accounts)

	def test_a_gateway_wired_to_another_providers_account_is_given_its_own_on_connect(self):
		stripes = self.account("Stripe card clearing")
		self.gateway("Stripe", stripes)
		self.gateway("Payrexx", stripes)

		with as_role("head_accountant"):
			account = provider_setup.ensure_clearing_account(
				self.company, "_T Payrexx clearing", gateway="Payrexx"
			)

		self.assertNotEqual(account, stripes)
		self.assertEqual(frappe.db.get_value("Account", account, "account_name"), "_T Payrexx clearing")

	def test_a_new_clearing_account_takes_the_first_free_number_beside_the_others(self):
		numbers = sorted(
			int(n)
			for hint in provider_setup._ANCHOR_HINTS
			for n in frappe.get_all(
				"Account",
				filters={
					"company": self.company,
					"account_type": "Bank",
					"is_group": 0,
					"account_name": ["like", f"%{hint}%"],
				},
				pluck="account_number",
			)
			if str(n or "").isdigit()
		)
		if len(numbers) < 2:
			self.skipTest("the chart of this company has fewer than two numbered clearing accounts")
		used = set(frappe.get_all("Account", filters={"company": self.company}, pluck="account_number"))
		expected = next(str(n) for n in range(numbers[0] + 1, numbers[0] + 60) if str(n) not in used)

		with as_role("head_accountant"):
			account = provider_setup.ensure_clearing_account(self.company, "_T clearing account")

		self.assertEqual(frappe.db.get_value("Account", account, "account_number"), expected)

	def test_the_patch_is_listed_with_its_module(self):
		path = frappe.get_app_path("payments", "patches.txt")
		with open(path) as handle:
			self.assertIn(PATCH, [line.strip() for line in handle])
		module = frappe.get_module(PATCH)
		self.assertTrue(callable(getattr(module, "execute", None)))
		self.assertTrue(os.path.exists(module.__file__))
