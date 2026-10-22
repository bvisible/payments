# //// Neoffice — added file (no upstream equivalent). The webshop raises its card payment on a placeholder
# //// Sales Order (draft, `PENDING-<quotation>`), because upstream ERPNext refuses a Payment Request against a
# //// Quotation; the gateway pages here must not read that as « the order exists » (maintenance#1802).
# Copyright (c) 2026, Neoffice and Contributors
# License: MIT. See LICENSE
"""A Payment Request on a placeholder order is not on an order, and the Wallee transaction is built from it."""

from __future__ import annotations

import unittest
from unittest import mock

import frappe

from payments.utils.order_state import submitted_sales_order


class TestWhenARequestIsOnAnOrder(unittest.TestCase):
	def docstatus_of(self, value):
		return mock.patch.object(frappe.db, "get_value", return_value=value)

	def test_a_submitted_order_is_an_order(self):
		with self.docstatus_of(1):
			self.assertEqual(submitted_sales_order("Sales Order", "SAL-ORD-2026-00001"), "SAL-ORD-2026-00001")

	def test_a_draft_placeholder_is_not(self):
		with self.docstatus_of(0):
			self.assertIsNone(submitted_sales_order("Sales Order", "PENDING-SAL-QTN-2026-00001"))

	def test_a_cancelled_order_is_not(self):
		with self.docstatus_of(2):
			self.assertIsNone(submitted_sales_order("Sales Order", "SAL-ORD-2026-00001"))

	def test_an_order_that_is_gone_is_not(self):
		with self.docstatus_of(None):
			self.assertIsNone(submitted_sales_order("Sales Order", "SAL-ORD-GONE"))

	def test_something_else_than_an_order_is_not(self):
		with self.docstatus_of(1) as get_value:
			self.assertIsNone(submitted_sales_order("Quotation", "SAL-QTN-2026-00001"))
			self.assertIsNone(submitted_sales_order("Sales Order", None))
			self.assertIsNone(submitted_sales_order(None, "SAL-ORD-2026-00001"))
			get_value.assert_not_called()


class TestTheWalleeTransactionReadsTheOrderTheRequestIsOn(unittest.TestCase):
	"""create_web_transaction built the lines and the billing address from the quotation the request pointed at; the
	request now points at the placeholder order, which carries the same lines (it is built from the cart)."""

	def run_with(self, reference_doctype):
		from payments.integrations.wallee import api

		pr = frappe._dict(
			payment_request_type="Inward",
			reference_doctype=reference_doctype,
			reference_name="DOC-1",
			grand_total=30,
			currency="CHF",
		)
		document = frappe._dict(
			items=[frappe._dict(item_name="Thing", item_code="T-1", qty=2, amount=30)],
			taxes=[],
			contact_email="buyer@yopmail.com",
			contact_display="Ada Buyer",
			customer_address=None,
		)

		def get_doc(doctype, name=None):
			return pr if doctype == "Payment Request" else document

		failed = {"status": "failed", "error_message": "stop here"}
		with (
			mock.patch.object(frappe, "get_doc", side_effect=get_doc) as getter,
			mock.patch.object(api, "_resolve_wallee_provider_name", return_value="Wallee"),
			mock.patch("payments.api.intent.create_intent", return_value=failed) as create_intent,
		):
			api.create_web_transaction("PR-1")
		return getter, create_intent.call_args.kwargs["metadata"]

	def test_from_the_placeholder_order(self):
		getter, metadata = self.run_with("Sales Order")
		getter.assert_any_call("Sales Order", "DOC-1")
		self.assertEqual([row["sku"] for row in metadata["line_items"]], ["T-1"])
		self.assertEqual(metadata["billing_address"]["email_address"], "buyer@yopmail.com")

	def test_from_a_quotation_as_before(self):
		getter, metadata = self.run_with("Quotation")
		getter.assert_any_call("Quotation", "DOC-1")
		self.assertEqual([row["sku"] for row in metadata["line_items"]], ["T-1"])
