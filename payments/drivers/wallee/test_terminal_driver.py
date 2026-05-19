# Copyright (c) 2026, Neoffice and Contributors
# License: MIT. See LICENSE
"""Unit tests for the Wallee Terminal driver.

These tests do NOT hit api-wallee.com — they monkeypatch the Wallee SDK to
assert that the driver builds the right arguments and reacts correctly to
canned responses. End-to-end validation happens manually on Osiris with a
physical Wallee terminal (see Phase C of the project plan).
"""

from __future__ import annotations

import hashlib
import hmac
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from payments.drivers.base import IntentRequest
from payments.drivers.wallee.terminal_driver import WalleeTerminalDriver


PROVIDER_NAME = "wallee_test_unit"
CHANNEL_CODE = "terminal"


def _wallee_tx(tx_id: int = 555, state: str = "PENDING", **overrides):
	"""Mock Wallee Transaction response. SDK returns an Enum with .value for state."""
	defaults = {
		"id": tx_id,
		"state": SimpleNamespace(value=state),
		"authorized_amount": 12.0,
	}
	defaults.update(overrides)
	obj = SimpleNamespace(**defaults)
	obj.to_dict = lambda: {"id": tx_id, "state": state, **overrides}
	return obj


def _wallee_refund(refund_id: int = 777, state: str = "SUCCESSFUL"):
	obj = SimpleNamespace(id=refund_id, state=SimpleNamespace(value=state))
	obj.to_dict = lambda: {"id": refund_id, "state": state}
	return obj


