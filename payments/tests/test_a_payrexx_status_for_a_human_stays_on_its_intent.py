# //// Neoffice — added file (no upstream equivalent). maintenance#2722: a Payrexx status the Payment Intent FSM has no
# //// state for (a chargeback, a dispute) is kept as a Payment Event on its intent. The event was written with a field
# //// `payment_intent` the doctype does not have, and refused (its link `intent` is mandatory): the delivery ended
# //// « Failed » with no trace on the intent, at the very moment a human had to look.
# Copyright (c) 2026, Neoffice and Contributors
# License: MIT. See LICENSE
"""A Payrexx status that needs a human leaves a Payment Event on its intent."""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.roles import as_role

from payments.tests.test_audit_guards_p2 import (
	PAYREXX_A,
	PAYREXX_HANDLE,
	PAYREXX_KEYS,
	_WithIntents,
	hex_hmac,
	payrexx_event,
)


class TestAPayrexxStatusForAHumanStaysOnItsIntent(_WithIntents):
	def payrexx_delivers(self, intent: str, status: str) -> str:
		body = payrexx_event(intent, status)
		with patch("payments.api.webhook_payrexx._payrexx_provider_name", return_value=PAYREXX_A):
			delivery = self.deliver(
				PAYREXX_HANDLE, body, {"X-Webhook-Signature": hex_hmac(body, PAYREXX_KEYS[PAYREXX_A])}
			)
		log = self.queued_log(delivery)
		with (
			as_role("Guest"),
			patch.object(frappe, "log_error") as alert,
		):  # the provider's delivery has no session
			frappe.get_attr("payments.api.webhook_payrexx.process_event")(log)
		self.alert = alert
		return log

	def test_a_chargeback_is_kept_on_its_intent_and_said(self):
		intent = self.intent(PAYREXX_A, "payrexx_web")
		frappe.db.set_value("Payment Intent", intent, "status", "succeeded")
		frappe.db.commit()
		log = self.payrexx_delivers(intent, "chargeback")

		self.assertEqual(frappe.db.get_value("Webhook Event Log", log, "status"), "Processed")
		self.assertEqual(
			self.status_of(intent), "succeeded", "a chargeback is not a refund: the intent stays"
		)
		events = frappe.get_all(
			"Payment Event",
			filters={"intent": intent, "webhook_event_log": log},
			fields=["from_status", "to_status", "payload_excerpt"],
		)
		self.assertEqual(len(events), 1, "the status that needs a human is kept on its intent")
		self.assertEqual((events[0].from_status, events[0].to_status), ("succeeded", "succeeded"))
		self.assertTrue(events[0].payload_excerpt.startswith("payrexx.chargeback"))
		self.assertTrue(any("chargeback" in str(call.args[0]) for call in self.alert.call_args_list))
