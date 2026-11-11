# //// Neoffice — added file (no upstream equivalent). Audit of the payments app, decision Q11 (maintenance#2296): a
# //// Stripe event whose job never finished is taken up again, then reported; a replay books nothing twice.
# Copyright (c) 2026, Neoffice and Contributors
# License: MIT. See LICENSE
"""A Stripe event the ERP stored and never applied is queued again every five minutes, five times at most, then the
managers hear of it once. Stripe was answered « ok » and never sends it again: without this, a card charged at the
terminal stays a sale unpaid at the till, and the cashier takes it again.

Nothing reaches Stripe: every signature is built with the algorithm the driver checks, from a secret the test made up,
and the queue is patched (the job the sweep queues is run by hand, as the worker would).
"""

from __future__ import annotations

import json
from unittest.mock import patch

import frappe
from frappe.tests.roles import as_role, make_persona
from frappe.utils import add_to_date, flt, now_datetime, nowdate

from payments.tests.test_audit_guards_p2 import (
	PAYREXX_A,
	STRIPE_A,
	STRIPE_HANDLE,
	STRIPE_SECRETS,
	_WithIntents,
	make_newest,
	stripe_event,
	stripe_signature,
)
from payments.tests.test_intent_api import _ensure_mock_setup

SWEEP = "payments.api.webhook_stripe.retry_lost_events"
WORKER = "payments.api.webhook_stripe.process_event"


