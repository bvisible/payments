#//// Neoffice — added file (no upstream equivalent). Smoke tests for the
#//// `Payment Provider` DocType of ADR-004: a valid record, the lowercase-identifier
#//// rule on `provider_name`, and the rejection of malformed `credentials_json`.
#//// Commits: e32ecf5 2026-05-13 "feat(payments): Phase 1 — unified payment driver layer (Provider × Channel × Driver)"
# Copyright (c) 2026, Neoffice and Contributors
# License: MIT. See LICENSE

import json

import frappe
from frappe.tests.utils import FrappeTestCase


class TestPaymentProvider(FrappeTestCase):
	"""Smoke tests for Payment Provider DocType."""

	def setUp(self):
		# Clean any leftover test record between runs.
		for name in ("test_provider_ok", "TEST_BAD"):
			if frappe.db.exists("Payment Provider", name):
				frappe.delete_doc("Payment Provider", name, force=True)

	def test_create_valid_provider(self):
		doc = frappe.get_doc(
			{
				"doctype": "Payment Provider",
				"provider_name": "test_provider_ok",
				"display_label": "Test Provider",
				"mode": "test",
				"credentials_json": json.dumps({"api_key": "sk_test_xxx"}),
				"driver_class": "payments.drivers.mock_driver.MockProvider",
			}
		).insert(ignore_permissions=True)
		self.assertEqual(doc.name, "test_provider_ok")
		self.assertEqual(doc.get_credentials(), {"api_key": "sk_test_xxx"})

	def test_reject_uppercase_provider_name(self):
		doc = frappe.get_doc(
			{
				"doctype": "Payment Provider",
				"provider_name": "TEST_BAD",
				"display_label": "Should fail",
				"mode": "test",
			}
		)
		with self.assertRaises(frappe.ValidationError):
			doc.insert(ignore_permissions=True)

	def test_reject_invalid_credentials_json(self):
		doc = frappe.get_doc(
			{
				"doctype": "Payment Provider",
				"provider_name": "test_provider_ok",
				"display_label": "Test",
				"mode": "test",
				"credentials_json": "not-json",
			}
		)
		with self.assertRaises(frappe.ValidationError):
			doc.insert(ignore_permissions=True)
