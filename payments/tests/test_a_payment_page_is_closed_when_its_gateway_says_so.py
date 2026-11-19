# //// Neoffice — added file (no upstream equivalent). maintenance#2981: Wallee cannot close a transaction still waiting for its
# //// card, and its refusal was read as « already closed ». The intent went to `canceled` while the page took the payment, the
# //// webhook of that payment was refused by the state machine, and the money stayed at the gateway with nothing to give it back.
# Copyright (c) 2026, Neoffice and Contributors
# License: MIT. See LICENSE
"""A hosted payment page is closed when its gateway says so, never on the strength of having asked."""

from __future__ import annotations

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe

from payments.api import intent as intent_api
from payments.drivers.base import DriverResponse
from payments.drivers.wallee.web_driver import WalleeWebDriver
from payments.tests.test_audit_guards_p2 import WALLEE_OPEN, _WithIntents, ensure_channel

#: What Wallee answers, word for word, when it is asked to void a transaction still waiting for its card (sandbox, 19.11.2026)
REFUSAL = Exception(
	"The transaction can only be moved to VOIDED state when the transaction is in state AUTHORIZED."
)


class TestWhatWalleeSaysOfATransactionItWouldNotVoid(TestCase):
	def answer_for(self, state=None, unreadable=False):
		service = MagicMock()
		service.post_payment_transactions_id_void_online.side_effect = REFUSAL
		if unreadable:
			service.get_payment_transactions_id.side_effect = Exception("no answer")
		else:
			service.get_payment_transactions_id.return_value = SimpleNamespace(state=state)
		driver = WalleeWebDriver.__new__(WalleeWebDriver)
		with (
			patch.object(WalleeWebDriver, "_services", return_value=(service, None)),
			patch.object(WalleeWebDriver, "_wallee_provider", SimpleNamespace(space_id=1)),
		):
			return driver.cancel_intent("625790627")

	def test_a_transaction_waiting_for_its_card_is_not_called_closed(self):
		for state in ("CREATE", "PENDING", "CONFIRMED"):
			with self.subTest(state=state):
				self.assertEqual(self.answer_for(state).status, "requires_action")

	def test_a_transaction_being_processed_is_said_so(self):
		self.assertEqual(self.answer_for("PROCESSING").status, "processing")

	def test_a_transaction_paid_meanwhile_is_said_paid(self):
		for state in ("AUTHORIZED", "COMPLETED", "FULFILL"):
			with self.subTest(state=state):
				self.assertEqual(self.answer_for(state).status, "succeeded")

	def test_a_transaction_that_can_take_nothing_more_is_closed(self):
		for state in ("VOIDED", "FAILED", "DECLINE"):
			with self.subTest(state=state):
				self.assertEqual(self.answer_for(state).status, "canceled")

	def test_a_transaction_that_cannot_be_read_is_not_called_closed(self):
		self.assertEqual(self.answer_for(unreadable=True).status, "failed")


class TestAPageIsClosedWhenItsGatewaySaysSo(_WithIntents):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_channel("wallee_web")
		frappe.db.commit()

	def cancelled_with(self, channel, answer):
		name = self.intent(WALLEE_OPEN, channel)
		driver = MagicMock()
		driver.cancel_intent.return_value = answer
		with patch.object(intent_api, "resolve_driver", return_value=driver):
			result = intent_api.cancel_intent(name, reason="replaced_by_a_new_payment")
		driver.cancel_intent.assert_called_once()
		return name, result

	def test_a_page_the_gateway_keeps_open_stays_open(self):
		name, result = self.cancelled_with("wallee_web", DriverResponse(status="requires_action"))
		self.assertEqual(result["status"], "requires_action")
		self.assertEqual(self.status_of(name), "requires_action")
		self.assertFalse(frappe.db.exists("Payment Event", {"intent": name, "to_status": "canceled"}))

	def test_a_payment_made_on_that_page_afterwards_is_taken(self):
		name, _result = self.cancelled_with("wallee_web", DriverResponse(status="requires_action"))
		doc = frappe.get_doc("Payment Intent", name)
		self.assertTrue(doc.transition_to("succeeded", event_source="webhook", ignore_invalid=True))
		self.assertEqual(self.status_of(name), "succeeded")

	def test_a_page_paid_a_moment_ago_is_not_buried(self):
		name, _result = self.cancelled_with("wallee_web", DriverResponse(status="succeeded"))
		self.assertEqual(self.status_of(name), "requires_action", "the webhook or the poll settles it")

	def test_a_gateway_that_cannot_answer_leaves_the_page_open_and_a_trace(self):
		answer = DriverResponse(status="failed", error_code="wallee_void_failed")
		with patch.object(frappe, "log_error") as logged:
			name, _result = self.cancelled_with("wallee_web", answer)
		self.assertEqual(self.status_of(name), "requires_action")
		logged.assert_called_once()

	def test_a_page_the_gateway_closed_is_cancelled(self):
		name, result = self.cancelled_with("wallee_web", DriverResponse(status="canceled"))
		self.assertEqual(result["status"], "canceled")
		self.assertEqual(self.status_of(name), "canceled")

	def test_a_till_keeps_its_own_rule(self):
		name, result = self.cancelled_with("terminal", DriverResponse(status="failed"))
		self.assertEqual(result["status"], "canceled")
		self.assertEqual(self.status_of(name), "canceled")
