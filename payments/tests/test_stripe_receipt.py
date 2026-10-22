# //// Neoffice — added file (no upstream equivalent). The receipt of a Stripe card charge (maintenance#1807).
# Copyright (c) 2026, Neoffice and Contributors
# License: MIT. See LICENSE
"""A card charge leaves a receipt the shop can conclude a payment on, and a charge is no payment of a request it
does not cover.

`Stripe Settings` keeps, on the charge's Integration Request, what STRIPE answered (amount, currency,
charge id) and binds it to the request the tile names; the shop concludes on that (payments.utils.stripe_receipt).
Stripe is a stand-in here: no card is charged. What is proven is what the server writes and reads.
"""

import json
from unittest.mock import patch

import frappe
from frappe.model.document import Document
from frappe.tests.utils import FrappeTestCase

from payments.utils import stripe_receipt

MARK = "_STRIPE-RECEIPT-TEST"


def a_payment_request(grand_total=10, currency="CHF"):
	"""A request as the shop raises it. Inserted with validation relaxed: what is under test is how the receipt is
	bound to it and compared with it, not ERPNext's own creation rules."""
	doc = frappe.new_doc("Payment Request")
	doc.update(
		{
			"payment_request_type": "Inward",
			"reference_doctype": "Sales Order",
			"reference_name": f"{MARK}-ORDER",
			"grand_total": grand_total,
			"currency": currency,
			"email_to": "receipt-test@example.com",
			"status": "Draft",
		}
	)
	doc.flags.ignore_validate = True
	doc.flags.ignore_links = True
	doc.insert(ignore_permissions=True, ignore_mandatory=True)
	return doc


def stripe_answers(amount, currency="chf", captured=True):
	"""Stripe's answer to a charge: what it says, not what was asked."""
	return frappe._dict(
		id="ch_" + frappe.generate_hash(length=16),
		amount=amount,
		currency=currency,
		captured=captured,
		paid=captured,
	)


