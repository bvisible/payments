# Copyright (c) 2026, Neoffice and Contributors
# License: MIT. See LICENSE
"""Unit tests for the TWINT PHP bridge driver (mocks the HTTP layer)."""

from __future__ import annotations

import json
from unittest.mock import patch, MagicMock

import frappe
from frappe.tests.utils import FrappeTestCase

from payments.drivers.base import IntentRequest
from payments.drivers.twint.php_bridge_driver import TwintPHPBridgeDriver

PROVIDER_NAME = "twint_test_unit"
CHANNEL_CODE = "qr_bridge"
MERCHANT_UUID = "test_merchant_unit"


def _ensure_fixtures() -> None:
	if not frappe.db.exists("Payment Provider", PROVIDER_NAME):
		frappe.get_doc(
			{
				"doctype": "Payment Provider",
				"provider_name": PROVIDER_NAME,
				"display_label": "TWINT (unit-tests)",
				"enabled": 1,
				"mode": "test",
				"driver_class": "payments.drivers.twint.php_bridge_driver.TwintPHPBridgeDriver",
				"credentials_json": json.dumps(
					{
						"service_url": "https://neoservice.example.com",
						"api_key": "test_key",
						"api_secret": "test_secret",
						"default_merchant_uuid": MERCHANT_UUID,
					}
				),
			}
		).insert(ignore_permissions=True)
	if not frappe.db.exists("Payment Channel", CHANNEL_CODE):
		frappe.get_doc(
			{
				"doctype": "Payment Channel",
				"channel_code": CHANNEL_CODE,
				"display_label": "TWINT QR Bridge",
				"ui_kind": "qr_display",
				"capabilities_json": json.dumps({"supports_refund": True, "requires_qr_scan": True}),
			}
		).insert(ignore_permissions=True)
	if not frappe.db.get_value(
		"Provider Channel Settings", {"provider": PROVIDER_NAME, "channel": CHANNEL_CODE}, "name"
	):
		frappe.get_doc(
			{
				"doctype": "Provider Channel Settings",
				"provider": PROVIDER_NAME,
				"channel": CHANNEL_CODE,
				"enabled": 1,
			}
		).insert(ignore_permissions=True)
	if not frappe.db.exists("Twint Settings", MERCHANT_UUID):
		frappe.get_doc(
			{
				"doctype": "Twint Settings",
				"merchant_uuid": MERCHANT_UUID,
				"display_label": "Test Merchant Unit",
				"enabled": 1,
				"store_uuid": "00000000-0000-0000-0000-000000000000",
				"environment": "sandbox",
				"p12_password": "not_real_password",
			}
		).insert(ignore_permissions=True)


def _build_driver() -> TwintPHPBridgeDriver:
	provider_doc = frappe.get_doc("Payment Provider", PROVIDER_NAME)
	channel_doc = frappe.get_doc("Payment Channel", CHANNEL_CODE)
	binding = frappe.db.get_value(
		"Provider Channel Settings", {"provider": PROVIDER_NAME, "channel": CHANNEL_CODE}, "name"
	)
	binding_doc = frappe.get_doc("Provider Channel Settings", binding)
	return TwintPHPBridgeDriver.from_docs(provider_doc, channel_doc, binding_doc)


def _mock_post_response(payload: dict) -> MagicMock:
	resp = MagicMock()
	resp.ok = True
	resp.status_code = 200
	resp.headers = {"Content-Type": "application/json"}
	resp.json = MagicMock(return_value={"message": payload})
	return resp


