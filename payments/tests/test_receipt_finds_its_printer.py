# //// Neoffice — added file (no upstream equivalent). maintenance#2131: a card receipt printed without naming a
# //// printer asked `frappe.db.get_value("Singles", …)` for the default one. `tabSingles` has no `modified` column to
# //// sort on, so the till got a 500 instead of its slip, or instead of being told that no printer is set.
"""A card receipt printed without naming a printer goes to the default one, or says there is none.

The default is written straight to `tabSingles` and read back for real: that read is what broke. Only what the
cloudprnt app owns stands in, since the CI does not install it: its table of printers and its queue.
"""

import json
import sys
from unittest.mock import MagicMock, patch

import frappe
from frappe import _
from frappe.tests.roles import as_role
from frappe.tests.utils import FrappeTestCase

from payments.api import terminal_receipt
from payments.tests.test_intent_api import _ensure_mock_setup

SETTINGS = "CloudPRNT Settings"
FIELD = "default_printer"
# `default_printer` links to a row of the printers table, so it holds that row's name, never a MAC address.
ROW = "a1b2c3d4e5"
MAC = "00:11:62:aa:bb:cc"
RECEIPT = {
	"amount": 1250,
	"currency": "CHF",
	"masked_pan": "535388******4256",
	"merchant_name": "Test Shop",
}


def _store_default_printer(*value):
	"""Straight to `tabSingles`, as the CI has no CloudPRNT Settings to save. No value given, no row."""
	frappe.db.delete("Singles", {"doctype": SETTINGS, "field": FIELD})
	if value:
		frappe.qb.into("Singles").columns("doctype", "field", "value").insert(
			SETTINGS, FIELD, *value
		).run()


class TestReceiptFindsItsPrinter(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_mock_setup()

	def setUp(self):
		self.intent = (
			frappe.get_doc(
				{
					"doctype": "Payment Intent",
					"provider": "mock",
					"channel": "terminal",
					"amount": 1250,
					"currency": "CHF",
					"status": "succeeded",
					"next_action_payload": json.dumps({"receipt": RECEIPT}),
				}
			)
			.insert(ignore_permissions=True)
			.name
		)
		self.queue = MagicMock(return_value={"status": "queued"})
		modules = patch.dict(
			sys.modules, {"cloudprnt.print_queue_manager": MagicMock(add_job_to_queue=self.queue)}
		)
		modules.start()
		self.addCleanup(modules.stop)

	def _print_with_no_printer_named(self):
		read = frappe.db.get_value

		def get_value(doctype, filters=None, *args, **kwargs):
			if doctype == "CloudPRNT Printers":
				return MAC if filters == ROW else None
			return read(doctype, filters, *args, **kwargs)

		with patch.object(frappe.db, "get_value", get_value), as_role("accountant"):
			return terminal_receipt.print_receipt(self.intent)

	def test_the_slip_goes_to_the_default_printer(self):
		_store_default_printer(ROW)

		result = self._print_with_no_printer_named()

		self.assertEqual(result["printer"], MAC)
		self.assertEqual(self.queue.call_args.kwargs["printer_mac"], MAC)

	def test_with_no_default_the_till_is_told_so(self):
		for case, stored in (("no row", ()), ("an empty value", (None,))):
			with self.subTest(case):
				_store_default_printer(*stored)

				with self.assertRaisesRegex(
					frappe.ValidationError, _("No CloudPRNT printer given and no default configured")
				):
					self._print_with_no_printer_named()

				self.queue.assert_not_called()
