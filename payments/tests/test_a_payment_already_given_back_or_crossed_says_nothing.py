# //// Neoffice — added file (no upstream equivalent). maintenance#3019: a night of tests on osiris left 33 rows for webhooks of
# //// payments already given back, one per second payment given back, and one per poll that crossed a webhook. One row per
# //// case the office has to act on: money nobody booked or gave back.
# Copyright (c) 2026, Neoffice and Contributors
# License: MIT. See LICENSE
"""One Error Log row per real case, not one per payment."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe

from payments.api import reconciliation
from payments.drivers.wallee import terminal_driver
from payments.tests.test_audit_guards_p2 import WALLEE_OPEN, _WithIntents, ensure_channel


class TestOneRowPerRealCase(_WithIntents):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_channel("wallee_web")
		frappe.db.commit()

	def rows_about(self, name):
		return frappe.get_all("Error Log", filters={"error": ["like", f"%{name}%"]}, pluck="method")

	def test_a_webhook_of_a_payment_already_given_back_writes_nothing(self):
		name = self.intent(WALLEE_OPEN, "wallee_web")
		frappe.db.set_value("Payment Intent", name, "status", "refunded", update_modified=False)
		doc = frappe.get_doc("Payment Intent", name)
		self.assertFalse(doc.transition_to("succeeded", event_source="webhook", ignore_invalid=True))
		self.assertEqual(self.status_of(name), "refunded")
		self.assertEqual(self.rows_about(name), [])

	def test_a_payment_on_a_cancelled_intent_is_still_written(self):
		"""The alarm that found maintenance#2981: money taken on an intent called closed."""
		name = self.intent(WALLEE_OPEN, "wallee_web")
		frappe.db.set_value("Payment Intent", name, "status", "canceled", update_modified=False)
		doc = frappe.get_doc("Payment Intent", name)
		self.assertFalse(doc.transition_to("succeeded", event_source="webhook", ignore_invalid=True))
		self.assertEqual(self.rows_about(name), ["Payment Intent: rejected FSM transition"])

	def test_a_payment_left_over_is_said_once_and_only_when_kept(self):
		kept = self.intent(WALLEE_OPEN, "wallee_web")
		frappe.db.set_value("Payment Intent", kept, "status", "succeeded", update_modified=False)
		given_back = self.intent(WALLEE_OPEN, "wallee_web")
		frappe.db.set_value("Payment Intent", given_back, "status", "refunded", update_modified=False)
		reconciliation.report_a_payment_left_over(kept, "FA-TEST", "ref-kept")
		reconciliation.report_a_payment_left_over(given_back, "FA-TEST", "ref-given-back")
		self.assertEqual(self.rows_about(kept), ["Card payment for an invoice already paid"])
		self.assertEqual(self.rows_about(given_back), [], "given back: no case for the office")

	def test_a_poll_that_crosses_a_webhook_writes_nothing(self):
		row = frappe._dict(
			name="PI-CROSSED",
			provider="wallee_test",
			channel="wallee_web",
			provider_intent_id="777",
			reference_doctype=None,
			reference_name=None,
		)
		driver = MagicMock()
		driver._services.return_value = (
			MagicMock(**{"get_payment_transactions_id.return_value": SimpleNamespace(state="COMPLETED")}),
		)
		driver._wallee_provider = SimpleNamespace(space_id=1)
		crossed = MagicMock(status="requires_action")
		crossed.transition_to.side_effect = frappe.TimestampMismatchError("modified after you opened it")
		lists = [[frappe._dict(name="wallee_test")], [row]]
		with (
			patch.object(
				terminal_driver.frappe, "get_all", side_effect=lambda *a, **k: lists.pop(0) if lists else []
			),
			patch("payments.drivers.registry.resolve_driver", return_value=driver),
			patch.object(terminal_driver.frappe, "get_doc", return_value=crossed),
			patch.object(terminal_driver.frappe, "log_error") as logged,
		):
			stats = terminal_driver.poll_pending_transactions()
		crossed.transition_to.assert_called_once()
		logged.assert_not_called()
		self.assertEqual(stats["errors"], 0)
