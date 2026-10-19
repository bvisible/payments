# //// Neoffice — added file (no upstream equivalent). The TWINT poller's timeout: an intent with no final status
# //// after 10 minutes has its order aborted at TWINT before it is canceled here (maintenance#1734).
# Copyright (c) 2026, Neoffice and Contributors
# License: MIT. See LICENSE
"""The timeout path of ``payments.api.twint._process_one_intent``, with a fake driver: no TWINT, no database."""

import json
from datetime import timedelta
from unittest.mock import MagicMock, patch

from frappe.tests.utils import FrappeTestCase
from frappe.utils import now_datetime

from payments.api import twint as twint_api


class FakeDriver:
	"""Answers like the bridge for an order TWINT still reads as received (not paired, not paid)."""

	def __init__(self, cancel_answer):
		self.cancel_answer = cancel_answer
		self.calls = []

	def _call_bridge(self, command, merchant_uuid, params):
		self.calls.append((command, merchant_uuid, params))
		if command == "monitor_status":
			return {"success": True, "transaction_status": "ORDER_RECEIVED"}
		if command == "cancel_payment":
			if isinstance(self.cancel_answer, Exception):
				raise self.cancel_answer
			return self.cancel_answer
		raise AssertionError(f"unexpected bridge command {command}")

	def _map_status(self, status):
		return "processing"


def _intent(minutes_old: int) -> dict:
	return {
		"name": "PI-TEST-1734",
		"provider": "twint_test",
		"channel": "twint_web",
		"amount": 50,
		"provider_intent_id": "order-1734",
		"metadata_json": json.dumps({"twint_merchant_uuid": "merchant-1734"}),
		"created_at": now_datetime() - timedelta(minutes=minutes_old),
	}


class TestTwintPollTimeout(FrappeTestCase):
	def run_poll(self, intent: dict, cancel_answer):
		driver = FakeDriver(cancel_answer)
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

	def commands(self, driver) -> list[str]:
		return [command for command, _merchant, _params in driver.calls]

	def test_a_timed_out_order_is_aborted_at_twint_then_the_intent_canceled(self):
		driver, doc, stats, _log, publish = self.run_poll(
			_intent(11), {"success": True, "transaction_status": "MERCHANT_ABORT"}
		)
		self.assertEqual(self.commands(driver), ["monitor_status", "cancel_payment"])
		self.assertEqual(driver.calls[1][1:], ("merchant-1734", {"order_id": "order-1734"}))
		doc.transition_to.assert_called_once()
		self.assertEqual(doc.transition_to.call_args.args, ("canceled",))
		self.assertEqual(doc.transition_to.call_args.kwargs["error_code"], "twint_timeout")
		self.assertEqual(stats["advanced"], 1)
		publish.assert_called_once()

	def test_an_abort_twint_refuses_leaves_the_intent_for_the_next_run(self):
		driver, doc, stats, log_error, publish = self.run_poll(
			_intent(11), {"success": False, "error": "Status transition error", "exception": "ApiFailure"}
		)
		self.assertEqual(self.commands(driver), ["monitor_status", "cancel_payment"])
		doc.transition_to.assert_not_called()
		publish.assert_not_called()
		self.assertEqual(stats["errors"], 1)
		log_error.assert_called_once()

	def test_an_abort_that_cannot_reach_twint_counts_as_refused(self):
		driver, doc, _stats, _log, _publish = self.run_poll(_intent(11), ConnectionError("bridge down"))
		self.assertEqual(self.commands(driver), ["monitor_status", "cancel_payment"])
		doc.transition_to.assert_not_called()

	def test_after_an_hour_of_refusals_the_intent_is_canceled_anyway(self):
		driver, doc, _stats, _log, publish = self.run_poll(
			_intent(61), {"success": False, "error": "refused"}
		)
		self.assertEqual(self.commands(driver), ["monitor_status", "cancel_payment"])
		doc.transition_to.assert_called_once()
		publish.assert_called_once()

	def test_an_order_younger_than_ten_minutes_is_left_alone(self):
		driver, doc, stats, _log, _publish = self.run_poll(_intent(2), {"success": True})
		self.assertEqual(self.commands(driver), ["monitor_status"])
		doc.transition_to.assert_not_called()
		self.assertEqual(stats["advanced"], 0)
