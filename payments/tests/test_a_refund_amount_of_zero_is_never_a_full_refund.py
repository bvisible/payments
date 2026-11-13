# //// Neoffice — added file (no upstream equivalent). maintenance#1310: once the endpoints' annotations cast what a
# //// request sends, a refund amount of "0" arrives as 0 — it must never read as « refund everything ».
import frappe
from frappe.tests.utils import FrappeTestCase

from payments.api.intent import _refund_amount


class TestTheRefundAmount(FrappeTestCase):
	def test_absent_or_empty_is_a_full_refund(self):
		self.assertIsNone(_refund_amount(None))
		self.assertIsNone(_refund_amount(""))

	def test_an_amount_is_taken_as_given(self):
		self.assertEqual(_refund_amount(1250), 1250)
		self.assertEqual(_refund_amount("1250"), 1250)

	def test_zero_is_refused_never_a_full_refund(self):
		for amount in (0, "0", -5):
			with self.assertRaises(frappe.ValidationError):
				_refund_amount(amount)
