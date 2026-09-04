#//// Neoffice — added file (no upstream equivalent). Exercises `payments.api.intent.*` →
#//// `resolve_driver` → `MockDriver` → the Payment Intent FSM, touching no external
#//// service. The only test in the repo that can run anywhere, on any site.
#//// Commits: e32ecf5 2026-05-13 "Phase 1".
# Copyright (c) 2026, Neoffice and Contributors
# License: MIT. See LICENSE
"""End-to-end tests for the public intent API using the MockDriver.

These tests exercise the wiring `payments.api.intent.*` → `resolve_driver` →
`MockDriver` → `Payment Intent` FSM, without touching any external service.
"""

import json

import frappe
from frappe.tests.utils import FrappeTestCase

from payments.api import intent as intent_api


def _ensure_mock_setup() -> None:
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
	if not frappe.db.exists("Payment Channel", "terminal"):
		frappe.get_doc(
			{
				"doctype": "Payment Channel",
				"channel_code": "terminal",
				"display_label": "POS Terminal",
				"ui_kind": "card_present_modal",
				"capabilities_json": json.dumps({"supports_refund": True, "supports_tip": False}),
			}
		).insert(ignore_permissions=True)
	if not frappe.db.exists(
		"Provider Channel Settings", {"provider": "mock", "channel": "terminal"}
	):
		frappe.get_doc(
			{
				"doctype": "Provider Channel Settings",
				"provider": "mock",
				"channel": "terminal",
				"enabled": 1,
			}
		).insert(ignore_permissions=True)


class TestIntentAPIWithMockDriver(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_mock_setup()

	def test_create_intent_round_trip(self):
		result = intent_api.create_intent(
			provider="mock",
			channel="terminal",
			amount=1500,
			currency="CHF",
			metadata={"source": "unit-test"},
		)
		self.assertIn("intent_name", result)
		self.assertEqual(result["amount"], 1500)
		self.assertEqual(result["currency"], "CHF")
		self.assertEqual(result["status"], "requires_action")
		self.assertIsNotNone(result["provider_intent_id"])
		self.assertTrue(result["provider_intent_id"].startswith("mock_pi_"))

	def test_create_then_get_status(self):
		created = intent_api.create_intent(provider="mock", channel="terminal", amount=500, currency="CHF")
		fetched = intent_api.get_intent_status(created["intent_name"])
		self.assertEqual(fetched["intent_name"], created["intent_name"])
		self.assertEqual(fetched["status"], "requires_action")

	def test_cancel_intent_terminal_state(self):
		created = intent_api.create_intent(provider="mock", channel="terminal", amount=200, currency="CHF")
		result = intent_api.cancel_intent(created["intent_name"])
		self.assertEqual(result["status"], "canceled")

	def test_create_rejects_zero_amount(self):
		with self.assertRaises(frappe.ValidationError):
			intent_api.create_intent(provider="mock", channel="terminal", amount=0, currency="CHF")

	def test_metadata_accepted_as_json_string(self):
		# HTTP callers will send metadata as a string.
		result = intent_api.create_intent(
			provider="mock",
			channel="terminal",
			amount=100,
			currency="CHF",
			metadata='{"customer": "alice"}',
		)
		intent_doc = frappe.get_doc("Payment Intent", result["intent_name"])
		self.assertEqual(intent_doc.get_metadata(), {"customer": "alice"})
