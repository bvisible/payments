# //// Neoffice — added file (no upstream equivalent). Tests of the reset helper of the
# //// end-to-end suite (`payments.tests.e2e.fixtures.reset_test_env`), which used to leave
# //// submitted invoices citing orders it had deleted, and, once the naming series was reused,
# //// orders of other customers (#778). The first two classes need no site; the last one needs
# //// a CLONE of the development instance (real company, chart of accounts, price lists),
# //// never the instance itself.
# Copyright (c) 2026, Neoffice and Contributors
# License: MIT. See LICENSE
"""The reset helper cancels what was submitted, deletes only drafts, and touches one customer only.

Measured on a clone of the development instance (2026-09-29): Frappe refuses to cancel a Sales
Order that a submitted invoice or a submitted Payment Request still links to (``LinkExistsError``),
but it has already written docstatus 2 to the row when it raises. The helper swallowed the error
and deleted the half-cancelled order with ``force=True``: the order vanished, the invoice kept
citing its name, and the naming series, reverted because the order was the last one, handed that
name to another customer's order.
"""

from __future__ import annotations

import unittest
import uuid
from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, nowdate

from payments.tests.e2e import fixtures


def _document(docstatus: int) -> mock.MagicMock:
	document = mock.MagicMock()
	document.docstatus = docstatus
	return document


def _reset_test_env() -> dict:
	"""`frappe.whitelist` wraps the helper in an argument type check that reads
	`frappe.local.flags`, which a test without a site does not have: call the wrapped function."""
	return getattr(fixtures.reset_test_env, "__wrapped__", fixtures.reset_test_env)()


class TestSafeCancelAndDelete(unittest.TestCase):
	"""One document at a time, with Frappe replaced by stand-ins: no site needed."""

	def setUp(self):
		self.db = mock.MagicMock()
		self.delete_doc = mock.MagicMock()
		self.log_error = mock.MagicMock()
		for patcher in (
			mock.patch.object(fixtures.frappe, "db", self.db),
			mock.patch.object(fixtures.frappe, "delete_doc", self.delete_doc),
			mock.patch.object(fixtures.frappe, "log_error", self.log_error),
			mock.patch.object(fixtures.frappe, "get_traceback", return_value="the traceback"),
		):
			patcher.start()
			self.addCleanup(patcher.stop)

	def _run(self, document, doctype="Sales Order", name="SO-0001"):
		with mock.patch.object(fixtures.frappe, "get_doc", return_value=document):
			return fixtures._safe_cancel_and_delete(doctype, name)

	def test_a_draft_is_deleted(self):
		document = _document(0)
		self.assertTrue(self._run(document))
		self.delete_doc.assert_called_once_with(
			"Sales Order", "SO-0001", force=True, ignore_permissions=True
		)
		document.cancel.assert_not_called()

	def test_a_submitted_document_is_cancelled_and_kept(self):
		document = _document(1)
		self.assertTrue(self._run(document))
		document.cancel.assert_called_once_with()
		self.delete_doc.assert_not_called()

	def test_a_cancelled_document_is_left_alone(self):
		document = _document(2)
		self.assertFalse(self._run(document))
		document.cancel.assert_not_called()
		self.delete_doc.assert_not_called()
		self.db.savepoint.assert_not_called()

	def test_a_cancel_that_fails_is_rolled_back_logged_and_never_followed_by_a_delete(self):
		document = _document(1)
		document.cancel.side_effect = frappe.LinkExistsError("still linked")

		self.assertFalse(self._run(document))

		self.db.rollback.assert_called_once_with(save_point=fixtures._RESET_SAVEPOINT)
		self.delete_doc.assert_not_called()
		title, message = self.log_error.call_args.args
		self.assertLessEqual(len(title), 140, "an Error Log title is limited to 140 characters")
		self.assertIn("SO-0001", title)
		self.assertEqual(message, "the traceback")

	def test_a_delete_that_fails_is_rolled_back_and_logged_too(self):
		self.delete_doc.side_effect = frappe.LinkExistsError("still linked")
		self.assertFalse(self._run(_document(0), doctype="Payment Intent", name="PI-0001"))
		self.db.rollback.assert_called_once_with(save_point=fixtures._RESET_SAVEPOINT)
		self.log_error.assert_called_once()

	def test_a_document_that_is_already_gone_is_not_an_error(self):
		with mock.patch.object(fixtures.frappe, "get_doc", side_effect=frappe.DoesNotExistError):
			self.assertFalse(fixtures._safe_cancel_and_delete("Sales Order", "SO-0001"))
		self.log_error.assert_not_called()

	def test_a_successful_step_releases_its_savepoint(self):
		self._run(_document(1))
		self.db.savepoint.assert_called_once_with(fixtures._RESET_SAVEPOINT)
		self.db.release_savepoint.assert_called_once_with(fixtures._RESET_SAVEPOINT)
		self.db.rollback.assert_not_called()