class TestALostStripeEventIsTakenUpAgain(_WithIntents):
	def setUp(self):
		super().setUp()
		make_newest(STRIPE_A)
		mail = patch("frappe.sendmail")
		mail.start()
		self.addCleanup(mail.stop)
		self.si = None
		self.addCleanup(self.forget_the_reports)

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		if self.si:
			for pe in frappe.get_all(
				"Payment Entry Reference", filters={"reference_name": self.si.name}, pluck="parent"
			):
				doc = frappe.get_doc("Payment Entry", pe)
				if doc.docstatus == 1:
					doc.cancel()
				frappe.delete_doc("Payment Entry", pe, force=True, ignore_permissions=True)
			for name in self.intents:
				frappe.db.delete("Payment Event", {"intent": name})
				frappe.delete_doc("Payment Intent", name, force=True, ignore_permissions=True)
			self.intents = []
			self.si.reload()
			if self.si.docstatus == 1:
				self.si.cancel()
			frappe.delete_doc("Sales Invoice", self.si.name, force=True, ignore_permissions=True)
			frappe.db.commit()
		super().tearDown()

	def forget_the_reports(self):
		frappe.db.delete("Notification Log", {"document_type": "Webhook Event Log"})
		frappe.db.delete("Error Log", {"method": ["like", "%Stripe%"]})
		frappe.db.commit()

	# --- the scenery ---------------------------------------------------------------------------------------------

	def a_lost_delivery(self, intent: str) -> str:
		"""Stripe delivers the event and is answered « ok »; the job that should apply it is lost (a restart)."""
		body = stripe_event(intent)
		log = self.queued_log(
			self.deliver(
				STRIPE_HANDLE, body, {"Stripe-Signature": stripe_signature(body, STRIPE_SECRETS[STRIPE_A])}
			)
		)
		frappe.db.commit()
		self.assertEqual(frappe.db.get_value("Webhook Event Log", log, "status"), "Queued")
		return log

	def age(self, log: str, minutes: int = 11, **values) -> None:
		"""The row was last written `minutes` ago."""
		frappe.db.set_value(
			"Webhook Event Log",
			log,
			{"modified": add_to_date(now_datetime(), minutes=-minutes), **values},
			update_modified=False,
		)
		frappe.db.commit()

	def sweep(self):
		with patch("frappe.enqueue") as enqueue:
			result = frappe.get_attr(SWEEP)()
		return result, enqueue

	def run_worker(self, log: str) -> None:
		with as_role("Guest"):  # the worker of a provider's delivery has no session
			frappe.get_attr(WORKER)(log)
		frappe.db.commit()  # the job ends: what it wrote is committed

	def row(self, log: str):
		return frappe.db.get_value(
			"Webhook Event Log", log, ["status", "retries", "alerted_at"], as_dict=True
		)

	def an_invoice(self):
		"""An invoice the office raised and sent; the card is taken at the counter, on a Stripe terminal."""
		_ensure_mock_setup()
		company = frappe.defaults.get_global_default("company")
		currency = company and frappe.get_cached_value("Company", company, "default_currency")
		accounts = company and frappe.get_all(
			"Mode of Payment Account",
			filters={"default_account": ["is", "set"], "parenttype": "Mode of Payment", "company": company},
			fields=["parent", "company", "default_account"],
			order_by="parent",
		)
		mop = next(
			(
				row
				for row in accounts or []
				if frappe.db.get_value("Account", row.default_account, "account_currency") == currency
			),
			None,
		)
		customers = frappe.get_all(
			"Customer",
			filters={"disabled": 0},
			or_filters=[["default_currency", "is", "not set"], ["default_currency", "=", currency]]
			if currency
			else None,
			pluck="name",
			order_by="name",
			limit=1,
		)
		item = frappe.db.get_value(
			"Item", {"disabled": 0, "is_stock_item": 0, "is_sales_item": 1, "has_variants": 0}, "name"
		)
		if not (mop and customers and item):
			self.skipTest("a mode of payment with an account, a customer and a service item are needed")
		with as_role("head_accountant"):
			self.si = frappe.get_doc(
				{
					"doctype": "Sales Invoice",
					"company": mop.company,
					"customer": customers[0],
					"posting_date": nowdate(),
					"due_date": nowdate(),
					"items": [{"item_code": item, "qty": 1, "rate": 84.5}],
				}
			)
			self.si.insert()
			self.si.submit()
		frappe.db.commit()
		return mop.parent

	# --- the sweep -------------------------------------------------------------------------------------------------

	# role-check: ok the sweep is a scheduled job, which runs as Administrator
	def test_a_row_its_job_never_finished_is_queued_again_after_ten_minutes(self):
		log = self.a_lost_delivery(self.intent(STRIPE_A))
		# Its job may still be on its way: not before ten minutes
		result, enqueue = self.sweep()
		self.assertNotIn(log, result["queued"])
		self.age(log)
		result, enqueue = self.sweep()
		self.assertIn(log, result["queued"])
		enqueue.assert_any_call(
			WORKER,
			queue="short",
			job_id=f"stripe-webhook-{log}",
			deduplicate=True,
			enqueue_after_commit=True,
			log_name=log,
		)
		self.assertEqual(self.row(log).retries, 1)
		# Counted, it waits ten minutes again
		result, _ = self.sweep()
		self.assertNotIn(log, result["queued"])

	# role-check: ok the sweep is a scheduled job, which runs as Administrator
	def test_a_row_that_failed_is_queued_again_too(self):
		log = self.a_lost_delivery(self.intent(STRIPE_A))
		self.age(log, status="Failed", error="TimestampMismatchError('stale copy')")
		result, _ = self.sweep()
		self.assertIn(log, result["queued"])

	# role-check: ok the sweep is a scheduled job, which runs as Administrator
	def test_a_replay_books_the_payment_once(self):
		mode = self.an_invoice()
		amount = int(round(flt(self.si.outstanding_amount) * 100))
		intent = self.intent(
			STRIPE_A,
			amount=amount,
			currency=self.si.currency,
			reference_doctype="Sales Invoice",
			reference_name=self.si.name,
			metadata_json=json.dumps({"mode_of_payment": mode}),
		)
		log = self.a_lost_delivery(intent)
		self.age(log)
		result, _ = self.sweep()
		self.assertIn(log, result["queued"])
		# The queue runs the job, and runs it again (a retry of Stripe and the sweep both queued it)
		self.run_worker(log)
		self.run_worker(log)
		self.assertEqual(self.status_of(intent), "succeeded")
		self.assertEqual(self.row(log).status, "Processed")
		self.assertEqual(
			frappe.db.count("Payment Event", {"intent": intent, "to_status": "succeeded"}),
			1,
			"a second move",
		)
		reference = frappe.db.get_value("Payment Intent", intent, "provider_intent_id")
		entries = frappe.get_all("Payment Entry", filters={"reference_no": reference}, pluck="docstatus")
		self.assertEqual(entries, [1], "the replay booked the payment twice")
		self.assertEqual(
			flt(frappe.db.get_value("Sales Invoice", self.si.name, "outstanding_amount")), 0
		)
		# Applied, it is never taken up again
		self.age(log)
		result, _ = self.sweep()
		self.assertNotIn(log, result["queued"] + result["reported"])

	# role-check: ok the sweep is a scheduled job, which runs as Administrator
	def test_after_five_tries_the_managers_hear_of_it_once(self):
		manager = make_persona("head_accountant")
		intent = self.intent(STRIPE_A, amount=8450)
		log = self.a_lost_delivery(intent)
		self.age(log, status="Failed", retries=5)
		result, enqueue = self.sweep()
		self.assertNotIn(log, result["queued"])
		self.assertIn(log, result["reported"])
		self.assertTrue(self.row(log).alerted_at)
		told = frappe.get_all(
			"Notification Log",
			filters={"for_user": manager, "document_type": "Webhook Event Log"},
			fields=["subject", "email_content"],
		)
		self.assertEqual(len(told), 1)
		self.assertIn("Stripe", told[0].subject)
		self.assertIn(intent, told[0].email_content)
		self.assertIn("84.50 CHF", told[0].email_content)
		self.assertTrue(frappe.db.exists("Error Log", {"method": told[0].subject[:140]}))
		# Once: the next sweep says nothing more of it
		self.age(log, status="Failed", retries=5)
		result, _ = self.sweep()
		self.assertNotIn(log, result["queued"] + result["reported"])
		self.assertEqual(
			frappe.db.count(
				"Notification Log", {"for_user": manager, "document_type": "Webhook Event Log"}
			),
			1,
		)

	# role-check: ok the sweep is a scheduled job, which runs as Administrator
	def test_an_old_row_is_reported_and_never_replayed(self):
		log = self.a_lost_delivery(self.intent(STRIPE_A))
		frappe.db.set_value(
			"Webhook Event Log",
			log,
			{
				"creation": add_to_date(now_datetime(), days=-4),
				"modified": add_to_date(now_datetime(), days=-4),
			},
			update_modified=False,
		)
		frappe.db.commit()
		result, _ = self.sweep()
		self.assertNotIn(log, result["queued"])
		self.assertIn(log, result["reported"])

	# role-check: ok the sweep is a scheduled job, which runs as Administrator
	def test_another_providers_row_is_left_to_its_own_poll(self):
		frappe.get_doc(
			{
				"doctype": "Webhook Event Log",
				"event_id": "auditp2-payrexx-lost",
				"provider": PAYREXX_A,
				"event_type": "transaction",
				"status": "Queued",
				"signature_valid": 1,
			}
		).insert(ignore_permissions=True)
		self.age("auditp2-payrexx-lost")
		result, _ = self.sweep()
		self.assertNotIn("auditp2-payrexx-lost", result["queued"] + result["reported"])

	def test_the_sweep_is_no_web_method(self):
		with self.assertRaises(frappe.PermissionError):
			frappe.is_whitelisted(frappe.get_attr(SWEEP))
		self.assertIn(SWEEP, frappe.get_hooks("scheduler_events")["cron"]["*/5 * * * *"])
