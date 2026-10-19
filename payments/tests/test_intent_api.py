# //// Neoffice — added file (no upstream equivalent). Exercises `payments.api.intent.*` →
# //// `resolve_driver` → `MockDriver` → the Payment Intent FSM, touching no external
# //// service. The only test in the repo that can run anywhere, on any site.
# //// Commits: e32ecf5 2026-05-13 "Phase 1".
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
	if not frappe.db.exists("Provider Channel Settings", {"provider": "mock", "channel": "terminal"}):
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
		created = intent_api.create_intent(
			provider="mock", channel="terminal", amount=500, currency="CHF"
		)
		fetched = intent_api.get_intent_status(created["intent_name"])
		self.assertEqual(fetched["intent_name"], created["intent_name"])
		self.assertEqual(fetched["status"], "requires_action")

	def test_cancel_intent_terminal_state(self):
		created = intent_api.create_intent(
			provider="mock", channel="terminal", amount=200, currency="CHF"
		)
		result = intent_api.cancel_intent(created["intent_name"])
		self.assertEqual(result["status"], "canceled")

	def test_a_cancellation_the_poller_recorded_first_is_not_an_error(self):
		# maintenance#1721: the TWINT poller reads the cancellation at the PSP and records it while cancel_intent
		# still holds the copy it loaded; saving that copy failed with TimestampMismatchError and the shop said
		# "Error cancelling payment" for a payment that was cancelled.
		from unittest.mock import patch

		from payments.drivers.base import DriverResponse
		from payments.drivers.mock_driver import MockDriver

		created = intent_api.create_intent(
			provider="mock", channel="terminal", amount=300, currency="CHF"
		)
		name = created["intent_name"]

		def the_poller_records_it_first(_driver, provider_intent_id):
			poller_copy = frappe.get_doc("Payment Intent", name)
			poller_copy.transition_to("canceled", event_source="poll", ignore_invalid=True)
			return DriverResponse(status="canceled", provider_intent_id=provider_intent_id)

		with patch.object(MockDriver, "cancel_intent", the_poller_records_it_first):
			result = intent_api.cancel_intent(name, reason="customer left")
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


class TestTheIntentApiChangesStateOnPostOnly(FrappeTestCase):
	"""maintenance#1721: a GET is rolled back by Frappe, so a refund opened as an address returned the money at the
	PSP and left the intent `succeeded`, refundable again. Creating, cancelling, refunding: POST only."""

	def test_creating_cancelling_and_refunding_refuse_a_get(self):
		for function in (intent_api.create_intent, intent_api.cancel_intent, intent_api.refund_intent):
			with self.subTest(function=function.__name__):
				self.assertEqual(tuple(frappe.allowed_http_methods_for_whitelisted_func[function]), ("POST",))

	def test_the_wrappers_that_create_or_cancel_refuse_a_get_too(self):
		# The shop's TWINT dialog and the mobile app reach create_intent / cancel_intent through these: a guard on
		# the inner function would not hold if the wrapper still took a GET.
		from payments.api import mobile_payments
		from payments.integrations.twint import api as twint_web

		for function in (
			twint_web.create_web_transaction,
			twint_web.cancel_web_transaction,
			mobile_payments.mobile_start_payment,
			mobile_payments.mobile_abandon_payment,
		):
			with self.subTest(function=function.__name__):
				self.assertEqual(tuple(frappe.allowed_http_methods_for_whitelisted_func[function]), ("POST",))

	def test_reading_a_status_stays_open_to_a_get(self):
		self.assertIn(
			"GET", tuple(frappe.allowed_http_methods_for_whitelisted_func[intent_api.get_intent_status])
		)
