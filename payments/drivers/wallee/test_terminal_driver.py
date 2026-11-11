# //// Neoffice — added file (no upstream equivalent). Unit tests for the Wallee
# //// Terminal driver; the Wallee SDK is monkeypatched, so nothing reaches
# //// api-wallee.com. End-to-end validation stays manual on Osiris with a physical
# //// terminal, there being no Wallee reader simulator.
# //// Commits: 49af519 2026-05-18 "feat(drivers): Wallee Terminal driver (server-driven, webhook + polling fallback)"
# ////          99e929c 2026-05-19 "feat(payments): merge wallee_integration into payments — ADR-005"
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
from unittest.mock import MagicMock, create_autospec, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from payments.drivers.base import IntentRequest
from payments.drivers.wallee.terminal_driver import WalleeTerminalDriver


PROVIDER_NAME = "wallee_test_unit"
CHANNEL_CODE = "terminal"


# //// Neoffice — the mocks are the SDK's own models now (08.11.2026). They were SimpleNamespace objects shaped by the driver's code, with
# //// an `authorized_amount` the real `Transaction` does not have: the full refund read it, died, and the tests stayed green.
def _wallee_tx(tx_id: int = 555, state: str = "PENDING", **overrides):
	"""A real Wallee SDK `Transaction`. Its state is an Enum (`.value`), its amounts are `authorization_amount` and `completed_amount`.
	An unknown field fails here, as it would against Wallee."""
	from wallee.models import Transaction

	fields = {"authorization_amount": 12.0, **overrides}
	return Transaction(id=tx_id, state=state, **fields)


def _wallee_refund(refund_id: int = 777, state: str = "SUCCESSFUL"):
	from wallee.models import Refund, RefundState

	return Refund(id=refund_id, state=RefundState(state))


def _wallee_error(status: int, reason: str, message: str, code: str):
	"""A real `wallee.exceptions.ApiException`, whose text carries `message='…'` and `code='…'` as the SDK's does."""
	from wallee.exceptions import ApiException

	return ApiException(
		status=status,
		reason=reason,
		body=f"ClientError(id='1', message='{message}', code='{code}')",
	)


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


def _a_refunds_service():
	"""The service the SDK really has, not a mock that answers to any name: a call the SDK does not know fails here, as it does for a customer."""
	from wallee.service.refunds_service import RefundsService

	return create_autospec(RefundsService, instance=True)


