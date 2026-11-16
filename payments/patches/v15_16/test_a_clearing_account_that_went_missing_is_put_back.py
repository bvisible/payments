# //// Neoffice — added file (no upstream equivalent). See put_back_the_clearing_accounts_that_went_missing.py.
import os

import frappe
from frappe.tests.utils import FrappeTestCase

from payments.patches.v15_16.test_each_provider_books_to_its_own_clearing_account import (
	_a_company_with_a_bank_and_no_gateway,
)
from payments.setup import clearing_accounts, provider_setup

GATEWAY = "Payment Gateway Account"
PATCH = "payments.patches.v15_16.put_back_the_clearing_accounts_that_went_missing"


class TestAClearingAccountThatWentMissingIsPutBack(FrappeTestCase):
	"""osiris, 16.11 (neoffice-maintenance#2747): Payrexx's and Wallee's accounts were removed outside frappe after
	the separation made them; their gateways and till modes still booked to names that no longer existed."""

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

	def a_till_mode_of(self, provider, mode):
		"""A till mode its provider's setup page made, put back as it was afterwards."""
		key = clearing_accounts.SETUP_STATES[provider]
		before = frappe.db.get_global(key)
		frappe.db.set_global(key, frappe.as_json({"till_mode": mode}))
		if before is None:
			self.addCleanup(frappe.defaults.clear_default, key, parent="__global")
		else:
			self.addCleanup(frappe.db.set_global, key, before)
		return mode

	def booked(self, gateway):
		return frappe.db.get_value(GATEWAY, gateway, "payment_account")

	def mode_account(self, mode):
		return frappe.db.get_value(
			"Mode of Payment Account", {"parent": mode, "company": self.company}, "default_account"
		)

	def removed_outside_frappe(self, account):
		"""As on osiris: the row is gone, nothing in Deleted Document, links left as they were."""
		frappe.db.delete("Account", {"name": account})

	def test_a_provider_account_removed_outside_frappe_is_put_back_and_its_gateway_and_till_mode_follow(
		self,
	):  # role-check: ok a patch runs as Administrator inside bench migrate
		payrexx_account = self.account("Payrexx card clearing")
		payrexx = self.gateway("Payrexx", payrexx_account)
		terminal = self.mode(self.a_till_mode_of("Payrexx", "_T Terminal Payrexx"), payrexx_account)
		self.removed_outside_frappe(payrexx_account)
		self.assertEqual(len(clearing_accounts.missing(self.company)), 2)

		reports = clearing_accounts.put_back(only=[self.company])

		target = self.booked(payrexx)
		self.assertTrue(frappe.db.exists("Account", target), "the gateway still books to a missing account")
		self.assertEqual(
			frappe.db.get_value("Account", target, "account_name"), clearing_accounts.label("Payrexx")
		)
		self.assertEqual(frappe.db.get_value("Account", target, "company"), self.company)
		self.assertEqual(self.mode_account(terminal), target)
		self.assertEqual(reports[0]["providers"]["Payrexx"]["target"], target)
		said = frappe.get_all(
			"Comment",
			filters={"reference_doctype": GATEWAY, "reference_name": payrexx},
			pluck="content",
		)
		self.assertTrue(any(payrexx_account in (text or "") for text in said), said)
		self.assertEqual(clearing_accounts.missing(self.company), [])

	def test_a_second_run_changes_nothing(
		self,
	):  # role-check: ok a patch runs as Administrator inside bench migrate
		wallee_account = self.account("Wallee clearing")
		wallee = self.gateway("Wallee", wallee_account)
		self.removed_outside_frappe(wallee_account)
		clearing_accounts.put_back(only=[self.company])
		after = self.booked(wallee)
		accounts = frappe.db.count("Account", {"company": self.company})

		self.assertEqual(clearing_accounts.put_back(only=[self.company]), [])

		self.assertEqual(self.booked(wallee), after)
		self.assertEqual(frappe.db.count("Account", {"company": self.company}), accounts)

	def test_where_every_account_exists_nothing_is_touched(
		self,
	):  # role-check: ok a patch runs as Administrator inside bench migrate
		stripe_account = self.account("Stripe clearing")
		stripe = self.gateway("Stripe", stripe_account)
		accounts = frappe.db.count("Account", {"company": self.company})

		self.assertEqual(clearing_accounts.put_back(only=[self.company]), [])

		self.assertEqual(self.booked(stripe), stripe_account)
		self.assertEqual(frappe.db.count("Account", {"company": self.company}), accounts)

	def test_a_till_mode_that_names_no_provider_is_left_and_named(
		self,
	):  # role-check: ok a patch runs as Administrator inside bench migrate
		cards_account = self.account("Cards clearing")
		cards = self.mode("_T Carte de crédit", cards_account)
		self.removed_outside_frappe(cards_account)
		accounts = frappe.db.count("Account", {"company": self.company})

		reports = clearing_accounts.put_back(only=[self.company])

		self.assertEqual([row["name"] for row in reports[0]["left"]], [cards])
		self.assertEqual(self.mode_account(cards), cards_account)
		self.assertEqual(frappe.db.count("Account", {"company": self.company}), accounts)

	def test_a_dry_run_says_what_it_would_put_back_and_writes_nothing(
		self,
	):  # role-check: ok a patch runs as Administrator inside bench migrate
		payrexx_account = self.account("Payrexx clearing")
		payrexx = self.gateway("Payrexx", payrexx_account)
		self.removed_outside_frappe(payrexx_account)

		reports = clearing_accounts.put_back(dry_run=True, only=[self.company])

		self.assertEqual([m["name"] for m in reports[0]["providers"]["Payrexx"]["moves"]], ["Payrexx"])
		self.assertEqual(self.booked(payrexx), payrexx_account)

	def test_the_patch_is_listed_with_its_module(self):
		path = frappe.get_app_path("payments", "patches.txt")
		with open(path) as handle:
			self.assertIn(PATCH, [line.strip() for line in handle])
		module = frappe.get_module(PATCH)
		self.assertTrue(callable(getattr(module, "execute", None)))
		self.assertTrue(os.path.exists(module.__file__))
