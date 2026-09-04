#//// Neoffice — added file (no upstream equivalent). Pins the Payment Intent state
#//// machine and its validations against a seeded Mock provider: allowed and refused
#//// transitions, the idempotent self-transition a webhook replay produces, the
#//// `Payment Event` written on each move, and the amount / currency / metadata
#//// checks. No upstream counterpart — there is no upstream Payment Intent.
#//// Commits: e32ecf5 2026-05-13 "feat(payments): Phase 1 — unified payment driver layer (Provider × Channel × Driver)"
# Copyright (c) 2026, Neoffice and Contributors
# License: MIT. See LICENSE
"""FSM and validation tests for Payment Intent."""

import json

import frappe
from frappe.tests.utils import FrappeTestCase


def _ensure_mock_provider_and_bindings() -> None:
	"""Idempotently create a minimal Mock Provider + Channel + binding for tests."""
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
	for channel_code, label, kind in (
		("terminal", "POS Terminal", "card_present_modal"),
		("web", "Web Checkout", "redirect"),
	):
		if not frappe.db.exists("Payment Channel", channel_code):
			frappe.get_doc(
				{
					"doctype": "Payment Channel",
					"channel_code": channel_code,
					"display_label": label,
					"ui_kind": kind,
					"capabilities_json": json.dumps({"supports_refund": True}),
				}
			).insert(ignore_permissions=True)
		binding = frappe.db.get_value(
			"Provider Channel Settings", {"provider": "mock", "channel": channel_code}, "name"
		)
		if not binding:
			frappe.get_doc(
				{
					"doctype": "Provider Channel Settings",
					"provider": "mock",
					"channel": channel_code,
					"enabled": 1,
				}
			).insert(ignore_permissions=True)


class TestPaymentIntentFSM(FrappeTestCase):
	"""Validates the FSM transitions enforced by :meth:`PaymentIntent.transition_to`."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_mock_provider_and_bindings()

	def _new_intent(self, **overrides) -> "PaymentIntent":  # noqa: F821
		defaults = {
			"doctype": "Payment Intent",
			"provider": "mock",
			"channel": "terminal",
			"amount": 1000,
			"currency": "CHF",
		}
		defaults.update(overrides)
		doc = frappe.get_doc(defaults).insert(ignore_permissions=True)
		return doc

	def test_default_status_is_requires_action(self):
		doc = self._new_intent()
		self.assertEqual(doc.status, "requires_action")
		self.assertIsNotNone(doc.created_at)
		self.assertIsNone(doc.completed_at)

	def test_valid_transition_to_succeeded_via_processing(self):
		doc = self._new_intent()
		self.assertTrue(doc.transition_to("processing"))
		self.assertEqual(doc.status, "processing")
		self.assertTrue(doc.transition_to("succeeded"))
		self.assertEqual(doc.status, "succeeded")
		self.assertIsNotNone(doc.completed_at)

	def test_invalid_transition_raises(self):
		doc = self._new_intent()
		doc.transition_to("succeeded")
		with self.assertRaises(frappe.ValidationError):
			doc.transition_to("requires_action")  # not allowed

	def test_invalid_transition_can_be_ignored(self):
		doc = self._new_intent()
		doc.transition_to("succeeded")
		result = doc.transition_to("requires_action", ignore_invalid=True)
		self.assertFalse(result)
		self.assertEqual(doc.status, "succeeded")

	def test_succeeded_can_be_refunded(self):
		doc = self._new_intent()
		doc.transition_to("succeeded")
		self.assertTrue(doc.transition_to("refunded"))
		self.assertEqual(doc.status, "refunded")

	def test_idempotent_self_transition(self):
		doc = self._new_intent()
		doc.transition_to("succeeded")
		# Re-asking the same terminal state should be a no-op, not an error.
		result = doc.transition_to("succeeded")
		self.assertFalse(result)
		self.assertEqual(doc.status, "succeeded")

	def test_payment_event_logged_on_transition(self):
		doc = self._new_intent()
		doc.transition_to("processing", event_source="api")
		events = frappe.get_all(
			"Payment Event",
			filters={"intent": doc.name},
			fields=["from_status", "to_status", "event_source"],
		)
		self.assertEqual(len(events), 1)
		self.assertEqual(events[0]["from_status"], "requires_action")
		self.assertEqual(events[0]["to_status"], "processing")
		self.assertEqual(events[0]["event_source"], "api")

	def test_amount_must_be_positive(self):
		with self.assertRaises(frappe.ValidationError):
			self._new_intent(amount=0)
		with self.assertRaises(frappe.ValidationError):
			self._new_intent(amount=-100)

	def test_currency_must_be_iso4217(self):
		with self.assertRaises(frappe.ValidationError):
			self._new_intent(currency="CH")  # too short
		with self.assertRaises(frappe.ValidationError):
			self._new_intent(currency="123")  # not alpha
		# Lowercase should be normalized to uppercase.
		doc = self._new_intent(currency="chf")
		self.assertEqual(doc.currency, "CHF")

	def test_invalid_metadata_json_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			self._new_intent(metadata_json="not-json")
		with self.assertRaises(frappe.ValidationError):
			self._new_intent(metadata_json='["array", "not", "object"]')
