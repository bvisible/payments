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
		self.mop = company and frappe.db.get_value(
			"Mode of Payment Account",
			{"default_account": ["is", "set"], "parenttype": "Mode of Payment", "company": company},
			["parent", "company"],
			as_dict=True,
		)
		customer = frappe.db.get_value("Customer", {"disabled": 0}, "name")
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

	def _paid_by_card(self, amount):
		out = intent_api.create_intent(
			provider="mock",
			channel="terminal",
			amount=int(round(flt(amount) * 100)),
			currency=self.si.currency,
			reference_doctype="Sales Invoice",
			reference_name=self.si.name,
			metadata={"mode_of_payment": self.mop.parent},
		)
		self.intents.append(out["intent_name"])
		intent = frappe.get_doc("Payment Intent", out["intent_name"])
		intent.transition_to("succeeded")  # saved: the doc_event reconciles it, as a webhook would
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
