# //// Neoffice — added file (no upstream equivalent). The reconciliation doc_event of a Payment Intent lets a
# //// lock error through instead of turning the rollback it caused into a silent success (maintenance#1831).
# Copyright (c) 2026, Neoffice and Contributors
# License: MIT. See LICENSE
"""A deadlock makes InnoDB roll the whole transaction back, the write that triggered the doc_event included.

`on_payment_intent_after_update` never breaks the save for a booking that fails (it logs it, and the booking is undone
by `all_or_nothing`). A lock error is another thing: nothing of the save is left, so swallowing it made the request answer
normally for a payment that had not been written. It goes up now, and the caller (a webhook the provider resends, a
poller's next pass, the till) does it again.

The booking itself is patched: the point is what the doc_event does with what the booking raises.
"""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from payments.api import reconciliation


def a_succeeded_intent():
	return frappe._dict(name="PI-TEST-1831", status="succeeded", docstatus=1)


class TestALockErrorGoesUp(FrappeTestCase):
	def test_a_deadlock_reaches_the_caller(self):
		with (
			patch.object(
				reconciliation, "reconcile_payment_intent", side_effect=frappe.QueryDeadlockError("1213")
			),
			patch("frappe.log_error"),
		):
			with self.assertRaises(frappe.QueryDeadlockError):
				reconciliation.on_payment_intent_after_update(a_succeeded_intent())

	def test_a_lock_wait_timeout_reaches_the_caller(self):
		with (
			patch.object(
				reconciliation, "reconcile_payment_intent", side_effect=frappe.QueryTimeoutError("1205")
			),
			patch("frappe.log_error"),
		):
			with self.assertRaises(frappe.QueryTimeoutError):
				reconciliation.on_payment_intent_after_update(a_succeeded_intent())

	def test_the_savepoint_that_a_deadlock_ended_is_not_left_to_hide_the_error(self):
		"""`all_or_nothing` finds its savepoint gone after a deadlock and says so; the deadlock still goes up."""
		with (
			patch.object(
				reconciliation, "reconcile_payment_intent", side_effect=frappe.QueryDeadlockError("1213")
			),
			patch.object(frappe.db, "rollback", side_effect=Exception("SAVEPOINT does not exist")),
			patch("frappe.log_error") as log_error,
		):
			with self.assertRaises(frappe.QueryDeadlockError):
				reconciliation.on_payment_intent_after_update(a_succeeded_intent())
		self.assertEqual(log_error.call_args.args[0], "Payment booking could not be undone")


class TestAnyOtherFailureStillDoesNotBreakTheSave(FrappeTestCase):
	def test_a_booking_that_fails_is_logged_and_the_save_goes_on(self):
		with (
			patch.object(
				reconciliation, "reconcile_payment_intent", side_effect=ValueError("frozen books")
			),
			patch("frappe.log_error") as log_error,
		):
			reconciliation.on_payment_intent_after_update(a_succeeded_intent())  # does not raise
		log_error.assert_called_once()
		self.assertEqual(log_error.call_args.args[0], "reconcile_payment_intent failed in doc_event")
		self.assertIn("frozen books", log_error.call_args.args[1])

	def test_an_intent_that_is_not_succeeded_is_not_reconciled(self):
		intent = frappe._dict(name="PI-TEST-1831", status="processing", docstatus=1)
		with patch.object(reconciliation, "reconcile_payment_intent") as reconcile:
			reconciliation.on_payment_intent_after_update(intent)
		reconcile.assert_not_called()