class TestResetOrder(unittest.TestCase):
	"""What `reset_test_env` asks for, and in which order, with faked documents: no site needed."""

	def setUp(self):
		self.calls: list[tuple[str, str]] = []
		self.cited: set[str] = set()
		self.db = mock.MagicMock()
		self.db.exists.side_effect = self._exists

		def get_all(doctype, filters=None, fields=None, pluck=None, order_by=None, **_):
			rows = {
				"Payment Entry": ["ACC-PAY-1"],
				"Payment Request": ["ACC-PRQ-1"],
				"Payment Intent": [],
				"Sales Invoice": ["FA-1"],
				"Sales Order": [("BC-1", 1), ("BC-2", 0)],
				"Quotation": ["QTN-1"],
			}.get(doctype, [])
			if doctype == "Payment Intent" and (filters or {}).get("reference_name") == "ACC-PRQ-1":
				rows = ["PI-1"]
			if doctype == "Sales Order" and not pluck:
				return [{"name": name, "docstatus": docstatus} for name, docstatus in rows]
			return rows

		for patcher in (
			mock.patch.object(fixtures.frappe, "db", self.db),
			mock.patch.object(fixtures.frappe, "get_all", side_effect=get_all),
			mock.patch.object(fixtures, "_cfg", return_value="Test Customer"),
			mock.patch.object(fixtures, "_safe_cancel_and_delete", side_effect=self._record),
			# //// Neoffice — the helper now asks who calls it (#952): a System Manager on a site that
			# //// switched the simulators on. This class tests the ORDER of the work, not that guard
			# //// (tests/test_e2e_guard.py does), and it has no site to ask: the check is a no-op here.
			mock.patch.object(fixtures, "e2e_only"),
		):
			patcher.start()
			self.addCleanup(patcher.stop)

	def _record(self, doctype, name):
		self.calls.append((doctype, name))
		return True

	def _exists(self, doctype, filters=None):
		if doctype == "Sales Invoice Item":
			return (filters or {}).get("sales_order") in self.cited
		return True

	def test_the_documents_are_handled_in_the_order_of_their_links(self):
		_reset_test_env()
		order = [doctype for doctype, _name in self.calls]
		self.assertEqual(
			order,
			[
				"Payment Entry",
				"Payment Intent",
				"Payment Request",
				"Sales Invoice",
				"Sales Order",
				"Sales Order",
				"Quotation",
			],
		)

	def test_the_invoices_of_the_test_customer_are_handled_at_all(self):
		stats = _reset_test_env()
		self.assertIn(("Sales Invoice", "FA-1"), self.calls)
		self.assertEqual(stats["sales_invoices"], 1)

	def test_an_order_an_invoice_still_cites_is_left_and_counted(self):
		self.cited = {"BC-1"}
		stats = _reset_test_env()
		self.assertNotIn(("Sales Order", "BC-1"), self.calls)
		self.assertIn(("Sales Order", "BC-2"), self.calls)
		self.assertEqual(stats["skipped_linked"], 1)
		self.assertEqual(stats["sales_orders"], 1)

	def test_a_submitted_order_only_waits_for_a_submitted_invoice_but_a_draft_for_any(self):
		_reset_test_env()
		exists_calls = [
			call.args for call in self.db.exists.call_args_list if call.args[0] == "Sales Invoice Item"
		]
		self.assertIn(("Sales Invoice Item", {"sales_order": "BC-1", "docstatus": 1}), exists_calls)
		self.assertIn(("Sales Invoice Item", {"sales_order": "BC-2"}), exists_calls)

	def test_the_work_is_committed_once_at_the_end(self):
		_reset_test_env()
		self.db.commit.assert_called_once_with()