def _patch_services(tx_mock=None, terminals_mock=None, refunds_mock=None):
	"""Return a context manager that patches WalleeTerminalDriver._services."""
	tx_mock = tx_mock or MagicMock(name="TransactionsService")
	terminals_mock = terminals_mock or MagicMock(name="PaymentTerminalsService")
	refunds_mock = refunds_mock or _a_refunds_service()
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
			# //// Neoffice — the condition is the doctype, not an app: Wallee came from the
			# //// retired `wallee_integration` and lives in `payments` since ADR-005, so naming
			# //// that app made this read like a dependency it is not (2026-09-22).
			raise cls.skipTest(cls, "Wallee Settings doctype absent: payments not installed on this site")
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
			result = driver.create_intent(IntentRequest(intent_name="PI-w-002", amount=100, currency="CHF"))
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

		terminals.post_payment_terminals_id_perform_transaction.assert_called_once_with(678, 12345, 100)
		self.assertEqual(result.status, "processing")
		self.assertEqual(result.next_action_payload["terminal_id"], 678)

	def test_confirm_intent_invalid_ids(self):
		driver = _build_driver()
		result = driver.confirm_intent("not-an-int", reader_id="678")
		self.assertEqual(result.status, "failed")
		self.assertEqual(result.error_code, "invalid_id")

	def _confirm_with(self, error: Exception):
		driver = _build_driver()
		terminals = MagicMock()
		terminals.post_payment_terminals_id_perform_transaction.side_effect = error
		with _patch_services(terminals_mock=terminals):
			return driver.confirm_intent("12345", reader_id="678")

	def test_a_terminal_that_never_answers_ends_in_a_readable_timeout(self):
		"""08.11.2026: the debug terminal was off; Wallee waited the 90 s of its long poll and said 543."""
		result = self._confirm_with(
			_wallee_error(
				543,
				"Request Timeout",
				"The request timed out. Make another attempt if there is a chance that the request can be completed.",
				"long_polling_timeout",
			)
		)

		# //// Neoffice — #2358: a timeout leaves the payment in flight (the SDK resumes it; the card may
		# //// still be tapped), it is settled by the webhook or the sweep, never ranked failed.
		self.assertEqual(result.status, "processing")
		self.assertEqual(result.raw["error_code"], "long_polling_timeout")
		self.assertEqual(result.next_action_payload["terminal_action_status"], "waiting")
		self.assertEqual(result.provider_intent_id, "12345")

	def test_a_read_that_times_out_leaves_the_payment_in_flight(self):
		from urllib3.exceptions import ReadTimeoutError

		result = self._confirm_with(
			ReadTimeoutError(None, "/perform", "Read timed out. (read timeout=30)")
		)
		self.assertEqual(result.status, "processing")

	def test_a_connection_that_never_opened_is_a_failure_to_retry(self):
		from urllib3.exceptions import ConnectTimeoutError

		result = self._confirm_with(ConnectTimeoutError(None, "Connection to app-wallee.com timed out"))
		self.assertEqual(result.status, "failed")

	def test_the_terminal_call_does_not_hold_the_till_ninety_seconds(self):
		driver = _build_driver()
		terminals = MagicMock()
		terminals.post_payment_terminals_id_perform_transaction.return_value = SimpleNamespace(
			to_dict=lambda: {}
		)
		with _patch_services(terminals_mock=terminals):
			driver.confirm_intent("12345", reader_id="678")
		self.assertEqual(terminals.api_client.configuration.request_timeout, (10, 30))

	def test_a_terminal_that_is_not_available_says_so_in_words(self):
		result = self._confirm_with(
			_wallee_error(
				422,
				"Unprocessable Entity",
				"The terminal is not available for transaction processing. Please make sure that the terminal is running, connected and is not processing any other transaction at the moment.",
				"terminal_not_available",
			)
		)

		self.assertEqual((result.status, result.error_code), ("failed", "terminal_not_available"))
		self.assertIn("running, connected", result.error_message)
		self.assertTrue(result.error_message.startswith("[HTTP 422]"))

	def test_a_transaction_cancelled_on_the_terminal_is_a_failure_not_an_exception(self):
		result = self._confirm_with(
			_wallee_error(
				422, "Unprocessable Entity", "Terminal transaction canceled.", "terminal_cancelled"
			)
		)

		self.assertEqual(result.status, "failed")
		self.assertIn("canceled", result.error_message)

	def test_whatever_goes_wrong_is_answered_never_raised(self):
		for error in (RuntimeError("boom"), ConnectionError("no route"), TimeoutError("read timed out")):
			with self.subTest(error=type(error).__name__):
				result = self._confirm_with(error)

				self.assertEqual(result.status, "failed")
				self.assertTrue(result.error_code)
				self.assertTrue(result.error_message)

	def test_the_message_and_code_are_pulled_out_of_the_sdks_text(self):
		from payments.drivers.wallee.terminal_driver import _extract_wallee_error

		message, code = _extract_wallee_error(
			_wallee_error(404, "Not Found", "The requested resource does not exist.", "resource_missing")
		)
		self.assertEqual(
			(message, code), ("[HTTP 404] The requested resource does not exist.", "resource_missing")
		)

		# an error the SDK words in another way is kept, cut short, and has no code
		message, code = _extract_wallee_error(RuntimeError("x" * 500))
		self.assertEqual(len(message), 200)
		self.assertIsNone(code)

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

	def test_refund_full_uses_the_captured_amount(self):
		driver = _build_driver()
		tx_service = MagicMock()
		tx_service.get_payment_transactions_id.return_value = _wallee_tx(
			tx_id=12345, state="COMPLETED", authorization_amount=15.0, completed_amount=12.0
		)
		refunds = _a_refunds_service()
		refunds.post_payment_refunds.return_value = _wallee_refund(state="SUCCESSFUL")
		with _patch_services(tx_mock=tx_service, refunds_mock=refunds):
			result = driver.refund("12345")  # full refund

		refunds.post_payment_refunds.assert_called_once()
		space, rc = refunds.post_payment_refunds.call_args.args
		self.assertEqual(space, 100)
		self.assertEqual(rc.transaction, 12345)
		self.assertEqual(rc.amount, 12.0)
		self.assertEqual(rc.type, "MERCHANT_INITIATED_ONLINE")
		self.assertEqual(result.status, "refunded")

	def test_refund_full_falls_back_to_the_authorized_amount(self):
		driver = _build_driver()
		tx_service = MagicMock()
		tx_service.get_payment_transactions_id.return_value = _wallee_tx(
			tx_id=12345, state="COMPLETED", authorization_amount=9.5
		)
		refunds = _a_refunds_service()
		refunds.post_payment_refunds.return_value = _wallee_refund(state="SUCCESSFUL")
		with _patch_services(tx_mock=tx_service, refunds_mock=refunds):
			result = driver.refund("12345")

		self.assertEqual(refunds.post_payment_refunds.call_args.args[1].amount, 9.5)
		self.assertEqual(result.status, "refunded")

	def test_refund_full_of_a_transaction_that_says_no_amount_is_refused_and_asks_wallee_nothing(
		self,
	):
		driver = _build_driver()
		tx_service = MagicMock()
		tx_service.get_payment_transactions_id.return_value = _wallee_tx(
			tx_id=12345, state="COMPLETED", authorization_amount=None
		)
		refunds = _a_refunds_service()
		with _patch_services(tx_mock=tx_service, refunds_mock=refunds):
			result = driver.refund("12345")

		self.assertEqual((result.status, result.error_code), ("failed", "invalid_amount"))
		refunds.post_payment_refunds.assert_not_called()

	def test_the_driver_reads_amounts_that_the_sdks_transaction_has(self):
		"""The contract with the SDK: a field the driver reads must exist on the model, or a mock can invent it and hide the error."""
		from wallee.models import Transaction

		for field in ("id", "state", "authorization_amount", "completed_amount"):
			self.assertIn(field, Transaction.model_fields)
		self.assertNotIn("authorized_amount", Transaction.model_fields)

	def test_refundable_amount_prefers_what_was_captured_then_what_was_authorized_then_nothing(self):
		from payments.drivers.wallee.terminal_driver import refundable_amount

		self.assertEqual(
			refundable_amount(_wallee_tx(authorization_amount=15.0, completed_amount=12.0)), 12.0
		)
		self.assertEqual(refundable_amount(_wallee_tx(authorization_amount=15.0)), 15.0)
		self.assertEqual(refundable_amount(_wallee_tx(authorization_amount=None)), 0.0)
		self.assertEqual(refundable_amount(SimpleNamespace()), 0.0)

	def test_refund_partial_uses_minor_amount(self):
		driver = _build_driver()
		refunds = _a_refunds_service()
		refunds.post_payment_refunds.return_value = _wallee_refund(state="SUCCESSFUL")
		with _patch_services(refunds_mock=refunds):
			result = driver.refund("12345", amount=500)  # 5.00 CHF
		self.assertEqual(refunds.post_payment_refunds.call_args.args[1].amount, 5.0)
		self.assertEqual(result.status, "refunded")

	def test_refund_pending_returns_processing(self):
		driver = _build_driver()
		refunds = _a_refunds_service()
		refunds.post_payment_refunds.return_value = _wallee_refund(state="PENDING")
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
		body = (
			b'{"entityId":222,"listenerEntityTechnicalName":"Transaction","state":"PENDING","spaceId":100}'
		)
		sig = hmac.new(b"unit-test-secret", body, hashlib.sha256).hexdigest()
		result = driver.handle_webhook(body, {"X-Signature": sig})
		self.assertTrue(result.signature_valid)
		self.assertIsNone(result.target_status)

	def test_webhook_refund_successful(self):
		driver = _build_driver()
		body = (
			b'{"entityId":777,"listenerEntityTechnicalName":"Refund","state":"SUCCESSFUL","spaceId":100}'
		)
		sig = hmac.new(b"unit-test-secret", body, hashlib.sha256).hexdigest()
		result = driver.handle_webhook(body, {"X-Signature": sig})
		self.assertTrue(result.signature_valid)
		self.assertEqual(result.target_status, "refunded")
		# Refund webhook does not carry a Frappe intent name directly (we'd need
		# to follow the refund.transaction.id back; left to enrichment step).
		self.assertIsNone(result.intent_name)


