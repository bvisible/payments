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
	def setUp(self):
		self.seen = {}
		self.fake_cache = MagicMock()
		self.fake_cache.get_value.side_effect = self.seen.get
		self.fake_cache.set_value.side_effect = lambda key, value, **_kw: self.seen.__setitem__(key, value)

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
			patch.object(twint_api.frappe, "cache", self.fake_cache),
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

	def test_a_failure_whose_reason_holds_the_word_success_is_not_a_payment(self):
		for reason in ("PAYMENT_UNSUCCESSFUL", "ISSUER_NOT_SUCCESS"):
			with self.subTest(reason=reason):
				_driver, doc, _stats, _log, _publish = self.run_poll(
					_intent(1), {"success": True, "order_status": "FAILURE", "transaction_status": reason}
				)

				self.assertEqual(doc.transition_to.call_args.args, ("failed",))

	def test_a_very_long_reason_is_bounded_in_the_error_code(self):
		_driver, doc, stats, _log, _publish = self.run_poll(
			_intent(1), {"success": True, "order_status": "FAILURE", "transaction_status": "X" * 300}
		)

		code = doc.transition_to.call_args.kwargs["error_code"]
		self.assertLessEqual(len(code), 140)
		self.assertTrue(code.startswith("twint_x"))
		self.assertEqual(stats["advanced"], 1)

	def test_what_twint_says_never_reaches_the_buyer_as_a_message(self):
		_driver, doc, _stats, _log, _publish = self.run_poll(
			_intent(1),
			{"success": True, "order_status": "FAILURE", "transaction_status": "<b onmouseover=x>"},
		)

		self.assertNotIn("error_message", doc.transition_to.call_args.kwargs)
		self.assertEqual(doc.transition_to.call_args.kwargs["error_code"], "twint_b_onmouseover_x")

	def test_a_paid_order_with_a_reason_nobody_mapped_is_paid_and_told_once(self):
		driver, doc, stats, log_error, _publish = self.run_poll(
			_intent(11),
			{"success": True, "order_status": "SUCCESS", "transaction_status": "A_REASON_OF_NEXT_YEAR"},
		)

		self.assertEqual(doc.transition_to.call_args.args, ("succeeded",))
		self.assertEqual(driver.calls, ["monitor_status"])  # no abort sent for money already taken
		self.assertNotIn("error_code", doc.transition_to.call_args.kwargs)
		log_error.assert_called_once()
		self.assertLessEqual(len(log_error.call_args.args[0]), 140)
		self.assertEqual(stats["advanced"], 1)

	def test_an_order_captured_for_less_than_asked_is_neither_cancelled_nor_closed_as_paid(self):
		driver, doc, stats, log_error, _publish = self.run_poll(
			_intent(11), {"success": True, "order_status": "SUCCESS", "transaction_status": "ORDER_PARTIAL_OK"}
		)

		doc.transition_to.assert_not_called()
		self.assertEqual(driver.calls, ["monitor_status"])
		log_error.assert_called_once()
		self.assertLessEqual(len(log_error.call_args.args[0]), 140)
		self.assertEqual(stats["errors"], 1)

	def test_the_held_order_is_reported_once_not_every_minute(self):
		answer = {"success": True, "order_status": "SUCCESS", "transaction_status": "ORDER_PARTIAL_OK"}
		_driver, _doc, _stats, first, _publish = self.run_poll(_intent(11), answer)
		_driver, _doc, _stats, second, _publish = self.run_poll(_intent(12), answer)

		first.assert_called_once()
		second.assert_not_called()

	def test_a_pass_that_lost_the_race_to_another_pass_leaves_the_intent_to_it(self):
		intent = _intent(1)
		driver = FakeDriver({"success": True, "order_status": "FAILURE", "transaction_status": "ISSUER_ABORT"})
		doc = MagicMock()
		doc.transition_to.side_effect = twint_api.frappe.TimestampMismatchError()
		stats = {"checked": 0, "skipped": 0, "errors": 0, "advanced": 0}
		with (
			patch("payments.drivers.registry.resolve_driver", return_value=driver),
			patch.object(twint_api.frappe, "get_doc", return_value=doc),
			patch.object(twint_api.frappe, "log_error") as log_error,
			patch.object(twint_api, "_publish_intent_update") as publish,
			patch.object(twint_api.frappe, "cache", self.fake_cache),
		):
			twint_api._process_one_intent(intent, stats)

		self.assertEqual((stats["skipped"], stats["advanced"], stats["errors"]), (1, 0, 0))
		log_error.assert_not_called()
		publish.assert_not_called()

	def test_an_intent_without_created_at_ages_by_its_creation_date(self):
		intent = _intent(11)
		intent["creation"] = intent["created_at"]
		intent["created_at"] = None
		driver, doc, _stats, _log, _publish = self.run_poll(
			intent,
			{"success": False, "error": "unreadable", "exception": "Psl\\Type\\Exception\\AssertException"},
		)

		self.assertEqual(driver.calls, ["monitor_status", "cancel_payment"])
		self.assertEqual(doc.transition_to.call_args.args, ("canceled",))

	def test_one_intent_that_raises_does_not_stop_the_ones_behind_it(self):
		first, second = _intent(1), _intent(1)
		first["name"], second["name"] = "PI-TEST-3259-A", "PI-TEST-3259-B"
		seen = []

		def process(intent, stats):
			seen.append(intent["name"])
			if intent["name"] == "PI-TEST-3259-A":
				raise ValueError("a field refused a value")

		with (
			patch.object(twint_api.frappe, "get_all", return_value=[first, second]),
			patch.object(twint_api, "_process_one_intent", side_effect=process),
			patch.object(twint_api, "_retry_unfinalized_web_orders"),
			patch.object(twint_api.frappe.db, "commit"),
			patch.object(twint_api.frappe, "log_error") as log_error,
		):
			stats = twint_api.poll_pending_twint_transactions()

		self.assertEqual(seen, ["PI-TEST-3259-A", "PI-TEST-3259-B"])
		self.assertEqual(stats["errors"], 1)
		log_error.assert_called_once()
		self.assertIn("PI-TEST-3259-A", log_error.call_args.args[1])

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
