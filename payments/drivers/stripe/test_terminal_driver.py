# Copyright (c) 2026, Neoffice and Contributors
# License: MIT. See LICENSE
"""Unit tests for the Stripe Terminal driver.

These tests do NOT hit api.stripe.com — they monkeypatch the SDK to assert that
the driver builds the right arguments and reacts correctly to canned responses.
The full end-to-end happens in :mod:`payments.tests.phase2_smoke` (which DOES
hit Stripe in sandbox mode, against a simulated reader).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from payments.drivers.base import IntentRequest
from payments.drivers.stripe.terminal_driver import StripeTerminalDriver


PROVIDER_NAME = "stripe_test_unit"
CHANNEL_CODE = "terminal"


def _stripe_pi_object(intent_id: str = "pi_test_123", **overrides) -> SimpleNamespace:
	"""Build a minimal mock PaymentIntent that quacks like stripe-python's object."""
	defaults = {
		"id": intent_id,
		"client_secret": f"{intent_id}_secret_xyz",
		"status": "requires_payment_method",
		"amount": 1500,
		"currency": "chf",
	}
	defaults.update(overrides)
	obj = SimpleNamespace(**defaults)
	obj.to_dict = lambda: dict(defaults)
	return obj


def _stripe_reader_object(reader_id: str = "tmr_test_001", **overrides) -> SimpleNamespace:
	defaults = {
		"id": reader_id,
		"label": "test reader",
		"device_type": "simulated_wisepos_e",
		"status": "online",
		"action": {"type": "process_payment_intent", "status": "in_progress"},
	}
	defaults.update(overrides)
	obj = SimpleNamespace(**defaults)
	obj.to_dict = lambda: dict(defaults)
	return obj


def _ensure_fixtures() -> None:
	"""Set up Payment Provider 'stripe_test_unit' + Channel 'terminal' + binding."""
	if not frappe.db.exists("Payment Provider", PROVIDER_NAME):
		frappe.get_doc(
			{
				"doctype": "Payment Provider",
				"provider_name": PROVIDER_NAME,
				"display_label": "Stripe (unit-tests)",
				"enabled": 1,
				"mode": "test",
				"driver_class": "payments.drivers.stripe.terminal_driver.StripeTerminalDriver",
				"credentials_json": json.dumps(
					{
						"secret_key": "sk_test_dummy_for_unit_tests",
						"publishable_key": "pk_test_dummy",
						"webhook_secret": "whsec_dummy",
					}
				),
			}
		).insert(ignore_permissions=True)
	if not frappe.db.exists("Payment Channel", CHANNEL_CODE):
		frappe.get_doc(
			{
				"doctype": "Payment Channel",
				"channel_code": CHANNEL_CODE,
				"display_label": "POS Terminal",
				"ui_kind": "card_present_modal",
				"capabilities_json": json.dumps({"supports_refund": True}),
			}
		).insert(ignore_permissions=True)
	binding = frappe.db.get_value(
		"Provider Channel Settings", {"provider": PROVIDER_NAME, "channel": CHANNEL_CODE}, "name"
	)
	if not binding:
		frappe.get_doc(
			{
				"doctype": "Provider Channel Settings",
				"provider": PROVIDER_NAME,
				"channel": CHANNEL_CODE,
				"enabled": 1,
			}
		).insert(ignore_permissions=True)


def _build_driver() -> StripeTerminalDriver:
	provider_doc = frappe.get_doc("Payment Provider", PROVIDER_NAME)
	channel_doc = frappe.get_doc("Payment Channel", CHANNEL_CODE)
	binding = frappe.db.get_value(
		"Provider Channel Settings", {"provider": PROVIDER_NAME, "channel": CHANNEL_CODE}, "name"
	)
	binding_doc = frappe.get_doc("Provider Channel Settings", binding)
	return StripeTerminalDriver.from_docs(provider_doc, channel_doc, binding_doc)