class TestAStripeChargeLeavesAReceipt(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.request = a_payment_request()

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		frappe.db.delete("Integration Request", {"request_description": MARK})
		frappe.db.delete("Integration Request", {"data": ["like", f"%{MARK}%"]})
		frappe.db.delete("Payment Request", {"reference_name": f"{MARK}-ORDER"})
		frappe.db.commit()

	# --- helpers ------------------------------------------------------------------

	def charge(
		self, answer, payment_request_id="__this__", reference=("User", "Administrator"), **data
	):
		"""The card tile's call, with Stripe stood in: the server asks for the charge and reads the answer."""
		import stripe

		from payments.payment_gateways.doctype.stripe_settings.stripe_settings import StripeSettings

		settings = frappe.new_doc("Stripe Settings")
		payload = {
			"stripe_token_id": "tok_test",
			"amount": 10,
			"currency": "CHF",
			"description": "test",
			"payer_email": "receipt-test@example.com",
			"reference_doctype": reference[0],
			"reference_docname": reference[1],
			"request_description": MARK,
		}
		if payment_request_id:
			payload["payment_request_id"] = (
				self.request.name if payment_request_id == "__this__" else payment_request_id
			)
		payload.update(data)
		with (
			patch.object(stripe.Charge, "create", return_value=answer),
			patch.object(StripeSettings, "finalize_request", autospec=True, return_value={}),
		):
			settings.create_request(payload)
		return settings

	def receipts(self, request=None):
		return frappe.get_all(
			"Integration Request",
			filters={
				"integration_request_service": "Stripe",
				"reference_doctype": "Payment Request",
				"reference_docname": request or self.request.name,
			},
			fields=["name", "status", "output"],
		)

	# --- what the server writes ---------------------------------------------------

	def test_a_captured_charge_leaves_what_stripe_answered(self):
		# the browser said 5; Stripe took 1000: the receipt holds Stripe's figure
		answer = stripe_answers(1000)
		self.charge(answer, amount=5)

		(receipt,) = self.receipts()
		self.assertEqual(receipt.status, "Completed")
		kept = json.loads(receipt.output)
		self.assertEqual((kept["charge"], kept["amount"], kept["currency"]), (answer.id, 1000, "chf"))
		self.assertTrue(kept["captured"])

	def test_the_receipt_is_bound_to_the_request_the_tile_names(self):
		self.charge(stripe_answers(1000))
		self.assertEqual(len(self.receipts()), 1)

	def test_a_request_that_does_not_exist_is_not_bound(self):
		self.charge(stripe_answers(1000), payment_request_id="ACC-PRQ-NOT-THERE")
		self.assertEqual(self.receipts("ACC-PRQ-NOT-THERE"), [])
		self.assertEqual(self.receipts(), [])

	def test_a_charge_that_was_not_captured_leaves_no_receipt(self):
		self.charge(stripe_answers(1000, captured=False))
		self.assertEqual(self.receipts(), [])

	# --- what the shop reads ------------------------------------------------------

	def test_a_charge_of_the_amount_covers_the_request(self):
		self.charge(stripe_answers(1000))
		self.assertTrue(stripe_receipt.covers(self.request.name))

	def test_a_charge_for_less_does_not_cover_it(self):
		self.charge(stripe_answers(50))
		self.assertFalse(stripe_receipt.covers(self.request.name))

	def test_two_small_charges_do_not_add_up_to_a_request(self):
		self.charge(stripe_answers(500))
		self.charge(stripe_answers(500))
		self.assertFalse(stripe_receipt.covers(self.request.name))

	def test_a_charge_in_another_currency_does_not_cover_it(self):
		self.charge(stripe_answers(1000, currency="eur"))
		self.assertFalse(stripe_receipt.covers(self.request.name))

	def test_the_amount_is_compared_the_way_the_charge_is_made(self):
		"""`cint(flt(amount) * 100)` truncates: 4.35 becomes 434 on the way to Stripe. The receipt of that charge covers
		the request of 4.35; a charge of 1 cent more or less does not."""
		request = a_payment_request(grand_total=4.35)
		self.charge(
			stripe_answers(stripe_receipt.stripe_minor_units(4.35)), payment_request_id=request.name
		)
		self.assertTrue(stripe_receipt.covers(request.name))
		other = a_payment_request(grand_total=4.35)
		self.charge(stripe_answers(436), payment_request_id=other.name)
		self.assertFalse(stripe_receipt.covers(other.name))

	def test_a_request_with_no_receipt_is_not_covered(self):
		self.assertFalse(stripe_receipt.covers(self.request.name))
		self.assertFalse(stripe_receipt.covers(None))
		self.assertFalse(stripe_receipt.covers("ACC-PRQ-NOT-THERE"))

	# --- what a charge may settle -------------------------------------------------

	def finalize(self, settings):
		"""`finalize_request` with the request's own hook stood in, to see whether it is called."""
		with patch.object(Document, "run_method", autospec=True, return_value=None) as run_method:
			settings.finalize_request()
		return [call for call in run_method.call_args_list if call.args[1] == "on_payment_authorized"]

	def settings_of(self, answer, reference):
		import stripe

		settings = frappe.new_doc("Stripe Settings")
		settings.data = frappe._dict(
			reference_doctype=reference[0],
			reference_docname=reference[1],
			payment_request_id=self.request.name,
			amount=5,
			currency="CHF",
			description="x",
			payer_email="a@b.c",
			stripe_token_id="tok_test",
			request_description=MARK,
		)
		settings.integration_request = frappe.get_doc(
			{
				"doctype": "Integration Request",
				"integration_request_service": "Stripe",
				"request_description": MARK,
				"status": "Completed",
				"data": "{}",
				"reference_doctype": reference[0],
				"reference_docname": reference[1],
			}
		).insert(ignore_permissions=True)
		stripe_receipt.write(settings.integration_request, answer, self.request.name)
		settings.flags.status_changed_to = "Completed"
		return settings

	def test_a_charge_that_does_not_cover_a_request_does_not_settle_it(self):
		settings = self.settings_of(stripe_answers(50), ("Payment Request", self.request.name))
		title = "Stripe: a charge does not cover the request it names"
		before = frappe.db.count("Error Log", {"method": title})
		self.assertEqual(self.finalize(settings), [], "the request must not be told it was paid")
		self.assertEqual(
			frappe.db.count("Error Log", {"method": title}), before + 1, "the refusal is not silent"
		)

	def test_a_charge_that_covers_the_request_settles_it(self):
		settings = self.settings_of(stripe_answers(1000), ("Payment Request", self.request.name))
		self.assertEqual(len(self.finalize(settings)), 1)

	def test_another_reference_keeps_upstreams_behaviour(self):
		settings = self.settings_of(stripe_answers(50), ("User", "Administrator"))
		self.assertEqual(len(self.finalize(settings)), 1)
