# //// Neoffice — added file (no upstream equivalent). Guards the channel registry: the
# //// database holds every shipped channel, provisioning is idempotent, and a deleted
# //// channel comes back with its ui_kind and capabilities. Upstream has no Payment
# //// Channel doctype.
# //// Commits: 7a0f7ca 2026-09-03 "fix(install): provision the shipped Payment Channels on a fresh site"
# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
import frappe
from frappe.tests.utils import FrappeTestCase

from payments.setup.payment_channels import CHANNELS, provision_payment_channels


class TestPaymentChannels(FrappeTestCase):
	def test_every_shipped_channel_exists(self):
		# On an installed site after_install provisioned them; on a migrated one the
		# patches did. Either way the registry and the database must agree.
		provision_payment_channels()
		for spec in CHANNELS:
			self.assertTrue(
				frappe.db.exists("Payment Channel", spec["channel_code"]), spec["channel_code"]
			)

	def test_provisioning_is_idempotent_and_recreates_a_missing_channel(self):
		self.assertEqual(provision_payment_channels(), [])
		frappe.delete_doc("Payment Channel", "twint_mobile", force=True, ignore_permissions=True)
		self.assertEqual(provision_payment_channels(codes=["twint_mobile"]), ["twint_mobile"])
		self.assertEqual(provision_payment_channels(), [])
		doc = frappe.get_doc("Payment Channel", "twint_mobile")
		self.assertEqual(doc.ui_kind, "qr_display")
		self.assertTrue(frappe.parse_json(doc.capabilities_json)["requires_qr_scan"])
