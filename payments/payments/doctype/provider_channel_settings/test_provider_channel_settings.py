# //// Neoffice — added file (no upstream equivalent). Covers #219: the webhook
# //// endpoint used to be built from the Payment Provider RECORD name, so a
# //// `wallee_test` provider got `/api/method/payments.api.webhook_wallee_test.handle`
# //// — a 404 — and it was rebuilt on every save, silently destroying a path a human
# //// had corrected. These tests pin both halves: derive from the FAMILY, and never
# //// claim an endpoint whose receiver does not ship.
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

PREFIX = "/api/method/payments.api.webhook_"


def _row(provider, endpoint=None):
	"""An unsaved Provider Channel Settings — the computation needs no database row."""
	doc = frappe.get_doc(
		{
			"doctype": "Provider Channel Settings",
			"provider": provider,
			"channel": "Online",
			"webhook_endpoint": endpoint,
		}
	)
	return doc


class TestProviderChannelSettingsWebhookEndpoint(FrappeTestCase):
	def _compute(self, provider, mode, endpoint=None):
		doc = _row(provider, endpoint)
		db = MagicMock()
		db.get_value.return_value = mode
		with patch.object(frappe, "db", db):
			doc._compute_webhook_endpoint()
		return doc.webhook_endpoint

	def test_family_receiver_not_the_record_name(self):
		"""wallee_test and wallee_live share the one receiver the app ships."""
		for provider, mode in (("wallee_test", "test"), ("wallee_live", "live")):
			self.assertEqual(self._compute(provider, mode), f"{PREFIX}wallee.handle")

	def test_a_provider_that_is_already_a_family_is_left_whole(self):
		self.assertEqual(self._compute("stripe", ""), f"{PREFIX}stripe.handle")

	def test_suffix_is_stripped_even_without_a_mode(self):
		"""Records that predate the `mode` field still resolve to their family."""
		self.assertEqual(self._compute("payrexx_live", ""), f"{PREFIX}payrexx.handle")

	def test_no_receiver_means_no_endpoint_rather_than_a_404(self):
		self.assertFalse(self._compute("acme_test", "test"))

	def test_a_stale_computed_path_is_cleared_not_kept(self):
		stale = f"{PREFIX}acme_test.handle"
		self.assertEqual(self._compute("acme_test", "test", stale), "")

	def test_an_endpoint_set_by_hand_survives_a_save(self):
		"""The whole second half of #219: validate() used to overwrite it every time."""
		manual = "/api/method/my_app.receivers.wallee.handle"
		self.assertEqual(self._compute("wallee_test", "test", manual), manual)

	def test_a_computed_path_is_corrected(self):
		wrong = f"{PREFIX}wallee_test.handle"
		self.assertEqual(self._compute("wallee_test", "test", wrong), f"{PREFIX}wallee.handle")
