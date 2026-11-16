# //// Neoffice — added file (no upstream equivalent). A card taken for a submitted Sales Invoice — a garage's at
# //// the car's handover — becomes a Payment Entry against it, booked once (api/reconciliation._payment_entry):
# //// until 21.10.2026 a `payments` row was appended and the save refused, logged at every later save.
# Copyright (c) 2026, Neoffice and Contributors
# License: MIT. See LICENSE

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt, nowdate

from payments.api import intent as intent_api
from payments.api.reconciliation import _payment_entry, reconcile_payment_intent
from payments.tests.test_intent_api import _ensure_mock_setup


def _last_error() -> str:
	"""The last row of the Error Log: reconcile_payment_intent runs in a doc_event that logs what it fails at and never
	breaks the save, so a payment that was not booked says nothing else."""
	rows = frappe.get_all("Error Log", fields=["method", "error"], order_by="creation desc", limit=1)
	return (
		f"{rows[0].method}: {(rows[0].error or '')[-1800:]}" if rows else "nothing in the Error Log"
	)


class TestACardForASubmittedInvoice(FrappeTestCase):
	def setUp(self):
		_ensure_mock_setup()
		# the site's own company: a test company of another suite may have no fiscal year
		company = frappe.defaults.get_global_default("company")
		currency = company and frappe.get_cached_value("Company", company, "default_currency")
		# in the company's currency throughout: a Payment Entry through an account or for a customer in another currency
		# needs an exchange rate, and the test records of a CI site hold such accounts and customers with no rate.
		accounts = company and frappe.get_all(
			"Mode of Payment Account",
			filters={"default_account": ["is", "set"], "parenttype": "Mode of Payment", "company": company},
			fields=["parent", "company", "default_account"],
			order_by="parent",
		)
		self.mop = next(
			(
				row
				for row in accounts or []
				if frappe.db.get_value("Account", row.default_account, "account_currency") == currency
			),
			None,
		)
		customers = frappe.get_all(
			"Customer",
			filters={"disabled": 0},
			or_filters=[["default_currency", "is", "not set"], ["default_currency", "=", currency]]
			if currency
			else None,
			pluck="name",
			order_by="name",
			limit=1,
		)
		customer = customers[0] if customers else None
		item = frappe.db.get_value(
			"Item", {"disabled": 0, "is_stock_item": 0, "is_sales_item": 1, "has_variants": 0}, "name"
		)
		if not (self.mop and customer and item):
			self.skipTest("a mode of payment with an account, a customer and a service item are needed")
		self.si = frappe.get_doc(
			{
				"doctype": "Sales Invoice",
				"company": self.mop.company,
				"customer": customer,
				"posting_date": nowdate(),
				"due_date": nowdate(),
				"items": [{"item_code": item, "qty": 1, "rate": 100}],
			}
		)
		self.si.insert(ignore_permissions=True)
		self.si.submit()
		self.intents = []

	def tearDown(self):
		# reconcile_payment_intent commits: what the test made is taken away by hand
		for pe in frappe.get_all(
			"Payment Entry Reference", filters={"reference_name": self.si.name}, pluck="parent"
		):
			doc = frappe.get_doc("Payment Entry", pe)
			if doc.docstatus == 1:
				doc.cancel()
			frappe.delete_doc("Payment Entry", pe, force=True, ignore_permissions=True)
		for name in self.intents:
			frappe.db.delete("Payment Event", {"intent": name})
			frappe.delete_doc("Payment Intent", name, force=True, ignore_permissions=True)
		self.si.reload()
		if self.si.docstatus == 1:
			self.si.cancel()
		frappe.delete_doc("Sales Invoice", self.si.name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def _paid_by_card(self, amount, session_user=None, metadata=None):
		out = intent_api.create_intent(
			provider="mock",
			channel="terminal",
			amount=int(round(flt(amount) * 100)),
			currency=self.si.currency,
			reference_doctype="Sales Invoice",
			reference_name=self.si.name,
			metadata=metadata if metadata is not None else {"mode_of_payment": self.mop.parent},
		)
		self.intents.append(out["intent_name"])
		intent = frappe.get_doc("Payment Intent", out["intent_name"])
		if session_user:
			frappe.set_user(session_user)
		try:
			intent.transition_to("succeeded")  # saved: the doc_event reconciles it, as a webhook would
			self.session_after = frappe.session.user
		finally:
			frappe.set_user("Administrator")
		return intent

	def test_the_card_pays_the_invoice_through_a_payment_entry_once(self):
		intent = self._paid_by_card(self.si.outstanding_amount)
		pe = frappe.db.get_value(
			"Payment Entry",
			{"reference_no": intent.provider_intent_id, "docstatus": 1},
			["name", "mode_of_payment", "paid_amount"],
			as_dict=True,
		)
		self.assertTrue(pe, f"the succeeded intent booked no payment. {_last_error()}")
		self.assertEqual(pe.mode_of_payment, self.mop.parent)
		self.assertEqual(flt(pe.paid_amount), flt(self.si.outstanding_amount))
		self.assertEqual(
			flt(frappe.db.get_value("Sales Invoice", self.si.name, "outstanding_amount")), 0
		)
		# never twice: the doc_event again, and the booking itself
		self.assertEqual(reconcile_payment_intent(intent.name)["action"], "skipped_already_reconciled")
		self.assertEqual(
			_payment_entry(intent, self.si, self.mop.parent)["action"], "skipped_already_paid"
		)
		self.assertEqual(
			frappe.db.count("Payment Entry", {"reference_no": intent.provider_intent_id}), 1
		)

	def test_a_card_for_part_of_the_invoice_leaves_the_rest_to_pay(self):
		before = flt(self.si.outstanding_amount)
		self._paid_by_card(40)
		left = flt(frappe.db.get_value("Sales Invoice", self.si.name, "outstanding_amount"))
		self.assertEqual(left, flt(before - 40, 2), _last_error())

	def test_a_card_confirmed_in_a_session_without_rights_is_booked_all_the_same(self):
		# erpnext 15.124 no longer takes `ignore_permissions` in get_payment_entry: it asks the session user for the
		# right to create the entry. A webhook or a customer's browser has none, and the doc_event only logs what fails.
		intent = self._paid_by_card(self.si.outstanding_amount, session_user="Guest")
		self.assertEqual(self.session_after, "Guest", "the session is handed back as it was")
		pe = frappe.db.get_value(
			"Payment Entry", {"reference_no": intent.provider_intent_id, "docstatus": 1}, "name"
		)
		self.assertTrue(pe, f"a guest session left the card unbooked. {_last_error()}")
		self.assertEqual(
			flt(frappe.db.get_value("Sales Invoice", self.si.name, "outstanding_amount")), 0
		)

	# ---- a payment with no mode of payment, by the gateway account the customer chose (maintenance#2528) ----

	def _a_gateway_account(self, company=None):
		"""A gateway account of the invoice's company, paying into the account of the mode of payment this test uses."""
		if not frappe.db.exists("Payment Gateway", "_Test Reconciliation Gateway"):
			frappe.get_doc(
				{"doctype": "Payment Gateway", "gateway": "_Test Reconciliation Gateway"}
			).insert(ignore_permissions=True)
		account = frappe.get_doc(
			{
				"doctype": "Payment Gateway Account",
				"payment_gateway": "_Test Reconciliation Gateway",
				"payment_account": self.mop.default_account,
				"currency": frappe.db.get_value("Account", self.mop.default_account, "account_currency"),
				"company": company or self.mop.company,
			}
		).insert(ignore_permissions=True)
		self.addCleanup(self._forget_the_gateway_account, account.name)
		return account

	@staticmethod
	def _forget_the_gateway_account(name):
		frappe.delete_doc("Payment Gateway Account", name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def _an_accountant(self):
		"""A desk user with the accounting role; told in the desk only, no mail sent from a test."""
		from frappe.desk.doctype.notification_settings.notification_settings import (
			create_notification_settings,
		)

		email = "_t-accountant-2747@example.com"
		if not frappe.db.exists("User", email):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": email,
					"first_name": "_T Accountant",
					"user_type": "System User",
					"send_welcome_email": 0,
					"roles": [{"role": "Accounts User"}],
				}
			).insert(ignore_permissions=True)
		create_notification_settings(email)
		frappe.db.set_value("Notification Settings", email, "enable_email_notifications", 0)
		self.addCleanup(self._forget_the_accountant, email)
		return email

	@staticmethod
	def _forget_the_gateway(name):
		frappe.delete_doc("Payment Gateway", name, force=True, ignore_permissions=True)
		frappe.db.commit()

	@staticmethod
	def _forget_the_accountant(email):
		frappe.db.delete("Notification Log", {"for_user": email})
		frappe.delete_doc("User", email, force=True, ignore_permissions=True)
		frappe.db.commit()

	def _forget_what_was_put_back(self, company, accounts_before, gateway):
		"""After tearDown has taken the payment and the invoice away: the account made again, the notes, the notices."""
		for account in (
			set(frappe.get_all("Account", filters={"company": company}, pluck="name")) - accounts_before
		):
			frappe.db.delete("Comment", {"reference_doctype": "Account", "reference_name": account})
			frappe.delete_doc("Account", account, force=True, ignore_permissions=True)
		frappe.db.delete(
			"Comment", {"reference_doctype": "Payment Gateway Account", "reference_name": gateway}
		)
		frappe.db.delete(
			"Notification Log", {"document_type": "Sales Invoice", "document_name": self.si.name}
		)
		frappe.db.commit()

	def test_a_gateway_whose_clearing_account_went_missing_has_it_put_back_and_the_accountants_are_told(
		self,
	):  # role-check: ok the provider's webhook books the payment as the system, no person acts
		# neoffice-maintenance#2747: on one site the account a gateway booked to was removed outside frappe; the payment
		# was refused and the invoice stayed unpaid, with one line in the Error Log.
		from payments.setup import provider_setup

		accountant = self._an_accountant()
		company = self.mop.company
		currency = frappe.db.get_value("Account", self.mop.default_account, "account_currency")
		gone = (
			frappe.get_doc(
				{
					"doctype": "Account",
					"account_name": "_T Wallee clearing 2747",
					"parent_account": frappe.db.get_value(
						"Account", self.mop.default_account, "parent_account"
					),
					"company": company,
					"account_type": "Bank",
					"account_currency": currency,
					"is_group": 0,
				}
			)
			.insert(ignore_permissions=True)
			.name
		)
		provider_setup.ensure_gateway("_T Wallee 2747")
		self.addCleanup(self._forget_the_gateway, "_T Wallee 2747")
		gateway = frappe.get_doc(
			{
				"doctype": "Payment Gateway Account",
				"payment_gateway": "_T Wallee 2747",
				"payment_account": gone,
				"currency": currency,
				"company": company,
			}
		).insert(ignore_permissions=True)
		self.addCleanup(self._forget_the_gateway_account, gateway.name)
		# removed outside frappe: no trash, the link left as it was
		frappe.db.delete("Account", {"name": gone})
		accounts_before = set(frappe.get_all("Account", filters={"company": company}, pluck="name"))
		self.addCleanup(self._forget_what_was_put_back, company, accounts_before, gateway.name)
		frappe.db.commit()
		owed = flt(self.si.outstanding_amount)

		intent = self._paid_by_card(owed, metadata={"gateway_account": gateway.name})

		pe = frappe.db.get_value(
			"Payment Entry",
			{"reference_no": intent.provider_intent_id, "docstatus": 1},
			["name", "paid_to", "paid_amount"],
			as_dict=True,
		)
		self.assertTrue(pe, f"the payment was not booked. {_last_error()}")
		self.assertTrue(frappe.db.exists("Account", pe.paid_to))
		self.assertEqual(
			frappe.db.get_value("Payment Gateway Account", gateway.name, "payment_account"), pe.paid_to
		)
		self.assertEqual(flt(pe.paid_amount), owed)
		self.assertEqual(flt(frappe.db.get_value("Sales Invoice", self.si.name, "outstanding_amount")), 0)
		told = frappe.get_all(
			"Notification Log",
			filters={"for_user": accountant, "document_type": "Sales Invoice", "document_name": self.si.name},
			pluck="subject",
		)
		self.assertEqual(len(told), 1, "the accountant was not told")
		self.assertIn(pe.paid_to, told[0])
		self.assertIn(self.si.name, told[0])
		said = frappe.get_all(
			"Comment",
			filters={"reference_doctype": "Payment Gateway Account", "reference_name": gateway.name},
			pluck="content",
		)
		self.assertTrue(any(gone in (text or "") for text in said), said)

	def test_with_no_mode_of_payment_the_gateway_account_the_customer_chose_takes_the_money(self):
		account = self._a_gateway_account()
		owed = flt(self.si.outstanding_amount)
		intent = self._paid_by_card(owed, metadata={"gateway_account": account.name})
		pe = frappe.db.get_value(
			"Payment Entry",
			{"reference_no": intent.provider_intent_id, "docstatus": 1},
			["name", "paid_to", "paid_amount"],
			as_dict=True,
		)
		self.assertTrue(pe, f"the succeeded intent booked no payment. {_last_error()}")
		self.assertEqual(
			pe.paid_to, self.mop.default_account, "through the account of the gateway the customer chose"
		)
		self.assertEqual(flt(pe.paid_amount), owed)
		self.assertEqual(
			flt(frappe.db.get_value("Sales Invoice", self.si.name, "outstanding_amount")), 0
		)

	def test_a_gateway_account_that_is_not_the_invoices_companys_is_no_reason_to_book_anything(self):
		other = frappe.db.get_value("Company", {"name": ["!=", self.mop.company]}, "name")
		if not other:
			self.skipTest("a second company is needed")
		account = self._a_gateway_account(company=other)
		intent = self._paid_by_card(
			self.si.outstanding_amount, metadata={"gateway_account": account.name}
		)
		self.assertEqual(
			frappe.db.count("Payment Entry", {"reference_no": intent.provider_intent_id}), 0
		)
		intent.reload()
		self.assertEqual(
			intent.get_metadata().get("reconciliation_action"), "skipped_no_mode_of_payment"
		)

	def test_an_unknown_gateway_account_or_none_books_nothing_as_before(self):
		for metadata in ({"gateway_account": "No such gateway - CHF"}, {}):
			intent = self._paid_by_card(self.si.outstanding_amount, metadata=metadata)
			self.assertEqual(
				frappe.db.count("Payment Entry", {"reference_no": intent.provider_intent_id}), 0
			)
