# //// Neoffice — added file (no upstream equivalent). A TWINT order that has failed, or that nothing can read, never keeps its intent open
# //// (maintenance#3259: a customer's bank refused, the engine could not read the reason, and the intent was read again every minute for 17 hours).
# Copyright (c) 2026, Neoffice and Contributors
# License: MIT. See LICENSE
"""``payments.api.twint._process_one_intent``, with a fake driver: no TWINT, no database."""

import json
from datetime import timedelta
from unittest.mock import MagicMock, patch

from frappe.tests.utils import FrappeTestCase
from frappe.utils import now_datetime

from payments.api import twint as twint_api
from payments.drivers.twint.php_bridge_driver import TwintPHPBridgeDriver


class FakeDriver:
	"""Answers like the engine: `monitor` is what a read of the order gives; the real status map is the driver's own."""

	def __init__(self, monitor, cancel=None):
		self.monitor = monitor
		self.cancel = (
			cancel if cancel is not None else {"success": True, "transaction_status": "MERCHANT_ABORT"}
		)
		self.calls = []

	def _call_bridge(self, command, merchant_uuid, params):
		self.calls.append(command)
		if command == "monitor_status":
			return self.monitor
		if command == "cancel_payment":
			return self.cancel
		raise AssertionError(f"unexpected bridge command {command}")

	def _map_status(self, status):
		return TwintPHPBridgeDriver._map_status(self, status)


def _intent(minutes_old: int) -> dict:
	return {
		"name": "PI-TEST-3259",
		"provider": "twint_test",
		"channel": "twint_web",
		"amount": 24795,
		"provider_intent_id": "order-3259",
		"metadata_json": json.dumps({"twint_merchant_uuid": "merchant-3259"}),
		"created_at": now_datetime() - timedelta(minutes=minutes_old),
	}


class TestAFailedOrderClosesItsIntent(FrappeTestCase):
	def run_poll(self, intent: dict, monitor: dict):
		driver = FakeDriver(monitor)
		doc = MagicMock()
		doc.transition_to.return_value = True
		stats = {"checked": 0, "skipped": 0, "errors": 0, "advanced": 0}
		with (
			patch("payments.drivers.registry.resolve_driver", return_value=driver),
			patch.object(twint_api.frappe, "get_doc", return_value=doc),
			patch.object(twint_api.frappe, "log_error") as log_error,
			patch.object(twint_api, "_publish_intent_update") as publish,
		):
			twint_api._process_one_intent(intent, stats)
		return driver, doc, stats, log_error, publish

	def test_the_map_of_the_driver_reads_a_refusal_of_the_bank_as_a_failure(self):
		self.assertEqual(TwintPHPBridgeDriver._map_status(None, "ISSUER_ABORT"), "failed")
		self.assertEqual(TwintPHPBridgeDriver._map_status(None, "issuer_abort"), "failed")

	def test_a_refusal_of_the_bank_closes_the_intent_as_failed_at_once(self):
		_driver, doc, stats, _log, publish = self.run_poll(
			_intent(1), {"success": True, "order_status": "FAILURE", "transaction_status": "ISSUER_ABORT"}
		)

		doc.transition_to.assert_called_once()
		self.assertEqual(doc.transition_to.call_args.args, ("failed",))
		self.assertEqual(doc.transition_to.call_args.kwargs["error_code"], "twint_issuer_abort")
		self.assertEqual(stats["advanced"], 1)
		publish.assert_called_once()

	def test_a_failed_order_with_a_reason_nobody_mapped_is_still_a_failure(self):
		_driver, doc, stats, _log, _publish = self.run_poll(
			_intent(1),
			{"success": True, "order_status": "FAILURE", "transaction_status": "A_REASON_OF_NEXT_YEAR"},
		)

		self.assertEqual(doc.transition_to.call_args.args, ("failed",))
		self.assertEqual(doc.transition_to.call_args.kwargs["error_code"], "twint_a_reason_of_next_year")
		self.assertEqual(stats["advanced"], 1)

	def test_an_order_still_in_progress_with_a_reason_nobody_mapped_is_left_alone_while_young(self):
		driver, doc, stats, _log, _publish = self.run_poll(
			_intent(2),
			{"success": True, "order_status": "IN_PROGRESS", "transaction_status": "A_REASON_OF_NEXT_YEAR"},
		)

		doc.transition_to.assert_not_called()
		self.assertEqual(driver.calls, ["monitor_status"])
		self.assertEqual(stats["advanced"], 0)

	def test_a_paid_order_is_not_touched_by_the_failure_rule(self):
		_driver, doc, _stats, _log, _publish = self.run_poll(
			_intent(1), {"success": True, "order_status": "SUCCESS", "transaction_status": "ORDER_OK"}
		)

		self.assertEqual(doc.transition_to.call_args.args, ("succeeded",))
		self.assertNotIn("error_code", doc.transition_to.call_args.kwargs)

	def test_a_reason_that_says_paid_is_never_overruled_by_a_failed_status(self):
		_driver, doc, _stats, _log, _publish = self.run_poll(
			_intent(1), {"success": True, "order_status": "FAILURE", "transaction_status": "ORDER_OK"}
		)

		self.assertEqual(doc.transition_to.call_args.args, ("succeeded",))
		self.assertNotIn("error_code", doc.transition_to.call_args.kwargs)

	def test_an_answer_nothing_can_read_is_logged_and_the_intent_is_left_alone_while_young(self):
		driver, doc, stats, log_error, _publish = self.run_poll(
			_intent(2),
			{"success": False, "error": "unreadable", "exception": "Psl\\Type\\Exception\\AssertException"},
		)

		log_error.assert_called_once()
		doc.transition_to.assert_not_called()
		self.assertEqual(driver.calls, ["monitor_status"])
		self.assertEqual(stats["errors"], 1)

	def test_an_answer_nothing_can_read_no_longer_keeps_the_intent_open_for_ever(self):
		driver, doc, stats, _log, publish = self.run_poll(
			_intent(11),
			{"success": False, "error": "unreadable", "exception": "Psl\\Type\\Exception\\AssertException"},
		)

		self.assertEqual(driver.calls, ["monitor_status", "cancel_payment"])
		doc.transition_to.assert_called_once()
		self.assertEqual(doc.transition_to.call_args.args, ("canceled",))
		self.assertEqual(doc.transition_to.call_args.kwargs["error_code"], "twint_timeout")
		self.assertEqual(stats["advanced"], 1)
		publish.assert_called_once()
