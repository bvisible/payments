# Copyright (c) 2026, Neoffice and Contributors
# License: MIT. See LICENSE
"""Unit tests for the Payrexx Terminal (ECR) driver.

These tests do NOT reach a terminal — the ``payrexx`` client is replaced by a
fake that replays canned ECR responses. The live counterpart is
:mod:`payments.tests.payrexx_terminal_acceptance`, which drives a real NexGo.

The cancellation tests below encode behaviour observed on a NexGo N86 on
2026-08-21 and are the reason :meth:`cancel_intent` polls: the ECR reply carries
the payment's *current* state, never the outcome of the request, and a cancel
sent immediately after the payment request is silently dropped.
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from frappe.tests.utils import FrappeTestCase

from payments.drivers.payrexx.terminal_driver import (
	PayrexxCancelUnconfirmed,
	PayrexxTerminalDriver,
)

SERIAL = "N860W0W9677"
PAYMENT_ID = "291f6bdf-fa94-4e3d-83f0-6444750be7a9"


def _payment(status: str) -> SimpleNamespace:
	# `slip` and `receipt` are what get_status hands the till so it can print its own
	# document; empty here, but they must exist or the call blows up on an attribute.
	return SimpleNamespace(
		payment_id=PAYMENT_ID,
		status=status,
		raw={"status": status},
		slip=[],
		receipt=None,
	)


class _FakeEcr:
	"""Replays a scripted sequence of terminal states.

	``cancel_responses`` is what each ``cancel_payment`` call returns;
	``poll_states`` is what successive ``get_payment`` calls report. Both are
	consumed left to right, and the last entry repeats — so a test can say "it
	never settles" with a single trailing value.
	"""

	def __init__(self, cancel_responses: list[str], poll_states: list[str]) -> None:
		self._cancels = list(cancel_responses)
		self._polls = list(poll_states)
		self.cancel_calls = 0
		self.poll_calls = 0

	@staticmethod
	def _next(queue: list[str]) -> str:
		return queue.pop(0) if len(queue) > 1 else queue[0]

	def cancel_payment(self, serial_number: str, payment_id: str) -> SimpleNamespace:
		assert serial_number == SERIAL
		assert payment_id == PAYMENT_ID
		self.cancel_calls += 1
		return _payment(self._next(self._cancels))

	def get_payment(self, serial_number: str, payment_id: str) -> SimpleNamespace:
		self.poll_calls += 1
		return _payment(self._next(self._polls))


class _FakeClient:
	def __init__(self, ecr: _FakeEcr) -> None:
		self.ecr = ecr


class TestPayrexxTerminalCancel(FrappeTestCase):
	"""``cancel_intent`` must never claim a cancellation it cannot prove."""

	@contextmanager
	def _driver(self, ecr: _FakeEcr):
		driver = PayrexxTerminalDriver.__new__(PayrexxTerminalDriver)

		@contextmanager
		def _client(_self=None):
			yield _FakeClient(ecr)

		with (
			patch.object(PayrexxTerminalDriver, "_client", _client),
			patch.object(PayrexxTerminalDriver, "_serial", lambda _s, _d: SERIAL),
			patch.object(PayrexxTerminalDriver, "_device_for_intent", lambda _s, _i: "DEV-X"),
			# The real sleep would make this suite take half a minute.
			patch("payments.drivers.payrexx.terminal_driver.time.sleep", lambda _s: None),
		):
			yield driver

	def test_cancel_confirmed_when_terminal_settles(self):
		"""The happy path still reports ``canceled``."""
		ecr = _FakeEcr(cancel_responses=["IN_PROGRESS"], poll_states=["TERMINATED"])
		with self._driver(ecr) as driver:
			result = driver.cancel_intent(PAYMENT_ID)
		self.assertEqual(result.status, "canceled")
		self.assertIsNone(result.error_code)

	def test_swallowed_first_cancel_is_retried(self):
		"""A cancel the terminal ignores is sent again rather than believed.

		This is the observed NexGo behaviour: the first request leaves the payment
		live, the second terminates it.
		"""
		ecr = _FakeEcr(
			cancel_responses=["IN_PROGRESS"],
			poll_states=["IN_PROGRESS", "IN_PROGRESS", "TERMINATED"],
		)
		with self._driver(ecr) as driver:
			result = driver.cancel_intent(PAYMENT_ID)
		self.assertEqual(result.status, "canceled")
		self.assertGreaterEqual(ecr.cancel_calls, 2, "the cancel must be retried")

	def test_unconfirmed_cancel_raises_rather_than_returns(self):
		"""A terminal that never settles must not produce a ``canceled`` intent.

		It has to *raise*: ``payments.api.intent.cancel_intent`` records the
		cancellation on any returned response and only leaves the intent's status
		alone when the driver raises. Returning here is what would let a till drop
		an order off the screen while the terminal still takes the card.
		"""
		ecr = _FakeEcr(cancel_responses=["IN_PROGRESS"], poll_states=["IN_PROGRESS"])
		with self._driver(ecr) as driver:
			with self.assertRaises(PayrexxCancelUnconfirmed) as caught:
				driver.cancel_intent(PAYMENT_ID)
		self.assertIn(SERIAL, str(caught.exception))

	def test_settled_payment_rejecting_the_retry_still_confirms(self):
		"""A 400 on the retry is the race, not a failure.

		Once the payment settles, the cancel endpoint answers ``400 payment not in
		progress``. The poll — not the retry's reply — decides the outcome.
		"""

		class _RejectingEcr(_FakeEcr):
			def cancel_payment(self, serial_number, payment_id):
				self.cancel_calls += 1
				if self.cancel_calls > 1:
					raise RuntimeError("NAKA API Error (400): payment not in progress")
				return _payment("IN_PROGRESS")

		ecr = _RejectingEcr(
			cancel_responses=["IN_PROGRESS"],
			poll_states=["IN_PROGRESS", "IN_PROGRESS", "TERMINATED"],
		)
		with self._driver(ecr) as driver:
			result = driver.cancel_intent(PAYMENT_ID)
		self.assertEqual(result.status, "canceled")
		self.assertGreaterEqual(ecr.cancel_calls, 2)

	def test_already_settled_payment_needs_no_polling(self):
		"""Cancelling a finished payment answers immediately."""
		ecr = _FakeEcr(cancel_responses=["TERMINATED"], poll_states=["TERMINATED"])
		with self._driver(ecr) as driver:
			result = driver.cancel_intent(PAYMENT_ID)
		self.assertEqual(result.status, "canceled")
		self.assertEqual(ecr.poll_calls, 0, "no poll needed when the reply already settles")

	def test_simulated_payment_short_circuits(self):
		"""A simulated payment has no terminal to ask."""
		ecr = _FakeEcr(cancel_responses=[], poll_states=[])
		with self._driver(ecr) as driver:
			result = driver.cancel_intent("sim_PI-2026-00000001")
		self.assertEqual(result.status, "canceled")
		self.assertEqual(ecr.cancel_calls, 0)

	def test_payment_taken_while_cancelling_is_a_sale(self):
		"""If the customer taps mid-cancellation, that is a sale, not a cancellation.

		The reader admits ``SUCCESS`` only briefly before settling to ``TERMINATED``,
		so the cancellation loop has to notice it as it polls. Reporting ``canceled``
		here books money that was taken as an abandoned sale.
		"""
		ecr = _FakeEcr(
			cancel_responses=["IN_PROGRESS"],
			poll_states=["IN_PROGRESS", "SUCCESS", "TERMINATED"],
		)
		with self._driver(ecr) as driver:
			result = driver.cancel_intent(PAYMENT_ID)
		self.assertEqual(result.status, "succeeded")
		self.assertTrue(result.next_action_payload.get("raced_payment"))


class TestPayrexxTerminatedIsAmbiguous(FrappeTestCase):
	"""``TERMINATED`` must never be read as an outcome.

	Measured on a NexGo N86 on 2026-08-22: a card payment that completed and printed
	its receipt, and a payment cancelled from the till, are indistinguishable
	afterwards — same status, same type, same (absent) reversalStatus.
	"""

	def test_terminated_does_not_map_to_canceled(self):
		from payments.drivers.payrexx._common import NEEDS_HUMAN, map_status

		self.assertIsNone(map_status("TERMINATED"))
		self.assertIsNone(map_status("terminated"))
		self.assertIn("terminated", NEEDS_HUMAN)

	def test_get_status_keeps_a_terminated_payment_open(self):
		"""The till keeps waiting; the webhook decides."""
		ecr = _FakeEcr(cancel_responses=[], poll_states=["TERMINATED"])
		driver = PayrexxTerminalDriver.__new__(PayrexxTerminalDriver)

		@contextmanager
		def _client(_self=None):
			yield _FakeClient(ecr)

		with (
			patch.object(PayrexxTerminalDriver, "_client", _client),
			patch.object(PayrexxTerminalDriver, "_serial", lambda _s, _d: SERIAL),
			patch.object(PayrexxTerminalDriver, "_device_for_intent", lambda _s, _i: "DEV-X"),
		):
			result = driver.get_status(PAYMENT_ID, device_id="DEV-X")

		self.assertEqual(result.status, "processing")
		self.assertEqual(result.next_action_payload["terminal_status"], "TERMINATED")
