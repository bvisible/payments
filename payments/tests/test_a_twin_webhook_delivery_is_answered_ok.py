# //// Neoffice — added file (no upstream equivalent). Wallee sent one event twice in the same second
# //// (2026-11-10, a real delivery on the test instance): the second insert hit the unique row and answered
# //// 409, so Wallee counted a delivered event as failed and sent it again. A twin is answered « ok ».
"""A twin delivery of one event is answered « ok », never 409."""

import uuid
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from payments.api import webhook_common


class TestATwinWebhookDelivery(FrappeTestCase):
	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		frappe.set_user("Administrator")  # the scene only: a delivery has no user
		self.event_id = f"_t-twin-{uuid.uuid4().hex[:10]}"

	def values(self):
		return {
			"event_id": self.event_id,
			"provider": frappe.db.get_value("Payment Provider", {}, "name"),
			"event_type": "transaction.pending",
			"signature_valid": 1,
			"status": "Queued",
		}

	def test_the_twin_finds_the_row_already_written(self):
		self.assertIsNotNone(webhook_common.record_delivery(self.values()))
		with patch.object(frappe.db, "rollback"):
			self.assertIsNone(webhook_common.record_delivery(self.values()))

	def test_the_wallee_endpoint_answers_ok_to_the_twin(self):
		from payments.api import webhook_wallee

		webhook_common.record_delivery(self.values())
		result = MagicMock(event_id=self.event_id, event_type="transaction.pending")
		with (
			patch.object(webhook_common, "signed_delivery", return_value=(MagicMock(), result)),
			patch.object(webhook_common, "received_before", return_value=False),
			patch.object(webhook_common, "provider_of", return_value=self.values()["provider"]),
			patch.object(webhook_common, "queue_event") as queued,
			patch.object(frappe.db, "rollback"),
			patch.object(frappe.db, "commit"),
		):
			frappe.local.request = MagicMock(get_data=lambda: b"{}", headers={})
			self.assertEqual(webhook_wallee.handle(), "ok")
		queued.assert_not_called()