class TestTwintPHPBridgeDriver(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_fixtures()

	def test_create_intent_calls_register_payment(self):
		driver = _build_driver()
		request = IntentRequest(
			intent_name="PI-test-twint-001",
			amount=2500,
			currency="CHF",
			metadata={"twint_merchant_uuid": MERCHANT_UUID},
		)
		with patch("requests.post") as mock_post:
			mock_post.return_value = _mock_post_response(
				{
					"success": True,
					"order_id": "order_abc123",
					"order_status": "InProgress",
					"transaction_status": "ORDER_RECEIVED",
					"pairing_token": "PAIR_TOKEN_XYZ",
				}
			)
			result = driver.create_intent(request)

		mock_post.assert_called_once()
		body = mock_post.call_args.kwargs["json"]
		self.assertEqual(body["command"], "register_payment")
		self.assertEqual(body["merchant_uuid"], MERCHANT_UUID)
		params_sent = json.loads(body["params"])
		self.assertEqual(params_sent["amount"], 2500)
		self.assertEqual(params_sent["currency"], "CHF")
		self.assertEqual(params_sent["merchant_reference"], "PI-test-twint-001")

		self.assertEqual(result.status, "requires_action")
		self.assertEqual(result.provider_intent_id, "order_abc123")
		self.assertEqual(result.next_action_type, "display_qr_payload")
		self.assertEqual(result.next_action_payload["pairing_token"], "PAIR_TOKEN_XYZ")

	def test_create_intent_returns_failed_when_bridge_unsuccessful(self):
		driver = _build_driver()
		with patch("requests.post") as mock_post:
			mock_post.return_value = _mock_post_response(
				{"success": False, "error": "Certificate expired", "exception": "RuntimeException"}
			)
			result = driver.create_intent(
				IntentRequest(
					intent_name="PI-fail-001",
					amount=500,
					currency="CHF",
					metadata={"twint_merchant_uuid": MERCHANT_UUID},
				)
			)
		self.assertEqual(result.status, "failed")
		self.assertEqual(result.error_code, "RuntimeException")
		self.assertIn("Certificate expired", result.error_message or "")

	def test_create_intent_without_merchant_uuid_fails(self):
		# Wipe the provider default and binding default first.
		provider_doc = frappe.get_doc("Payment Provider", PROVIDER_NAME)
		creds = provider_doc.get_credentials()
		creds.pop("default_merchant_uuid", None)
		provider_doc.credentials_json = json.dumps(creds)
		provider_doc.save(ignore_permissions=True)

		driver = _build_driver()
		result = driver.create_intent(
			IntentRequest(intent_name="PI-no-merchant", amount=1, currency="CHF")
		)
		self.assertEqual(result.status, "failed")
		self.assertEqual(result.error_code, "no_merchant_uuid")

		# Restore the credentials for subsequent tests.
		creds["default_merchant_uuid"] = MERCHANT_UUID
		provider_doc.credentials_json = json.dumps(creds)
		provider_doc.save(ignore_permissions=True)

	def test_status_mapping_buckets(self):
		driver = _build_driver()
		self.assertEqual(driver._map_status("ORDER_OK_SUCCESS"), "succeeded")
		self.assertEqual(driver._map_status("MERCHANT_COMPLETED"), "succeeded")
		self.assertEqual(driver._map_status("CLIENT_FAILED"), "failed")
		self.assertEqual(driver._map_status("CLIENT_ABORTED"), "canceled")
		self.assertEqual(driver._map_status("IN_PROGRESS"), "processing")
		self.assertEqual(driver._map_status("PAIRED_AND_PENDING"), "processing")
		self.assertIsNone(driver._map_status("UNKNOWN_STATE"))
		self.assertIsNone(driver._map_status(None))

	def test_cancel_intent_requires_merchant_uuid_via_metadata(self):
		# Create a Payment Intent record with metadata.twint_merchant_uuid so the driver can find it.
		intent_doc = frappe.get_doc(
			{
				"doctype": "Payment Intent",
				"provider": PROVIDER_NAME,
				"channel": CHANNEL_CODE,
				"amount": 100,
				"currency": "CHF",
				"provider_intent_id": "order_to_cancel",
				"metadata_json": json.dumps({"twint_merchant_uuid": MERCHANT_UUID}),
			}
		).insert(ignore_permissions=True)
		driver = _build_driver()
		with patch("requests.post") as mock_post:
			mock_post.return_value = _mock_post_response({"success": True, "order_id": "order_to_cancel"})
			result = driver.cancel_intent("order_to_cancel")
		self.assertEqual(result.status, "canceled")
		# Cleanup.
		for ev in frappe.get_all("Payment Event", filters={"intent": intent_doc.name}, pluck="name"):
			frappe.delete_doc("Payment Event", ev, force=True, ignore_permissions=True)
		frappe.delete_doc("Payment Intent", intent_doc.name, force=True, ignore_permissions=True)

	def test_refund_requires_amount(self):
		driver = _build_driver()
		result = driver.refund("order_xyz", amount=None)
		self.assertEqual(result.status, "failed")
		self.assertEqual(result.error_code, "amount_required")

	def test_handle_webhook_returns_not_supported(self):
		driver = _build_driver()
		result = driver.handle_webhook(b"{}", {})
		self.assertFalse(result.signature_valid)
		self.assertEqual(result.error_code, "twint_no_webhooks")
