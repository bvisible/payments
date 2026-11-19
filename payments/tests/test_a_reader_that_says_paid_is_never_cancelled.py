# //// Neoffice — added file (no upstream equivalent). maintenance#3017: a till's cancellation was recorded whatever the reader
# //// answered, so a card tapped during the cancellation (Payrexx) or a Tap to Pay payment that already existed ended
# //// `canceled`, and money taken stayed unbooked.
# Copyright (c) 2026, Neoffice and Contributors
# License: MIT. See LICENSE
"""A reader that answers the payment went through is never marked cancelled: the provider is read again, and paid is booked."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe

from payments.api import intent as intent_api
from payments.drivers.base import DriverResponse
from payments.tests.test_audit_guards_p2 import WALLEE_OPEN, _WithIntents, ensure_channel

#: What the Payrexx reader's driver answers when the terminal reports « SUCCESS » (one of the nine payment statuses Payrexx
#: confirmed in writing on 18.08.2026) while the cancellation is on its way (drivers/payrexx/terminal_driver.py)
PAYREXX_TAPPED = DriverResponse(
	status="succeeded",
	provider_intent_id="2026111901",
	next_action_payload={"terminal_status": "SUCCESS", "raced_payment": True},
)
#: What Tap to Pay answers when the payment exists at Payrexx (drivers/payrexx/tap_to_pay_driver.py): its mapped status
TAP_TO_PAY_EXISTS = {
	"status": "succeeded",
	"provider_intent_id": "777",
	"error_code": "cancel_not_possible",
	"error_message": "This Tap to Pay payment already exists at Payrexx (transaction 777).",
}


class TestAReaderThatSaysPaidIsNeverCancelled(_WithIntents):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_channel("payrexx_tap_to_pay")
		frappe.db.commit()

	def cancelled(self, channel, answer, now):
		name = self.intent(WALLEE_OPEN, channel)
		driver = MagicMock()
		driver.cancel_intent.return_value = answer
		if isinstance(now, Exception):
			driver.get_status.side_effect = now
		else:
			driver.get_status.return_value = DriverResponse(status=now)
		with (
			patch.object(intent_api, "resolve_driver", return_value=driver),
			patch.object(intent_api.frappe, "enqueue"),
		):
			result = intent_api.cancel_intent(name, reason="cashier")
		return name, result, driver

	def test_a_card_tapped_while_the_payrexx_reader_was_cancelled_is_booked(self):
		name, result, driver = self.cancelled("terminal", PAYREXX_TAPPED, "succeeded")
		self.assertEqual(self.status_of(name), "succeeded", "money taken, never buried as cancelled")
		self.assertEqual(result["status"], "succeeded", "the till reads that it was paid")
		driver.get_status.assert_called_once()

	def test_a_tap_to_pay_payment_that_exists_and_is_paid_is_booked(self):
		name, _result, _driver = self.cancelled(
			"payrexx_tap_to_pay", DriverResponse(**TAP_TO_PAY_EXISTS), "succeeded"
		)
		self.assertEqual(self.status_of(name), "succeeded")

	def test_a_tap_to_pay_payment_still_on_its_way_is_not_cancelled(self):
		answer = DriverResponse(**{**TAP_TO_PAY_EXISTS, "status": "processing"})
		name, _result, _driver = self.cancelled("payrexx_tap_to_pay", answer, "processing")
		self.assertEqual(self.status_of(name), "requires_action", "the webhook or the poll says how it ended")

	def test_a_payment_the_provider_cannot_be_asked_about_is_neither_cancelled_nor_booked(self):
		with patch.object(intent_api.frappe, "log_error") as logged:
			name, _result, _driver = self.cancelled("terminal", PAYREXX_TAPPED, RuntimeError("no answer"))
		self.assertEqual(self.status_of(name), "requires_action")
		logged.assert_called_once()

	def test_a_cancellation_the_reader_took_is_still_a_cancellation(self):
		name, _result, driver = self.cancelled("terminal", DriverResponse(status="canceled"), "canceled")
		self.assertEqual(self.status_of(name), "canceled")
		driver.get_status.assert_not_called()

	def test_a_failed_cancellation_keeps_the_tills_rule(self):
		name, _result, driver = self.cancelled("terminal", DriverResponse(status="failed"), "failed")
		self.assertEqual(self.status_of(name), "canceled")
		driver.get_status.assert_not_called()
