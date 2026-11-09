# //// Neoffice — added file (no upstream equivalent). See payments/utils/legacy_payment_records.py.
"""The payment records of the retired apps are read as they were, copied once, never deleted (#2390)."""

import json
from datetime import datetime

import frappe
from frappe.tests.utils import FrappeTestCase

from payments.utils import legacy_payment_records as legacy

CAPTURED = {
	"name": "st-test-0001",
	"status": "Captured",
	"amount": 24.0,
	"currency": "chf",
	"payment_intent_id": "pi_test_captured",
	"terminal": "Reader",
	"stripe_settings": "Stripe-Card",
	"stripe_created": "2026-01-27 16:39:17",
	"stripe_captured": "2026-01-27 16:39:30",
	"creation": "2026-01-27 16:39:17",
	"modified": "2026-01-27 16:39:31",
}
TICKET = {"name": "POS-TEST-0001", "customer": "Walk-in", "consolidated_invoice": "SINV-TEST-0001"}


class TestTheReadingOfALegacyRow(FrappeTestCase):
	def test_amounts_are_read_in_rappen(self):
		self.assertEqual(legacy.to_minor(12.5), 1250)
		self.assertEqual(legacy.to_minor("CHF 125.000000000"), 12500)
		self.assertEqual(legacy.to_minor("0.005"), 1)
		self.assertEqual(legacy.to_minor(None), 0)
		self.assertEqual(legacy.to_minor("not a number"), 0)

	def test_a_stripe_row_says_what_became_of_the_payment(self):
		self.assertEqual(legacy.stripe_status({"status": "Captured"}), ("succeeded", None))
		self.assertEqual(
			legacy.stripe_status({"status": "Captured", "stripe_amount_refunded": 24}), ("refunded", None)
		)
		self.assertEqual(legacy.stripe_status({"status": "Cancelled"}), ("canceled", None))
		self.assertEqual(
			legacy.stripe_status({"status": "Failed", "error_code": "card_declined"}),
			("failed", "card_declined"),
		)

	def test_an_attempt_the_till_never_saw_end_is_never_left_open(self):
		# `processing` would be polled at the provider and its answer booked: an old attempt is `failed`.
		for status in ("Draft", "Processing", "Authorized", None):
			self.assertEqual(legacy.stripe_status({"status": status}), ("failed", legacy.UNFINISHED))

	def test_a_twint_request_says_what_became_of_the_payment(self):
		paid = {"payment_status": "SUCCESS", "status_reason": "ORDER_OK"}
		self.assertEqual(legacy.twint_status(paid, reversed_=False), ("succeeded", None))
		self.assertEqual(legacy.twint_status(paid, reversed_=True), ("refunded", None))
		pairing = {
			"status": "IN_PROGRESS",
			"payment_status": "IN_PROGRESS",
			"status_reason": "ORDER_RECEIVED",
		}
		self.assertEqual(legacy.twint_status(pairing, reversed_=False), ("failed", legacy.UNFINISHED))
		self.assertEqual(
			legacy.twint_status({"payment_initiated": "1725971304"}, False), ("failed", legacy.UNFINISHED)
		)
		self.assertEqual(legacy.twint_status({}, reversed_=False), ("canceled", legacy.ABANDONED))

	def test_a_request_finds_its_settlement_lines_and_its_reversal(self):
		lines = [
			{"name": "a", "reference": "_Walk-in_000000000001", "operation": "Immediate"},
			{"name": "b", "reference": "_Walk-in_000000000001_000000000002", "operation": "REVERSAL"},
			{"name": "c", "reference": "_Walk-in_0000000000", "operation": "Immediate"},
		]
		mine, reversed_ = legacy.settlement_lines("_Walk-in_000000000001", lines)
		self.assertEqual([line["name"] for line in mine], ["a", "b"])
		self.assertTrue(reversed_)
		self.assertEqual(legacy.settlement_lines(None, lines), ([], False))

	def test_epoch_seconds_are_read_as_a_time(self):
		self.assertIsInstance(legacy.from_epoch("1708595343"), datetime)
		self.assertIsNone(legacy.from_epoch(None))
		self.assertIsNone(legacy.from_epoch("garbage"))


