# //// Neoffice — added file (no upstream equivalent). maintenance#2579: on the Payrexx hosted page a transaction is one
# //// attempt and the gateway (the payment link) is the intent. An attempt that expired or was cancelled moved the
# //// intent to `canceled`, which is final, while the link still took payments: a payment made on it afterwards was
# //// taken by Payrexx and refused here. The worker now reads the gateway before it follows such an attempt.
# Copyright (c) 2026, Neoffice and Contributors
# License: MIT. See LICENSE
"""A Payrexx attempt that ends does not end an intent whose payment link is still open."""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.roles import as_role

from payments.drivers.base import DriverResponse
from payments.drivers.payrexx.web_driver import PayrexxWebDriver
from payments.tests.test_audit_guards_p2 import (
	PAYREXX_A,
	PAYREXX_HANDLE,
	PAYREXX_KEYS,
	_WithIntents,
	hex_hmac,
	payrexx_event,
)


def the_gateway_says(status: str | None = None, error_code: str | None = None):
	"""What Payrexx answers when the link is read back (PayrexxWebDriver.get_status), without reaching it."""
	response = DriverResponse(status=status or "failed", error_code=error_code)
	return patch.object(PayrexxWebDriver, "get_status", return_value=response)


class TestAPayrexxAttemptDoesNotEndAnOpenLink(_WithIntents):
	def payrexx_delivers(self, intent: str, status: str) -> str:
		"""Payrexx delivers the transaction `status` of `intent`, signed; the worker applies it."""
		body = payrexx_event(intent, status)
		with patch("payments.api.webhook_payrexx._payrexx_provider_name", return_value=PAYREXX_A):
			delivery = self.deliver(
				PAYREXX_HANDLE, body, {"X-Webhook-Signature": hex_hmac(body, PAYREXX_KEYS[PAYREXX_A])}
			)
		log = self.queued_log(delivery)
		with as_role("Guest"):  # the provider's delivery has no session
			frappe.get_attr("payments.api.webhook_payrexx.process_event")(log)
		return log

	def test_an_expired_attempt_on_a_link_still_waiting_leaves_the_intent_open(self):
		intent = self.intent(PAYREXX_A, "payrexx_web")
		with the_gateway_says("requires_action") as read:
			log = self.payrexx_delivers(intent, "expired")
		read.assert_called_once()
		self.assertEqual(self.status_of(intent), "requires_action")
		self.assertEqual(frappe.db.get_value("Webhook Event Log", log, "status"), "Processed")
		self.assertTrue(
			frappe.db.exists("Payment Event", {"intent": intent, "to_status": "requires_action"}),
			"the attempt is recorded",
		)

	def test_a_payment_made_on_the_link_after_an_expired_attempt_is_taken(self):
		intent = self.intent(PAYREXX_A, "payrexx_web")
		with the_gateway_says("requires_action"):
			self.payrexx_delivers(intent, "expired")
		self.payrexx_delivers(intent, "confirmed")
		self.assertEqual(self.status_of(intent), "succeeded")

	def test_a_cancelled_attempt_on_a_link_still_waiting_leaves_the_intent_open(self):
		intent = self.intent(PAYREXX_A, "payrexx_web")
		with the_gateway_says("requires_action"):
			self.payrexx_delivers(intent, "cancelled")
		self.assertEqual(self.status_of(intent), "requires_action")

	def test_an_unreadable_link_moves_nothing(self):
		intent = self.intent(PAYREXX_A, "payrexx_web")
		with the_gateway_says(error_code="transport_error"):
			self.payrexx_delivers(intent, "expired")
		self.assertEqual(self.status_of(intent), "requires_action")

	def test_a_closed_link_cancels_the_intent(self):
		intent = self.intent(PAYREXX_A, "payrexx_web")
		with the_gateway_says("canceled"):
			self.payrexx_delivers(intent, "expired")
		self.assertEqual(self.status_of(intent), "canceled")

	def test_a_link_payrexx_no_longer_has_cancels_the_intent(self):
		intent = self.intent(PAYREXX_A, "payrexx_web")
		with the_gateway_says(error_code="NotFoundError"):
			self.payrexx_delivers(intent, "expired")
		self.assertEqual(self.status_of(intent), "canceled")
