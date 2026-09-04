#//// Neoffice — added file (no upstream equivalent). Pins the dedup guarantee of
#//// `Webhook Event Log`: because `event_id` is the autoname, a replayed provider
#//// event raises DuplicateEntryError at the database level instead of being
#//// processed a second time. Upstream has no shared webhook log to dedup.
#//// Commits: e32ecf5 2026-05-13 "feat(payments): Phase 1 — unified payment driver layer (Provider × Channel × Driver)"
# Copyright (c) 2026, Neoffice and Contributors
# License: MIT. See LICENSE
"""Dedup guarantee tests for Webhook Event Log.

The DocType uses ``event_id`` as its autoname, which combined with Frappe's
auto-named primary key gives us a DB-level uniqueness constraint. Inserting the
same event_id twice must raise DuplicateEntryError.
"""

import frappe
from frappe.tests.utils import FrappeTestCase


def _ensure_mock_provider() -> None:
	if not frappe.db.exists("Payment Provider", "mock"):
		frappe.get_doc(
			{
				"doctype": "Payment Provider",
				"provider_name": "mock",
				"display_label": "Mock",
				"enabled": 1,
				"mode": "test",
				"driver_class": "payments.drivers.mock_driver.MockDriver",
			}
		).insert(ignore_permissions=True)


class TestWebhookEventLogDedup(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_mock_provider()

	def test_first_insert_succeeds(self):
		log = frappe.get_doc(
			{
				"doctype": "Webhook Event Log",
				"event_id": "evt_test_unique_001",
				"provider": "mock",
				"event_type": "mock.event",
				"status": "Queued",
				"raw_payload": '{"hello": "world"}',
				"signature_valid": 1,
			}
		).insert(ignore_permissions=True)
		self.assertEqual(log.name, "evt_test_unique_001")
		self.assertIsNotNone(log.received_at)

	def test_duplicate_insert_raises(self):
		# Pre-insert.
		evt_id = "evt_test_duplicate_002"
		if not frappe.db.exists("Webhook Event Log", evt_id):
			frappe.get_doc(
				{
					"doctype": "Webhook Event Log",
					"event_id": evt_id,
					"provider": "mock",
					"event_type": "mock.event",
					"status": "Queued",
				}
			).insert(ignore_permissions=True)
		# Second insert must fail. Frappe wraps DB unique violations in different
		# exception types depending on backend; we accept any of them.
		with self.assertRaises((frappe.DuplicateEntryError, frappe.UniqueValidationError, frappe.ValidationError)):
			frappe.get_doc(
				{
					"doctype": "Webhook Event Log",
					"event_id": evt_id,
					"provider": "mock",
					"event_type": "mock.event",
					"status": "Queued",
				}
			).insert(ignore_permissions=True)