class TestTheIntentOfALegacyRow(FrappeTestCase):
	def test_a_captured_card_payment_keeps_its_ticket_customer_and_provider_id(self):
		values = legacy.stripe_intent(CAPTURED, TICKET, "DEV-2026-000001")
		self.assertEqual(values["status"], "succeeded")
		self.assertEqual(values["amount"], 2400)
		self.assertEqual(values["currency"], "CHF")
		self.assertEqual(values["provider_intent_id"], "pi_test_captured")
		self.assertEqual(
			(values["reference_doctype"], values["reference_name"]), ("POS Invoice", "POS-TEST-0001")
		)
		self.assertEqual(values["device"], "DEV-2026-000001")
		metadata = values["metadata"]
		self.assertEqual(metadata["legacy_key"], "Stripe Transaction/st-test-0001")
		self.assertEqual(metadata["customer"], "Walk-in")
		self.assertEqual(metadata["consolidated_invoice"], "SINV-TEST-0001")
		self.assertEqual(metadata["legacy"]["app"], "neopay_integration")
		# booked by the till at the time: the reconciliation of payments must never book it again
		self.assertTrue(metadata["reconciled_at"])
		json.dumps(metadata, default=str)

	def test_a_row_without_an_amount_is_not_copied(self):
		self.assertIsNone(legacy.stripe_intent(dict(CAPTURED, amount=0), TICKET, None))
		self.assertIsNone(legacy.twint_intent({"name": "x", "amount": None}, [], False, None))

	def test_a_paid_twint_request_keeps_its_ticket_and_its_settlement(self):
		row = {
			"name": "tpr-test-0001",
			"amount": 55,
			"customer": "Walk-in",
			"payment_against_type": "POS Invoice",
			"payment_against": "POS-TEST-0002",
			"payment_status": "SUCCESS",
			"status_reason": "ORDER_OK",
			"payment_initiated": "1708595330",
			"payment_successful": "1708595343",
			"reference": "_Walk-in_000000000001",
			"creation": "2024-02-22 10:48:50",
			"modified": "2024-02-22 10:49:10",
		}
		lines = [
			{"name": "b", "reference": "_Walk-in_000000000001_x", "operation": "REVERSAL", "amount": "55"}
		]
		values = legacy.twint_intent(row, lines, True, ("POS Invoice", "POS-TEST-0002"))
		self.assertEqual(values["status"], "refunded")
		self.assertEqual(values["amount"], 5500)
		self.assertEqual(values["reference_name"], "POS-TEST-0002")
		self.assertEqual(values["metadata"]["legacy"]["settlement"][0]["operation"], "REVERSAL")
		self.assertEqual(values["metadata"]["customer"], "Walk-in")
		self.assertEqual(values["completed_at"], legacy.from_epoch("1708595343"))


class TestTheShopTwintOfTwintIntegration(FrappeTestCase):
	def test_a_paid_shop_payment_keeps_its_order_and_customer(self):
		row = {
			"name": "uuid-1",
			"order_uuid": "uuid-1",
			"amount": 89.9,
			"currency": "CHF",
			"status": "SUCCESS",
			"reference_doctype": "Sales Order",
			"reference_docname": "SO-TEST-0001",
			"payer_name": "A buyer",
			"creation": "2025-07-01 10:00:00",
			"modified": "2025-07-01 10:01:00",
		}
		values = legacy.twint_web_intent(row, customer="CUST-1")
		self.assertEqual(values["status"], "succeeded")
		self.assertEqual(values["amount"], 8990)
		self.assertEqual(values["provider_intent_id"], "uuid-1")
		self.assertEqual(
			(values["reference_doctype"], values["reference_name"]), ("Sales Order", "SO-TEST-0001")
		)
		self.assertEqual(values["metadata"]["customer"], "CUST-1")
		self.assertEqual(values["metadata"]["legacy_key"], "Twint Transaction/uuid-1")

	def test_a_shop_payment_never_finished_is_not_left_open(self):
		for status in ("PENDING", "IN_PROGRESS", None):
			self.assertEqual(legacy.twint_web_status({"status": status}), ("failed", legacy.UNFINISHED))
		self.assertEqual(legacy.twint_web_status({"status": "FAILURE"}), ("failed", None))


class TestACopyIsMadeOnce(FrappeTestCase):
	def test_a_row_already_copied_or_known_by_its_provider_id_is_left_alone(self):
		first = legacy.stripe_intent(CAPTURED, TICKET, None)
		second = legacy.stripe_intent(
			dict(CAPTURED, name="other", payment_intent_id="pi_known"), None, None
		)
		third = legacy.stripe_intent(
			dict(CAPTURED, name="third", payment_intent_id="pi_new"), None, None
		)
		report = {"sources": {}}
		known = ({"Stripe Transaction/st-test-0001"}, {"pi_known"})
		legacy._copy(
			"Stripe Transaction",
			[({}, first), ({}, second), ({}, third), ({}, None)],
			"stripe",
			"terminal",
			None,
			known,
			True,
			report,
		)
		part = report["sources"]["Stripe Transaction"]
		self.assertEqual((part["rows"], part["copied"], part["already"]), (4, 1, 2))
		self.assertEqual(part["skipped"], {"no_amount": 1})
		self.assertIn("Stripe Transaction/third", known[0])

	def test_a_site_without_any_legacy_table_is_left_as_it_is(
		self,
	):  # role-check: ok a patch runs as Administrator during bench migrate
		if legacy.table_exists(legacy.STRIPE) or legacy.table_exists(legacy.TWINT_REQUEST):
			self.skipTest(
				"this site carries legacy payment tables: the proof on such data is the staging copy"
			)
		before = frappe.db.count("Payment Intent")
		report = legacy.copy_legacy_payment_records(dry_run=1)
		self.assertEqual(report["sources"], {})
		self.assertEqual(frappe.db.count("Payment Intent"), before)
