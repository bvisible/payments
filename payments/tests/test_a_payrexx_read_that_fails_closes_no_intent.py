# //// Neoffice — added file (no upstream equivalent). maintenance#2721: the Payrexx sweep (poll_pending_payrexx_transactions)
# //// read a driver's error as Payrexx's answer. A driver reports any error as `failed` with an error_code, so a timeout,
# //// a refused connection or a 5xx closed an intent the buyer may have paid. Only Payrexx's own status closes one now.
# Copyright (c) 2026, Neoffice and Contributors
# License: MIT. See LICENSE
"""A read of Payrexx that fails leaves the intent for the next pass; only Payrexx's answer closes it."""

from __future__ import annotations

import datetime
from unittest.mock import patch

import frappe

from payments.api import webhook_payrexx
from payments.drivers.base import DriverResponse
from payments.tests.test_audit_guards_p2 import PAYREXX_DRIVER, _WithIntents, ensure_provider

# The sweep reads the providers named payrexx*.
PROVIDER = "payrexx_auditpoll"


class _Reader:
	"""Stands in for the Payrexx driver: what get_status answers."""

	def __init__(self, response: DriverResponse):
		self.response = response
		self.asked = 0

	def get_status(self, provider_intent_id, **_kwargs):
		self.asked += 1
		return self.response


class TestAPayrexxReadThatFailsClosesNoIntent(_WithIntents):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_provider(PROVIDER, PAYREXX_DRIVER, "payrexx_web", {"webhook_signing_key": "auditpoll-key"})
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		frappe.db.delete("Provider Channel Settings", {"provider": PROVIDER})
		frappe.db.delete("Payment Provider", {"name": PROVIDER})
		frappe.db.commit()
		super().tearDownClass()

	def a_pending_intent(self, channel: str = "payrexx_web") -> str:
		name = self.intent(PROVIDER, channel)
		# the sweep reads only what has waited more than five minutes
		frappe.db.set_value(
			"Payment Intent",
			name,
			"modified",
			frappe.utils.now_datetime() - datetime.timedelta(minutes=10),
			update_modified=False,
		)
		frappe.db.commit()
		return name

	def sweep(self, response: DriverResponse) -> _Reader:
		"""One pass of the sweep over this test's intents only, Payrexx answering `response`."""
		reader = _Reader(response)
		real_get_all = frappe.get_all
		mine = list(self.intents)

		def only_mine(doctype, *args, **kwargs):
			if doctype == "Payment Intent" and "filters" in kwargs:
				kwargs = {**kwargs, "filters": {**kwargs["filters"], "name": ["in", mine]}}
			return real_get_all(doctype, *args, **kwargs)

		with (
			patch("payments.drivers.registry.resolve_driver", return_value=reader),
			patch.object(frappe, "get_all", side_effect=only_mine),
			patch.object(webhook_payrexx, "_retry_unfinalized_orders"),
			patch.object(frappe, "enqueue") as enqueue,
		):
			webhook_payrexx.poll_pending_payrexx_transactions()
		reader.enqueue = enqueue
		return reader

	def failed_read(self, error_code: str) -> DriverResponse:
		# what payrexx/_common.error_response hands back for any exception
		return DriverResponse(status="failed", error_code=error_code, error_message="no answer")

	def test_a_timeout_leaves_a_hosted_page_intent_waiting(self):
		intent = self.a_pending_intent()
		reader = self.sweep(self.failed_read("transport_error"))
		self.assertEqual(reader.asked, 1)
		self.assertEqual(self.status_of(intent), "requires_action")

	def test_a_server_error_leaves_a_hosted_page_intent_waiting(self):
		intent = self.a_pending_intent()
		self.sweep(self.failed_read("ServerError"))
		self.assertEqual(self.status_of(intent), "requires_action")

	def test_a_timeout_leaves_a_terminal_payment_waiting_and_the_reader_alone(self):
		intent = self.a_pending_intent("terminal")
		reader = self.sweep(self.failed_read("transport_error"))
		self.assertEqual(self.status_of(intent), "requires_action")
		reader.enqueue.assert_not_called()  # nobody cancels a payment whose state was not read

	def test_payrexx_saying_failed_fails_the_intent(self):
		intent = self.a_pending_intent()
		self.sweep(DriverResponse(status="failed"))
		self.assertEqual(self.status_of(intent), "failed")

	def test_payrexx_saying_expired_cancels_the_intent(self):
		intent = self.a_pending_intent()
		self.sweep(DriverResponse(status="canceled"))
		self.assertEqual(self.status_of(intent), "canceled")

	def test_a_payment_page_payrexx_no_longer_has_is_canceled(self):
		intent = self.a_pending_intent()
		self.sweep(self.failed_read("NotFoundError"))
		self.assertEqual(self.status_of(intent), "canceled")