class TestStripeTerminalDriver(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_fixtures()

	def test_create_intent_builds_correct_kwargs(self):
		driver = _build_driver()
		request = IntentRequest(
			intent_name="PI-test-001",
			amount=1500,
			currency="CHF",
			reference_doctype="POS Invoice",
			reference_name="POSINV-0001",
			metadata={"branch": "lausanne"},
		)
		with patch("stripe.PaymentIntent.create") as mock_create:
			mock_create.return_value = _stripe_pi_object()
			result = driver.create_intent(request)

		mock_create.assert_called_once()
		kwargs = mock_create.call_args.kwargs
		self.assertEqual(kwargs["amount"], 1500)
		self.assertEqual(kwargs["currency"], "chf")
		self.assertEqual(kwargs["payment_method_types"], ["card_present"])
		self.assertEqual(kwargs["capture_method"], "manual")
		# Idempotency key is intent_name + a 12-char body hash for body-aware idempotency.
		self.assertTrue(kwargs["idempotency_key"].startswith("pi_create_PI-test-001_"))
		self.assertEqual(len(kwargs["idempotency_key"]), len("pi_create_PI-test-001_") + 12)
		# Metadata: frappe_intent_name + channel + reference_* + caller metadata.
		md = kwargs["metadata"]
		self.assertEqual(md["frappe_intent_name"], "PI-test-001")
		self.assertEqual(md["channel"], "pos_terminal")
		self.assertEqual(md["reference_doctype"], "POS Invoice")
		self.assertEqual(md["reference_name"], "POSINV-0001")
		self.assertEqual(md["branch"], "lausanne")

		self.assertEqual(result.status, "requires_action")
		self.assertEqual(result.provider_intent_id, "pi_test_123")
		self.assertEqual(result.next_action_type, "display_card_present_modal")

	def test_create_intent_returns_failed_on_stripe_error(self):
		import stripe

		driver = _build_driver()
		with patch("stripe.PaymentIntent.create") as mock_create:
			mock_create.side_effect = stripe.error.InvalidRequestError(
				"amount too small", "amount", code="amount_too_small"
			)
			result = driver.create_intent(
				IntentRequest(intent_name="PI-x", amount=1, currency="CHF")
			)
		self.assertEqual(result.status, "failed")
		self.assertEqual(result.error_code, "amount_too_small")

	def test_confirm_intent_requires_reader_id(self):
		driver = _build_driver()
		result = driver.confirm_intent("pi_test_123")
		self.assertEqual(result.status, "failed")
		self.assertEqual(result.error_code, "missing_reader_id")

	def test_confirm_intent_calls_process_payment_intent(self):
		driver = _build_driver()
		with patch("stripe.terminal.Reader.process_payment_intent") as mock_proc:
			mock_proc.return_value = _stripe_reader_object()
			result = driver.confirm_intent("pi_test_123", reader_id="tmr_001")

		mock_proc.assert_called_once()
		args = mock_proc.call_args.args
		kwargs = mock_proc.call_args.kwargs
		self.assertEqual(args[0], "tmr_001")
		self.assertEqual(kwargs["payment_intent"], "pi_test_123")
		self.assertEqual(kwargs["idempotency_key"], "proc_pi_test_123")
		self.assertEqual(result.status, "processing")
		self.assertEqual(result.next_action_payload["reader_id"], "tmr_001")

	def test_confirm_intent_terminal_reader_timeout_returns_processing(self):
		"""compass §1: terminal_reader_timeout is often a false negative — never re-create."""
		import stripe

		driver = _build_driver()
		with patch("stripe.terminal.Reader.process_payment_intent") as mock_proc:
			mock_proc.side_effect = stripe.error.APIConnectionError(
				"reader timed out", code="terminal_reader_timeout"
			)
			result = driver.confirm_intent("pi_test_123", reader_id="tmr_001")

		# Status stays processing — caller MUST reconcile via webhook, not retry.
		self.assertEqual(result.status, "processing")
		self.assertEqual(result.error_code, "terminal_reader_timeout")

	def test_cancel_intent_calls_payment_intent_cancel(self):
		driver = _build_driver()
		with patch("stripe.PaymentIntent.retrieve") as mock_get, patch(
			"stripe.PaymentIntent.cancel"
		) as mock_cancel:
			mock_get.return_value = _stripe_pi_object()
			mock_cancel.return_value = _stripe_pi_object(status="canceled")
			result = driver.cancel_intent("pi_test_123")

		mock_cancel.assert_called_once_with("pi_test_123", api_key="sk_test_dummy_for_unit_tests")
		self.assertEqual(result.status, "canceled")

	def test_cancel_intent_handles_already_canceled(self):
		import stripe

		driver = _build_driver()
		with patch("stripe.PaymentIntent.retrieve") as mock_get, patch(
			"stripe.PaymentIntent.cancel"
		) as mock_cancel:
			mock_get.return_value = _stripe_pi_object()
			mock_cancel.side_effect = stripe.error.InvalidRequestError(
				"already canceled", "id", code="payment_intent_unexpected_state"
			)
			result = driver.cancel_intent("pi_test_123")
		self.assertEqual(result.status, "canceled")

	def test_refund_calls_stripe_refund_create(self):
		driver = _build_driver()
		fake_refund = SimpleNamespace(
			id="re_001", amount=500, status="succeeded", to_dict=lambda: {"id": "re_001"}
		)
		with patch("stripe.Refund.create") as mock_refund:
			mock_refund.return_value = fake_refund
			result = driver.refund("pi_test_123", amount=500)

		mock_refund.assert_called_once()
		kwargs = mock_refund.call_args.kwargs
		self.assertEqual(kwargs["payment_intent"], "pi_test_123")
		self.assertEqual(kwargs["amount"], 500)
		self.assertEqual(kwargs["idempotency_key"], "rf_pi_test_123_500")
		self.assertEqual(result.status, "refunded")

	def test_handle_webhook_invalid_signature(self):
		driver = _build_driver()
		import stripe

		with patch("stripe.Webhook.construct_event") as mock_construct:
			mock_construct.side_effect = stripe.error.SignatureVerificationError(
				"bad sig", "sig_xxx"
			)
			result = driver.handle_webhook(b'{"foo":"bar"}', {"Stripe-Signature": "sig_xxx"})
		self.assertFalse(result.signature_valid)
		self.assertEqual(result.error_code, "invalid_signature")

	def test_handle_webhook_payment_intent_succeeded_maps_to_succeeded(self):
		driver = _build_driver()
		fake_event = {
			"id": "evt_001",
			"type": "payment_intent.succeeded",
			"data": {
				"object": {
					"id": "pi_test_123",
					"status": "succeeded",
					"metadata": {"frappe_intent_name": "PI-2026-00000001"},
				}
			},
		}
		with patch("stripe.Webhook.construct_event") as mock_construct:
			mock_construct.return_value = fake_event
			result = driver.handle_webhook(b"{}", {"Stripe-Signature": "any"})
		self.assertTrue(result.signature_valid)
		self.assertEqual(result.target_status, "succeeded")
		self.assertEqual(result.intent_name, "PI-2026-00000001")

	def test_handle_webhook_reader_action_failed_maps_to_failed(self):
		driver = _build_driver()
		fake_event = {
			"id": "evt_002",
			"type": "terminal.reader.action_failed",
			"data": {
				"object": {
					"id": "tmr_001",
					"action": {
						"type": "process_payment_intent",
						"payment_intent": {
							"id": "pi_test_xyz",
							"metadata": {"frappe_intent_name": "PI-2026-00000007"},
						},
					},
				}
			},
		}
		with patch("stripe.Webhook.construct_event") as mock_construct:
			mock_construct.return_value = fake_event
			result = driver.handle_webhook(b"{}", {"Stripe-Signature": "any"})
		self.assertEqual(result.target_status, "failed")
		self.assertEqual(result.intent_name, "PI-2026-00000007")

	def test_handle_webhook_passthrough_event_recorded_no_target(self):
		driver = _build_driver()
		fake_event = {
			"id": "evt_003",
			"type": "terminal.reader.action_succeeded",
			"data": {
				"object": {
					"id": "tmr_001",
					"action": {
						"type": "process_payment_intent",
						"payment_intent": {
							"id": "pi_test_xyz",
							"metadata": {"frappe_intent_name": "PI-2026-00000007"},
						},
					},
				}
			},
		}
		with patch("stripe.Webhook.construct_event") as mock_construct:
			mock_construct.return_value = fake_event
			result = driver.handle_webhook(b"{}", {"Stripe-Signature": "any"})
		self.assertTrue(result.signature_valid)
		self.assertIsNone(result.target_status)
		self.assertEqual(result.intent_name, "PI-2026-00000007")

	def test_capture_payment_calls_payment_intent_capture(self):
		driver = _build_driver()
		with patch("stripe.PaymentIntent.capture") as mock_cap:
			mock_cap.return_value = _stripe_pi_object(status="processing")
			result = driver.capture_payment("pi_test_123")
		mock_cap.assert_called_once_with(
			"pi_test_123",
			api_key="sk_test_dummy_for_unit_tests",
			idempotency_key="cap_pi_test_123",
		)
		self.assertEqual(result.status, "processing")