def _ensure_fixtures() -> None:
	"""Set up Payment Provider 'wallee_test_unit' + Channel 'terminal' + binding +
	a ``Wallee Settings`` row linked to that provider.

	Wallee Settings was promoted from Single → regular DocType in the merger
	(see ADR-005). One record per Payment Provider, keyed by ``provider``.
	"""
	# Wallee Settings DocType must be present (after the merger it lives in
	# payments/payments/doctype/wallee_settings/). Tests can't run if the
	# DocType isn't registered yet.
	if not frappe.db.exists("DocType", "Wallee Settings"):
		return

	# Create Payment Provider FIRST — Wallee Settings.provider links to it.
	if not frappe.db.exists("Payment Provider", PROVIDER_NAME):
		frappe.get_doc(
			{
				"doctype": "Payment Provider",
				"provider_name": PROVIDER_NAME,
				"display_label": "Wallee (unit-tests)",
				"enabled": 1,
				"mode": "test",
				"driver_class": "payments.drivers.wallee.terminal_driver.WalleeTerminalDriver",
				"credentials_json": "{}",
			}
		).insert(ignore_permissions=True)

	# Wallee Settings row for this provider (autoname=field:provider, so
	# the document name equals PROVIDER_NAME).
	if not frappe.db.exists("Wallee Settings", PROVIDER_NAME):
		frappe.get_doc(
			{
				"doctype": "Wallee Settings",
				"provider": PROVIDER_NAME,
				"enabled": 1,
				"user_id": 42,
				"authentication_key": "unit-test-key",
				"space_id": 100,
				"api_host": "https://app-wallee.com/api/v2.0",
				"webhook_secret": "unit-test-secret",
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


def _build_driver() -> WalleeTerminalDriver:
	provider_doc = frappe.get_doc("Payment Provider", PROVIDER_NAME)
	channel_doc = frappe.get_doc("Payment Channel", CHANNEL_CODE)
	binding = frappe.db.get_value(
		"Provider Channel Settings", {"provider": PROVIDER_NAME, "channel": CHANNEL_CODE}, "name"
	)
	binding_doc = frappe.get_doc("Provider Channel Settings", binding)
	return WalleeTerminalDriver.from_docs(provider_doc, channel_doc, binding_doc)


def _patch_services(tx_mock=None, terminals_mock=None, refunds_mock=None):
	"""Return a context manager that patches WalleeTerminalDriver._services."""
	tx_mock = tx_mock or MagicMock(name="TransactionsService")
	terminals_mock = terminals_mock or MagicMock(name="PaymentTerminalsService")
	refunds_mock = refunds_mock or MagicMock(name="RefundsService")
	return patch.object(
		WalleeTerminalDriver,
		"_services",
		return_value=(tx_mock, terminals_mock, refunds_mock),
	)


class TestWalleeTerminalDriver(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if not frappe.db.exists("DocType", "Wallee Settings"):
			raise cls.skipTest(cls, "wallee_integration app not installed on this site")
		_ensure_fixtures()

	# --------- create_intent ---------

	def test_create_intent_builds_tx_create_with_correct_args(self):
		driver = _build_driver()
		request = IntentRequest(
			intent_name="PI-w-001",
			amount=1500,  # rappen → 15.00 CHF major
			currency="CHF",
			metadata={"description": "Boutique unit test"},
		)
		tx_service = MagicMock(name="TransactionsService")
		tx_service.post_payment_transactions.return_value = _wallee_tx(tx_id=999, state="PENDING")
		with _patch_services(tx_mock=tx_service):
			result = driver.create_intent(request)

		tx_service.post_payment_transactions.assert_called_once()
		args, _ = tx_service.post_payment_transactions.call_args
		space_id, tx_create = args
		self.assertEqual(space_id, 100)
		self.assertEqual(tx_create.currency, "CHF")
		self.assertFalse(tx_create.auto_confirmation_enabled)
		self.assertEqual(tx_create.merchant_reference, "PI-w-001")
		self.assertEqual(len(tx_create.line_items), 1)
		self.assertEqual(tx_create.line_items[0].amount_including_tax, 15.0)
		self.assertEqual(tx_create.line_items[0].unique_id, "PI-w-001")

		self.assertEqual(result.status, "requires_action")
		self.assertEqual(result.provider_intent_id, "999")
		self.assertEqual(result.next_action_type, "display_card_present_modal")
		self.assertEqual(result.next_action_payload["wallee_state"], "PENDING")

	def test_create_intent_failed_on_sdk_exception(self):
		driver = _build_driver()
		tx_service = MagicMock()
		tx_service.post_payment_transactions.side_effect = RuntimeError("Wallee 503")
		with _patch_services(tx_mock=tx_service):
			result = driver.create_intent(
				IntentRequest(intent_name="PI-w-002", amount=100, currency="CHF")
			)
		self.assertEqual(result.status, "failed")
		self.assertEqual(result.error_code, "wallee_create_failed")
		self.assertIn("Wallee 503", result.error_message)

	# --------- confirm_intent ---------

	def test_confirm_intent_requires_reader_id(self):
		driver = _build_driver()
		result = driver.confirm_intent("12345")
		self.assertEqual(result.status, "failed")
		self.assertEqual(result.error_code, "missing_reader_id")

	def test_confirm_intent_pushes_to_terminal(self):
		driver = _build_driver()
		terminals = MagicMock()
		terminals.post_payment_terminals_id_perform_transaction.return_value = SimpleNamespace(
			to_dict=lambda: {"ok": True}
		)
		with _patch_services(terminals_mock=terminals):
			result = driver.confirm_intent("12345", reader_id="678")

		terminals.post_payment_terminals_id_perform_transaction.assert_called_once_with(
			678, 12345, 100
		)
		self.assertEqual(result.status, "processing")
		self.assertEqual(result.next_action_payload["terminal_id"], 678)

	def test_confirm_intent_invalid_ids(self):
		driver = _build_driver()
		result = driver.confirm_intent("not-an-int", reader_id="678")
		self.assertEqual(result.status, "failed")
		self.assertEqual(result.error_code, "invalid_id")

	# --------- cancel_intent ---------

	def test_cancel_intent_voids_via_sdk(self):
		driver = _build_driver()
		tx_service = MagicMock()
		tx_service.post_payment_transactions_id_void_online.return_value = _wallee_tx(state="VOIDED")
		with _patch_services(tx_mock=tx_service):
			result = driver.cancel_intent("12345")
		tx_service.post_payment_transactions_id_void_online.assert_called_once_with(12345, 100)
		self.assertEqual(result.status, "canceled")

	def test_cancel_intent_tolerates_already_terminal(self):
		driver = _build_driver()
		tx_service = MagicMock()
		tx_service.post_payment_transactions_id_void_online.side_effect = RuntimeError(
			"Transaction is already in state VOIDED"
		)
		with _patch_services(tx_mock=tx_service):
			result = driver.cancel_intent("12345")
		self.assertEqual(result.status, "canceled")

	# --------- refund ---------

	def test_refund_full_uses_authorized_amount(self):
		driver = _build_driver()
		tx_service = MagicMock()
		tx_service.get_payment_transactions_id.return_value = _wallee_tx(
			tx_id=12345, state="COMPLETED", authorized_amount=12.0
		)
		refunds = MagicMock()
		refunds.refund.return_value = _wallee_refund(state="SUCCESSFUL")
		with _patch_services(tx_mock=tx_service, refunds_mock=refunds):
			result = driver.refund("12345")  # full refund

		refunds.refund.assert_called_once()
		space, rc = refunds.refund.call_args.args
		self.assertEqual(space, 100)
		self.assertEqual(rc.transaction, 12345)
		self.assertEqual(rc.amount, 12.0)
		self.assertEqual(rc.type, "MERCHANT_INITIATED_ONLINE")
		self.assertEqual(result.status, "refunded")

	def test_refund_partial_uses_minor_amount(self):
		driver = _build_driver()
		refunds = MagicMock()
		refunds.refund.return_value = _wallee_refund(state="SUCCESSFUL")
		with _patch_services(refunds_mock=refunds):
			result = driver.refund("12345", amount=500)  # 5.00 CHF
		self.assertEqual(refunds.refund.call_args.args[1].amount, 5.0)
		self.assertEqual(result.status, "refunded")

	def test_refund_pending_returns_processing(self):
		driver = _build_driver()
		refunds = MagicMock()
		refunds.refund.return_value = _wallee_refund(state="PENDING")
		with _patch_services(refunds_mock=refunds):
			result = driver.refund("12345", amount=500)
		self.assertEqual(result.status, "processing")

	# --------- handle_webhook ---------

	def test_webhook_signature_mismatch(self):
		driver = _build_driver()
		body = b'{"entityId":1,"listenerEntityTechnicalName":"Transaction","state":"COMPLETED"}'
		result = driver.handle_webhook(body, {"X-Signature": "deadbeef"})
		self.assertFalse(result.signature_valid)
		self.assertEqual(result.error_code, "invalid_signature")

	def test_webhook_transaction_completed_maps_succeeded(self):
		driver = _build_driver()
		body = b'{"entityId":12345,"listenerEntityTechnicalName":"Transaction","state":"COMPLETED","spaceId":100}'
		sig = hmac.new(b"unit-test-secret", body, hashlib.sha256).hexdigest()
		# Pre-create a Payment Intent so the lookup succeeds.
		intent = frappe.get_doc(
			{
				"doctype": "Payment Intent",
				"provider": PROVIDER_NAME,
				"channel": CHANNEL_CODE,
				"amount": 1200,
				"currency": "CHF",
				"status": "processing",
				"provider_intent_id": "12345",
			}
		).insert(ignore_permissions=True)
		try:
			result = driver.handle_webhook(body, {"X-Signature": sig})
		finally:
			frappe.delete_doc("Payment Intent", intent.name, force=True, ignore_permissions=True)

		self.assertTrue(result.signature_valid)
		self.assertEqual(result.target_status, "succeeded")
		self.assertEqual(result.intent_name, intent.name)
		self.assertTrue(result.event_id.startswith("wallee-transaction-12345-"))

	def test_webhook_transaction_pending_no_transition(self):
		driver = _build_driver()
		body = b'{"entityId":222,"listenerEntityTechnicalName":"Transaction","state":"PENDING","spaceId":100}'
		sig = hmac.new(b"unit-test-secret", body, hashlib.sha256).hexdigest()
		result = driver.handle_webhook(body, {"X-Signature": sig})
		self.assertTrue(result.signature_valid)
		self.assertIsNone(result.target_status)

	def test_webhook_refund_successful(self):
		driver = _build_driver()
		body = b'{"entityId":777,"listenerEntityTechnicalName":"Refund","state":"SUCCESSFUL","spaceId":100}'
		sig = hmac.new(b"unit-test-secret", body, hashlib.sha256).hexdigest()
		result = driver.handle_webhook(body, {"X-Signature": sig})
		self.assertTrue(result.signature_valid)
		self.assertEqual(result.target_status, "refunded")
		# Refund webhook does not carry a Frappe intent name directly (we'd need
		# to follow the refund.transaction.id back; left to enrichment step).
		self.assertIsNone(result.intent_name)