class TestResetOnARealSite(FrappeTestCase):
	"""The real thing, on a clone of the development instance. Everything is rolled back at the end.

	`reset_test_env` commits, which would make the test's own documents permanent: the commit is
	replaced by a no-op for the duration of each test, and the whole transaction is rolled back.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if "erpnext" not in frappe.get_installed_apps():
			raise unittest.SkipTest("ERPNext is not installed on this site")
		cls.company = frappe.db.get_single_value(
			"Global Defaults", "default_company"
		) or frappe.db.get_value("Company", {}, "name")
		if not cls.company:
			raise unittest.SkipTest("no company on this site: run it on a clone of the dev instance")
		currency = frappe.db.get_value("Company", cls.company, "default_currency")
		cls.price_list = frappe.db.get_value(
			"Price List", {"selling": 1, "enabled": 1, "currency": currency}, "name"
		)
		if not cls.price_list:
			raise unittest.SkipTest("this site has no selling price list in the company currency")

	def setUp(self):
		commit = mock.patch.object(frappe.db, "commit")
		commit.start()
		self.addCleanup(commit.stop)
		self.addCleanup(frappe.db.rollback)

		self.tag = uuid.uuid4().hex[:6]
		self.customer = self._customer("reset test customer")
		self.other_customer = self._customer("reset test bystander")
		self.item = self._item()
		# //// Neoffice — the helper answers only on a site that switched the simulators on (#952).
		config = mock.patch.dict(
			frappe.conf, {"e2e_test_customer": self.customer, "enable_e2e_simulators": 1}
		)
		config.start()
		self.addCleanup(config.stop)

	# -- builders -------------------------------------------------------------------------------

	def _leaf(self, doctype: str) -> str:
		return frappe.db.get_value(doctype, {"is_group": 0}, "name")

	def _customer(self, label: str) -> str:
		return (
			frappe.get_doc(
				{
					"doctype": "Customer",
					"customer_name": f"E2E {label} {self.tag}",
					"customer_type": "Company",
					"customer_group": self._leaf("Customer Group"),
					"territory": self._leaf("Territory"),
				}
			)
			.insert(ignore_permissions=True)
			.name
		)

	def _item(self) -> str:
		return (
			frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": f"E2E-RESET-{self.tag}",
					"item_name": f"E2E reset {self.tag}",
					"item_group": self._leaf("Item Group"),
					"stock_uom": frappe.db.get_single_value("Stock Settings", "stock_uom") or "Nos",
					"is_stock_item": 0,
					"is_sales_item": 1,
				}
			)
			.insert(ignore_permissions=True)
			.name
		)

	def _order(self, customer: str, submit: bool = True):
		order = frappe.get_doc(
			{
				"doctype": "Sales Order",
				"customer": customer,
				"company": self.company,
				"transaction_date": nowdate(),
				"delivery_date": add_days(nowdate(), 1),
				"selling_price_list": self.price_list,
				"items": [
					{"item_code": self.item, "qty": 1, "rate": 10, "delivery_date": add_days(nowdate(), 1)}
				],
			}
		).insert(ignore_permissions=True)
		if submit:
			order.submit()
		return order

	def _invoice(self, order):
		from erpnext.selling.doctype.sales_order.sales_order import make_sales_invoice

		invoice = make_sales_invoice(order.name)
		invoice.insert(ignore_permissions=True)
		invoice.submit()
		return invoice

	def _request(self, order, customer: str):
		request = frappe.get_doc(
			{
				"doctype": "Payment Request",
				"payment_request_type": "Inward",
				"party_type": "Customer",
				"party": customer,
				"reference_doctype": "Sales Order",
				"reference_name": order.name,
				"grand_total": 10,
				"currency": order.currency,
				"company": self.company,
				"email_to": "reset-test@example.com",
			}
		).insert(ignore_permissions=True)
		request.submit()
		return request

	def _chain(self, customer: str):
		"""An order, the invoice made from it and a payment request for it, all submitted."""
		order = self._order(customer)
		invoice = self._invoice(order)
		request = self._request(order, customer)
		return order.name, invoice.name, request.name

	def _docstatus(self, doctype: str, name: str):
		return frappe.db.get_value(doctype, name, "docstatus")

	def _invoice_lines_citing_a_missing_order(self, customer: str) -> list:
		return list(
			frappe.db.sql(
				"""select item.name from `tabSales Invoice Item` item
				join `tabSales Invoice` invoice on invoice.name = item.parent
				where invoice.customer = %s and ifnull(item.sales_order, '') != ''
				and not exists (
					select 1 from `tabSales Order` `order` where `order`.name = item.sales_order
				)""",
				customer,
			)
		)

	# -- the tests ------------------------------------------------------------------------------

	def test_submitted_documents_are_cancelled_and_kept_and_no_invoice_cites_a_missing_order(self):
		order, invoice, request = self._chain(self.customer)

		stats = fixtures.reset_test_env()

		submitted = {"customer": self.customer, "docstatus": 1}
		self.assertEqual(frappe.db.count("Sales Invoice", submitted), 0)
		self.assertEqual(self._docstatus("Sales Invoice", invoice), 2, "the invoice is kept")
		self.assertEqual(self._docstatus("Sales Order", order), 2, "the order is kept")
		self.assertEqual(self._docstatus("Payment Request", request), 2, "the request is kept")
		self.assertEqual(self._invoice_lines_citing_a_missing_order(self.customer), [])
		counts = ("sales_invoices", "sales_orders", "payment_requests", "skipped_linked")
		self.assertEqual(tuple(stats[key] for key in counts), (1, 1, 1, 0))

	def test_the_next_order_does_not_take_the_number_of_a_reset_one(self):
		order, _invoice, _request = self._chain(self.customer)

		fixtures.reset_test_env()

		# A new order takes the next number of the series: had the reset one been deleted as the last of
		# its series, the counter would have been reverted and this one would carry its name.
		fresh = self._order(self.other_customer, submit=False)
		self.assertNotEqual(fresh.name, order)

	def test_the_documents_of_another_customer_are_left_alone(self):
		other_order, other_invoice, other_request = self._chain(self.other_customer)
		self._chain(self.customer)

		fixtures.reset_test_env()

		self.assertEqual(self._docstatus("Sales Order", other_order), 1)
		self.assertEqual(self._docstatus("Sales Invoice", other_invoice), 1)
		self.assertEqual(self._docstatus("Payment Request", other_request), 1)

	def test_a_draft_order_nobody_cites_is_deleted(self):
		draft = self._order(self.customer, submit=False)
		fixtures.reset_test_env()
		self.assertFalse(frappe.db.exists("Sales Order", draft.name))

	def test_a_cancel_frappe_refuses_leaves_the_order_submitted_and_present(self):
		# A submitted payment request blocks the cancel of its order. Frappe has already written
		# docstatus 2 to the row when it raises; the helper used to delete that half-cancelled row.
		order = self._order(self.customer)
		self._request(order, self.customer)

		self.assertFalse(fixtures._safe_cancel_and_delete("Sales Order", order.name))

		self.assertEqual(self._docstatus("Sales Order", order.name), 1, "the order is exactly as it was")
		logged = {"method": ["like", f"E2E reset left Sales Order {order.name}%"]}
		self.assertTrue(
			frappe.db.exists("Error Log", logged), "the refusal is written to the Error Log, not swallowed"
		)
