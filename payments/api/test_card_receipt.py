# Copyright (c) 2026, Neoffice and Contributors
# License: MIT. See LICENSE
"""Unit tests for filling the card mentions on a POS invoice."""

from __future__ import annotations

import json
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from payments.api.card_receipt import fill_card_details

INTENT = "PI-TEST-CARD-0001"

#: The receipt a settled Payrexx terminal payment leaves on the intent, as
#: measured on a NexGo N86 on 2026-08-22.
_RECEIPT = {
	"amount": 100,
	"currency": "CHF",
	"datetime": "Aug 22, 2026 02:14:33 PM",
	"masked_pan": "535388******4256",
	"card_scheme": "MASTERCARD",
	"authorisation": "A0000000041010",
	"merchant_name": "Neoservice Christillin",
	"terminal_label": "Terminal-0506",
	"payment_method": "CARD",
	"payment_id": "ca55cf91-1812-4668-b188-6bde74783aa5",
}


def _invoice(**row):
	doc = frappe.new_doc("Sales Invoice")
	doc.append("payments", {"mode_of_payment": "Cash", "amount": 1.0, **row})
	return doc


class TestCardReceipt(FrappeTestCase):
	def _fill(self, doc, receipt=_RECEIPT):
		payload = json.dumps({"receipt": receipt}) if receipt is not None else None
		with patch("payments.api.card_receipt.frappe.db.get_value", return_value=payload):
			fill_card_details(doc)
		return doc.payments[0]

	def test_fills_the_mentions_a_card_receipt_needs(self):
		row = self._fill(_invoice(card_payment_intent=INTENT))

		self.assertEqual(row.card_brand, "MASTERCARD")
		self.assertEqual(row.card_last4, "4256")
		self.assertEqual(row.card_dedicated_file_name, "A0000000041010")
		self.assertEqual(row.card_charge_id, "ca55cf91-1812-4668-b188-6bde74783aa5")
		self.assertEqual(row.amount_authorized, 100)

	def test_authorization_code_is_left_empty(self):
		"""The AID is not an authorisation code, and Payrexx returns no such code.

		Filling the field with the one value we happen to have would put a wrong
		number on a document a customer may use to dispute a charge.
		"""
		row = self._fill(_invoice(card_payment_intent=INTENT))
		self.assertFalse(row.card_authorization_code)

	def test_only_the_last_four_digits_are_kept(self):
		"""The slip carries the whole masked PAN; the field wants the tail."""
		row = self._fill(_invoice(card_payment_intent=INTENT))
		self.assertEqual(row.card_last4, "4256")
		self.assertNotIn("*", row.card_last4)

	def test_a_row_with_no_intent_is_untouched(self):
		"""Cash rows exist on the same invoice and must stay empty."""
		row = self._fill(_invoice())
		self.assertIsNone(row.get("card_brand"))

	def test_existing_values_are_never_overwritten(self):
		"""Another driver may have filled these, or a human may have corrected them."""
		row = self._fill(_invoice(card_payment_intent=INTENT, card_brand="VISA"))
		self.assertEqual(row.card_brand, "VISA")

	def test_an_intent_with_no_receipt_is_silent(self):
		"""A terminal payment that settled before the details were kept."""
		row = self._fill(_invoice(card_payment_intent=INTENT), receipt=None)
		self.assertIsNone(row.get("card_brand"))

	def test_an_unparseable_pan_does_not_produce_a_last4(self):
		"""Better no mention than four characters that are not the card's."""
		row = self._fill(
			_invoice(card_payment_intent=INTENT), receipt={**_RECEIPT, "masked_pan": "n/a"}
		)
		self.assertFalse(row.card_last4)
