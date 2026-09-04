#//// Neoffice — added file (no upstream equivalent). Unit tests for the Payrexx web
#//// and terminal drivers with the client stubbed: the HTTP layer is the standalone
#//// `payrexx` library's job and is tested there against a live account, so these
#//// only cover what this app owns — the request built from an IntentRequest, the FSM
#//// status derived, and that a transport failure reads as *unknown*, never as a
#//// clean rejection.
#//// Commits: 4c05756 2026-08-11 "feat(payrexx): add Payrexx as a third payment provider"
#////          ac59479 2026-08-11 "fix(payrexx): read the terminal serial from the right Payment Device field"
# Copyright (c) 2026, Neoffice and Contributors
# License: MIT. See LICENSE
"""Unit tests for the Payrexx drivers.

The HTTP layer is the ``payrexx`` library's job and is tested there against a live
account. These tests stub the client and check what this app is responsible for:

- the request the driver builds from an ``IntentRequest``
- the FSM status it derives from a Payrexx status
- that a transport failure is reported as *unknown*, never as a clean rejection
- that a statuses with no FSM equivalent does **not** move the Payment Intent
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from payments.drivers.base import IntentRequest
from payments.drivers.payrexx._common import NEEDS_HUMAN, map_status
from payments.drivers.payrexx.terminal_driver import PayrexxTerminalDriver
from payments.drivers.payrexx.web_driver import PayrexxWebDriver

PROVIDER_NAME = "payrexx_test_unit"
WEB_CHANNEL = "payrexx_web"
TERMINAL_CHANNEL = "terminal"
DEVICE_LABEL = "Payrexx Terminal (unit-tests)"
SERIAL = "SN-N86-UNIT"


def _ensure_fixtures() -> None:
	if not frappe.db.exists("Payment Provider", PROVIDER_NAME):
		frappe.get_doc(
			{
				"doctype": "Payment Provider",
				"provider_name": PROVIDER_NAME,
				"display_label": "Payrexx (unit-tests)",
				"enabled": 1,
				"mode": "test",
				"driver_class": "payments.drivers.payrexx.web_driver.PayrexxWebDriver",
				"credentials_json": json.dumps(
					{
						"instance": "demo",
						"api_secret": "unit_secret",
						"pos_api_secret": "unit_pos_secret",
						"webhook_signing_key": "unit_signing_key",
					}
				),
			}
		).insert(ignore_permissions=True)

	for code, label, ui_kind in (
		(WEB_CHANNEL, "Payrexx Web Checkout", "redirect"),
		(TERMINAL_CHANNEL, "POS Terminal", "card_present_modal"),
	):
		if not frappe.db.exists("Payment Channel", code):
			frappe.get_doc(
				{
					"doctype": "Payment Channel",
					"channel_code": code,
					"display_label": label,
					"ui_kind": ui_kind,
					"capabilities_json": json.dumps({"supports_refund": True}),
				}
			).insert(ignore_permissions=True)

		if not frappe.db.get_value(
			"Provider Channel Settings", {"provider": PROVIDER_NAME, "channel": code}, "name"
		):
			frappe.get_doc(
				{
					"doctype": "Provider Channel Settings",
					"provider": PROVIDER_NAME,
					"channel": code,
					"enabled": 1,
				}
			).insert(ignore_permissions=True)

	# Payment Device is autonamed from a naming series, so it is looked up by its
	# label rather than by a fixed name.
	if not frappe.db.exists("Payment Device", {"device_label": DEVICE_LABEL}):
		binding = frappe.db.get_value(
			"Provider Channel Settings",
			{"provider": PROVIDER_NAME, "channel": TERMINAL_CHANNEL},
			"name",
		)
		frappe.get_doc(
			{
				"doctype": "Payment Device",
				"device_label": DEVICE_LABEL,
				"provider_channel_settings": binding,
				"serial_number": SERIAL,
				"provider_device_id": SERIAL,
				"enabled": 1,
			}
		).insert(ignore_permissions=True)


def _device_name() -> str:
	"""The autonamed Payment Device record name for our fixture."""
	return frappe.db.get_value("Payment Device", {"device_label": DEVICE_LABEL}, "name")


def _fake_gateway(**overrides):
	gateway = MagicMock()
	gateway.id = overrides.get("id", 36085448)
	gateway.hash = overrides.get("hash", "aaaa1111")
	gateway.link = overrides.get("link", "https://demo.payrexx.com/?payment=aaaa1111")
	gateway.app_link = overrides.get("app_link")
	gateway.status = overrides.get("status", "waiting")
	gateway.payment_methods = overrides.get("payment_methods", ())
	gateway.filter_was_applied = bool(gateway.payment_methods)
	gateway.raw = overrides.get("raw", {})
	return gateway


def _client_stub(**attrs):
	"""A context-manager stub standing in for PayrexxClient."""
	client = MagicMock()
	client.__enter__ = MagicMock(return_value=client)
	client.__exit__ = MagicMock(return_value=False)
	for key, value in attrs.items():
		setattr(client, key, value)
	return client


class TestPayrexxStatusMapping(FrappeTestCase):
	def test_documented_statuses_map_to_the_fsm(self):
		self.assertEqual(map_status("waiting"), "requires_action")
		self.assertEqual(map_status("confirmed"), "succeeded")
		self.assertEqual(map_status("cancelled"), "canceled")
		self.assertEqual(map_status("expired"), "canceled")
		self.assertEqual(map_status("declined"), "failed")
		self.assertEqual(map_status("error"), "failed")
		self.assertEqual(map_status("refunded"), "refunded")
		self.assertEqual(map_status("partially-refunded"), "refunded")
		self.assertEqual(map_status("authorized"), "processing")
		self.assertEqual(map_status("reserved"), "processing")
		self.assertEqual(map_status("refund_pending"), "processing")

	def test_sdk_only_statuses_are_known(self):
		"""`initiated` appears in the PHP SDK constants and in no documentation."""
		self.assertEqual(map_status("initiated"), "requires_action")

	def test_disputes_never_map_onto_refunded(self):
		"""A chargeback is not a refund — conflating them misstates the books."""
		for status in ("chargeback", "disputed", "insecure", "uncaptured"):
			self.assertIsNone(map_status(status), f"{status} must not drive a transition")
			self.assertIn(status, NEEDS_HUMAN)

	def test_unknown_status_returns_none_rather_than_guessing(self):
		self.assertIsNone(map_status("some-status-payrexx-adds-in-2027"))
		self.assertIsNone(map_status(None))
		self.assertIsNone(map_status(""))


class TestPayrexxWebDriver(FrappeTestCase):
	def setUp(self):
		_ensure_fixtures()
		self.driver = PayrexxWebDriver.from_docs(
			frappe.get_doc("Payment Provider", PROVIDER_NAME),
			frappe.get_doc("Payment Channel", WEB_CHANNEL),
			frappe.get_doc(
				"Provider Channel Settings",
				frappe.db.get_value(
					"Provider Channel Settings",
					{"provider": PROVIDER_NAME, "channel": WEB_CHANNEL},
					"name",
				),
			),
		)

	def test_create_intent_sends_the_intent_name_as_reference(self):
		"""referenceId is the only anchor from a Payrexx transaction back to us."""
		client = _client_stub()
		client.gateway.create.return_value = _fake_gateway()

		with patch.object(PayrexxWebDriver, "_client", return_value=client):
			response = self.driver.create_intent(
				IntentRequest(intent_name="PI-TEST-0001", amount=1500, currency="CHF")
			)

		kwargs = client.gateway.create.call_args.kwargs
		self.assertEqual(kwargs["reference_id"], "PI-TEST-0001")
		self.assertEqual(kwargs["amount"], 1500)
		self.assertEqual(kwargs["currency"], "CHF")
		self.assertEqual(response.status, "requires_action")
		self.assertEqual(response.next_action_type, "redirect_to_url")
		self.assertIn("payment=", response.next_action_payload["url"])

	def test_all_three_return_urls_point_at_our_success_page(self):
		"""The page reads the real status, so it must own failure and cancel too."""
		client = _client_stub()
		client.gateway.create.return_value = _fake_gateway()

		with patch.object(PayrexxWebDriver, "_client", return_value=client):
			self.driver.create_intent(
				IntentRequest(intent_name="PI-TEST-0002", amount=100, currency="CHF")
			)

		kwargs = client.gateway.create.call_args.kwargs
		for key in ("success_redirect_url", "failed_redirect_url", "cancel_redirect_url"):
			self.assertIn("/payrexx/success?payment_intent=PI-TEST-0002", kwargs[key])

	def test_dropped_payment_method_filter_is_logged(self):
		"""A silently-dropped filter lets a shopper pay by an unrecorded method."""
		client = _client_stub()
		client.gateway.create.return_value = _fake_gateway(payment_methods=())

		with (
			patch.object(PayrexxWebDriver, "_client", return_value=client),
			patch("frappe.log_error") as log_error,
		):
			self.driver.create_intent(
				IntentRequest(
					intent_name="PI-TEST-0003",
					amount=100,
					currency="CHF",
					metadata={"payment_methods": ["twint"]},
				)
			)

		self.assertTrue(log_error.called)
		self.assertIn("filter", log_error.call_args.args[0].lower())

	def test_honoured_filter_logs_nothing(self):
		client = _client_stub()
		client.gateway.create.return_value = _fake_gateway(payment_methods=("twint",))

		with (
			patch.object(PayrexxWebDriver, "_client", return_value=client),
			patch("frappe.log_error") as log_error,
		):
			self.driver.create_intent(
				IntentRequest(
					intent_name="PI-TEST-0004",
					amount=100,
					currency="CHF",
					metadata={"payment_methods": ["twint"]},
				)
			)

		self.assertFalse(log_error.called)

	def test_transport_failure_is_reported_as_unknown_not_declined(self):
		"""The distinction decides whether it is safe to act on the result."""
		from payrexx.errors import PayrexxTransportError

		client = _client_stub()
		client.gateway.create.side_effect = PayrexxTransportError("connection reset")

		with patch.object(PayrexxWebDriver, "_client", return_value=client):
			response = self.driver.create_intent(
				IntentRequest(intent_name="PI-TEST-0005", amount=100, currency="CHF")
			)

		self.assertEqual(response.status, "failed")
		self.assertEqual(response.error_code, "transport_error")

	def test_get_status_maps_a_confirmed_gateway(self):
		client = _client_stub()
		client.gateway.retrieve.return_value = _fake_gateway(status="confirmed")

		with patch.object(PayrexxWebDriver, "_client", return_value=client):
			response = self.driver.get_status("36085448")

		self.assertEqual(response.status, "succeeded")

	def test_get_status_of_a_disputed_gateway_stays_processing(self):
		"""No FSM state exists for a dispute, so the intent must not be moved."""
		client = _client_stub()
		client.gateway.retrieve.return_value = _fake_gateway(status="chargeback")

		with patch.object(PayrexxWebDriver, "_client", return_value=client):
			response = self.driver.get_status("36085448")

		self.assertEqual(response.status, "processing")


class TestPayrexxTerminalDriver(FrappeTestCase):
	def setUp(self):
		_ensure_fixtures()
		self.driver = PayrexxTerminalDriver.from_docs(
			frappe.get_doc("Payment Provider", PROVIDER_NAME),
			frappe.get_doc("Payment Channel", TERMINAL_CHANNEL),
			frappe.get_doc(
				"Provider Channel Settings",
				frappe.db.get_value(
					"Provider Channel Settings",
					{"provider": PROVIDER_NAME, "channel": TERMINAL_CHANNEL},
					"name",
				),
			),
		)

	def _payment_stub(self, status="pending"):
		payment = MagicMock()
		payment.payment_id = "pay_unit_1"
		payment.status = status
		payment.slip = ()
		payment.raw = {}
		return payment

	def test_create_intent_resolves_the_serial_from_the_device(self):
		client = _client_stub()
		client.ecr.create_payment.return_value = self._payment_stub()

		with patch.object(PayrexxTerminalDriver, "_client", return_value=client):
			response = self.driver.create_intent(
				IntentRequest(
					intent_name="PI-TERM-0001",
					amount=1500,
					currency="CHF",
					device_id=_device_name(),
					metadata={"payment_method": "twint", "purpose": "Table 4"},
				)
			)

		args, kwargs = client.ecr.create_payment.call_args
		self.assertEqual(args[0], SERIAL)
		self.assertEqual(kwargs["payment_reference"], "PI-TERM-0001")
		self.assertEqual(kwargs["payment_method"], "twint")
		self.assertEqual(kwargs["purpose"], "Table 4")
		self.assertEqual(response.next_action_type, "display_card_present_modal")
		self.assertEqual(response.provider_intent_id, "pay_unit_1")

	def test_missing_device_fails_cleanly(self):
		response = self.driver.create_intent(
			IntentRequest(intent_name="PI-TERM-0002", amount=100, currency="CHF")
		)
		self.assertEqual(response.status, "failed")
		self.assertEqual(response.error_code, "missing_device")

	def test_terminal_status_never_drives_the_fsm(self):
		"""payment_status has no documented vocabulary, so it stays informational."""
		client = _client_stub()
		client.ecr.get_payment.return_value = self._payment_stub(status="approved")

		with (
			patch.object(PayrexxTerminalDriver, "_client", return_value=client),
			patch.object(PayrexxTerminalDriver, "_device_for_intent", return_value=_device_name()),
		):
			response = self.driver.get_status("pay_unit_1")

		self.assertEqual(response.status, "processing")
		self.assertEqual(response.next_action_payload["terminal_status"], "approved")

	def test_transport_failure_on_a_terminal_payment_is_flagged_unknown(self):
		"""The case that would charge a customer twice if treated as a failure."""
		from payrexx.errors import PayrexxTransportError

		client = _client_stub()
		client.ecr.create_payment.side_effect = PayrexxTransportError("timeout")

		with patch.object(PayrexxTerminalDriver, "_client", return_value=client):
			response = self.driver.create_intent(
				IntentRequest(
					intent_name="PI-TERM-0003",
					amount=1500,
					currency="CHF",
					device_id=_device_name(),
				)
			)

		self.assertEqual(response.error_code, "transport_error")

	def test_pairing_returns_the_terminal_reported_configuration(self):
		"""Reading currency and tipping off the device beats assuming per client."""
		pairing = MagicMock()
		pairing.serial_number = SERIAL
		pairing.paired = True
		pairing.cashier_name = "Till 1"
		pairing.currency = "CHF"
		pairing.language = "fr"
		pairing.point_of_sale_name = "Boutique"
		pairing.timezone = "Europe/Zurich"
		pairing.has_tipping = True

		client = _client_stub()
		client.ecr.get_pairing.return_value = pairing

		with patch.object(PayrexxTerminalDriver, "_client", return_value=client):
			result = self.driver.pair_terminal(SERIAL, "QP3U58", cashier_name="Till 1")

		self.assertTrue(result["paired"])
		self.assertEqual(result["currency"], "CHF")
		self.assertTrue(result["has_tipping"])
		client.ecr.pair.assert_called_once()
