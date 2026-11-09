# //// Neoffice — added file (no upstream equivalent). The Payrexx setup wizard rebuilt the stored
# //// credentials from its three form fields: a key it did not show (the webhook signing key) was
# //// dropped each time it was reopened, and the API secret had to be typed again although the form
# //// said « already stored ». It also stopped on a site that never got the `terminal` channel.
"""The Payrexx wizard changes what it is given, keeps the rest, and finds the channels it needs."""

import json
from unittest.mock import patch

import frappe
from frappe.tests.roles import as_role
from frappe.tests.utils import FrappeTestCase

from payments.api import payrexx_setup

STORED = {
	"instance": "_t-shop",
	"api_secret": "_t-api-secret",
	"pos_api_secret": "_t-pos-secret",
	"webhook_signing_key": "_t-signing-key",
}


class TestPayrexxSetupKeepsWhatItIsNotGiven(FrappeTestCase):
	def setUp(self):
		# save_credentials commits on purpose (a wizard step must stick); a test must leave nothing.
		self._commit = patch.object(frappe.db, "commit")
		self._commit.start()
		self.addCleanup(self._commit.stop)
		self.addCleanup(frappe.db.rollback)

		frappe.set_user("Administrator")  # the scene only; the merchant acts below
		if frappe.db.exists("Payment Provider", payrexx_setup.PROVIDER):
			doc = frappe.get_doc("Payment Provider", payrexx_setup.PROVIDER)
		else:
			doc = frappe.new_doc("Payment Provider")
			doc.provider_name = payrexx_setup.PROVIDER
			doc.display_label = "Payrexx"
			doc.driver_class = "payments.drivers.payrexx.provider.PayrexxProvider"
		doc.credentials_json = json.dumps(STORED)
		doc.mode = "test"
		doc.enabled = 1
		doc.flags.ignore_permissions = True
		doc.save()

	def stored(self):
		doc = frappe.get_doc("Payment Provider", payrexx_setup.PROVIDER)
		return json.loads(doc.credentials_json), doc.mode

	def test_reopening_to_go_live_keeps_every_stored_secret(self):
		with as_role("head_accountant"):
			payrexx_setup.save_credentials(instance="_t-shop", api_secret="", mode="live")

		creds, mode = self.stored()
		self.assertEqual(mode, "live")
		self.assertEqual(creds, STORED)

	def test_a_typed_signing_key_replaces_the_stored_one_and_nothing_else(self):
		with as_role("head_accountant"):
			payrexx_setup.save_credentials(
				instance="_t-shop", webhook_signing_key="  _t-new-key  ", mode="test"
			)

		creds, _mode = self.stored()
		self.assertEqual(creds["webhook_signing_key"], "_t-new-key")
		self.assertEqual(creds["api_secret"], STORED["api_secret"])
		self.assertEqual(creds["pos_api_secret"], STORED["pos_api_secret"])

	def test_the_address_from_the_address_bar_is_stored_as_the_bare_instance(self):
		with as_role("head_accountant"):
			payrexx_setup.save_credentials(instance="  https://_t-shop.payrexx.com/  ", mode="live")

		creds, _mode = self.stored()
		self.assertEqual(creds["instance"], "_t-shop")

	def test_every_pasted_form_of_the_instance_gives_the_account_name(self):
		from payments.drivers.payrexx._common import instance_name

		for typed in (
			"shop",
			"Shop",
			"shop.payrexx.com",
			"https://shop.payrexx.com/",
			"https://shop.payrexx.com/cadmin/index.php?cmd=checkout&act=integrations",
			"http://shop.payrexx.com",
		):
			self.assertEqual(instance_name(typed), "shop", typed)
		self.assertEqual(instance_name(None), "")

	def test_a_failed_connection_says_why(self):
		class _Client:
			def __enter__(self):
				return self

			def __exit__(self, *exc):
				return False

			def health_check(self):
				return {"ok": False, "instance": "_t-shop", "error": "The API secret is not correct."}

		with (
			patch("payments.drivers.payrexx._common.build_client", return_value=_Client()),
			as_role("head_accountant"),
		):
			result = payrexx_setup.test_connection()

		self.assertFalse(result["ok"])
		self.assertEqual(result["error"], "The API secret is not correct.")

	def test_no_api_secret_anywhere_is_still_refused(self):
		frappe.db.set_value(
			"Payment Provider",
			payrexx_setup.PROVIDER,
			"credentials_json",
			json.dumps({"instance": "_t-shop"}),
		)
		with as_role("head_accountant"), self.assertRaises(frappe.ValidationError):
			payrexx_setup.save_credentials(instance="_t-shop", mode="test")

	def test_the_setup_says_whether_the_signing_key_is_there_never_what_it_is(self):
		with as_role("head_accountant"):
			setup = payrexx_setup.get_current_setup()

		self.assertTrue(setup["provider"]["has_webhook_key"])
		self.assertNotIn(STORED["webhook_signing_key"], json.dumps(setup))
		self.assertTrue(setup["webhook_url"].endswith("/api/method/payments.api.webhook_payrexx.handle"))

	def test_the_terminal_channel_is_provisioned_when_the_site_never_got_it(self):
		asked = []

		def provision(codes=None):
			asked.extend(codes or [])
			return []

		with (
			patch.object(payrexx_setup, "test_connection", return_value={"ok": True}),
			patch("payments.setup.payment_channels.provision_payment_channels", side_effect=provision),
			as_role("head_accountant"),
		):
			payrexx_setup.setup_channels(web=False, terminal=True)

		self.assertEqual(asked, ["terminal"])
