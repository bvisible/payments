# //// Neoffice — added file (no upstream equivalent). See repoint_the_twint_gateway_to_the_bridge_settings.py.
from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase

from payments.patches.v15_13 import repoint_the_twint_gateway_to_the_bridge_settings as patch


class TestATwintGatewayIsPointedAtTheBridgeSettings(FrappeTestCase):
	def run_patch(self, pointing_at, legacy_doctype_exists, controller="Twint-1"):
		def exists(doctype, name=None):
			if doctype == "Payment Gateway":
				return True
			return name == patch.BRIDGE or (name == patch.LEGACY and legacy_doctype_exists)

		def get_value(doctype, filters, fieldname=None, **kwargs):
			return pointing_at if doctype == "Payment Gateway" else controller

		with (
			mock.patch.object(frappe.db, "exists", side_effect=exists),
			mock.patch.object(frappe.db, "get_value", side_effect=get_value),
			mock.patch.object(frappe.db, "set_value") as write,
			mock.patch.object(frappe.db, "commit"),
		):
			patch.execute()
		return write

	def test_a_gateway_that_names_a_doctype_which_is_gone_is_pointed_at_the_bridge(self):
		write = self.run_patch(patch.LEGACY, legacy_doctype_exists=False)
		write.assert_called_once_with(
			"Payment Gateway",
			"Twint",
			{"gateway_settings": patch.BRIDGE, "gateway_controller": "Twint-1"},
			update_modified=False,
		)

	def test_nothing_is_touched_while_the_legacy_doctype_still_exists(self):
		self.assertFalse(self.run_patch(patch.LEGACY, legacy_doctype_exists=True).called)

	def test_a_gateway_that_already_names_the_bridge_is_left_alone(self):
		self.assertFalse(self.run_patch(patch.BRIDGE, legacy_doctype_exists=False).called)

	def test_without_a_bridge_record_only_the_settings_are_repointed(self):
		write = self.run_patch(patch.LEGACY, legacy_doctype_exists=False, controller=None)
		self.assertEqual(write.call_args.args[2], {"gateway_settings": patch.BRIDGE})