class TestWalleeWebDriverRefund(FrappeTestCase):
	"""The hosted-page driver refunds as the terminal driver does: the same transaction, the same fields."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if not frappe.db.exists("DocType", "Wallee Settings"):
			raise cls.skipTest(cls, "Wallee Settings doctype absent: payments not installed on this site")
		_ensure_fixtures()

	def _driver(self):
		from payments.drivers.wallee.web_driver import WalleeWebDriver

		provider_doc = frappe.get_doc("Payment Provider", PROVIDER_NAME)
		return WalleeWebDriver.from_docs(provider_doc, None, None)

	def _refund_with(self, tx, amount=None):
		from payments.drivers.wallee.web_driver import WalleeWebDriver

		tx_service = MagicMock()
		tx_service.get_payment_transactions_id.return_value = tx
		refunds = _a_refunds_service()
		refunds.post_payment_refunds.return_value = _wallee_refund(state="SUCCESSFUL")
		with patch.object(WalleeWebDriver, "_services", return_value=(tx_service, refunds)):
			result = self._driver().refund("12345", amount=amount)
		return result, refunds

	def test_a_full_refund_gives_back_what_was_captured(self):
		result, refunds = self._refund_with(
			_wallee_tx(state="COMPLETED", authorization_amount=15.0, completed_amount=12.0)
		)

		self.assertEqual(result.status, "refunded")
		self.assertEqual(refunds.post_payment_refunds.call_args.args[1].amount, 12.0)

	def test_a_full_refund_of_an_authorized_transaction_gives_back_what_was_authorized(self):
		result, refunds = self._refund_with(_wallee_tx(state="COMPLETED", authorization_amount=9.5))

		self.assertEqual(result.status, "refunded")
		self.assertEqual(refunds.post_payment_refunds.call_args.args[1].amount, 9.5)

	def test_a_transaction_with_no_amount_is_refused_and_nothing_is_asked_of_wallee(self):
		result, refunds = self._refund_with(_wallee_tx(state="COMPLETED", authorization_amount=None))

		self.assertEqual((result.status, result.error_code), ("failed", "invalid_amount"))
		refunds.post_payment_refunds.assert_not_called()

	def test_a_partial_refund_names_its_amount_and_reads_nothing(self):
		result, refunds = self._refund_with(_wallee_tx(state="COMPLETED"), amount=500)

		self.assertEqual(result.status, "refunded")
		self.assertEqual(refunds.post_payment_refunds.call_args.args[1].amount, 5.0)
