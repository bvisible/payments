# //// Neoffice — added file (no upstream equivalent). The Wallee setup page (2026-11-10) and the webhook it
# //// sets up at Wallee: Wallee signs those deliveries with a key pair (ECDSA), and the driver now proves
# //// them with the public key Wallee serves; the HMAC path for a secret set by hand is unchanged.
"""Wallee « Connect » lays the ERP side down; a Wallee-signed delivery is proven, a tampered one refused."""

import base64
import json
import uuid
from unittest.mock import patch

import frappe
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from frappe.tests.roles import as_role
from frappe.tests.utils import FrappeTestCase

from payments.api import wallee_setup


def _signed(payload: bytes, key, key_id: str) -> str:  # noqa: ANN001
	signature = base64.b64encode(key.sign(payload, ec.ECDSA(hashes.SHA256()))).decode()
	return f"algorithm=SHA256withECDSA, keyId={key_id}, signature={signature}"


class TestWalleeConnect(FrappeTestCase):
	def setUp(self):
		self._commit = patch.object(frappe.db, "commit")
		self.commit = self._commit.start()
		self.addCleanup(self._commit.stop)
		self.addCleanup(frappe.db.rollback)
		frappe.set_user("Administrator")  # the scene only
		frappe.db.set_global(wallee_setup.STATUS_KEY, "{}")

	def shop_rows(self):
		if not frappe.db.exists("DocType", "Webshop Settings"):
			return []
		return [r.payment_gateway_account for r in frappe.get_doc("Webshop Settings").payment_methods]

	def connect(self):
		with (
			patch.object(wallee_setup, "_health", return_value={"ok": True}),
			patch.object(wallee_setup, "_ensure_webhook", return_value={"url_id": 1, "listener_id": 2}),
			as_role("head_accountant"),
		):
			return wallee_setup.connect(space_id="12345", user_id="678", authentication_key="_t-key")

	def test_connect_prepares_everything_and_leaves_the_shop_as_it_was(self):
		before = self.shop_rows()
		result = self.connect()

		self.assertTrue(result["ok"])
		self.assertEqual(self.shop_rows(), before, "nothing may reach the shop on its own")
		name = wallee_setup._provider_name()
		self.assertEqual(frappe.db.get_value("Wallee Settings", {"provider": name}, "space_id"), 12345)
		for channel in wallee_setup.CHANNEL_DRIVERS:
			self.assertTrue(
				frappe.db.exists("Provider Channel Settings", {"provider": name, "channel": channel})
			)
		states = {row["key"]: row["state"] for row in result["rows"]}
		self.assertEqual(states["accounting"], "ok")
		self.assertEqual(states["till"], "ok")
		self.assertEqual(states["webhook"], "ok")
		if frappe.db.get_value("Payment Provider", name, "mode") != "live":
			self.assertEqual(states["shop"], "waiting")

	def test_a_refused_key_is_rolled_back_not_committed(self):
		rolled = []
		with (
			patch.object(wallee_setup, "_health", return_value={"ok": False, "error": "refused"}),
			patch.object(frappe.db, "rollback", side_effect=lambda *a, **k: rolled.append(1)),
			as_role("head_accountant"),
		):
			result = wallee_setup.connect(space_id="999", user_id="111", authentication_key="_wrong")
		self.assertFalse(result["ok"])
		self.assertIn("nothing was changed", result["error"])
		self.assertEqual(rolled, [1], "a refused key must roll the transaction back")
		self.commit.assert_not_called()

	def test_going_to_production_makes_wallee_choosable(self):
		self.connect()
		with patch.object(wallee_setup, "_health", return_value={"ok": True}), as_role(
			"head_accountant"
		):
			status = wallee_setup.set_mode(mode="live")
		self.assertEqual(status["mode"], "live")
		self.assertEqual({r["key"]: r["state"] for r in status["rows"]}["shop"], "ok")


class TestWalleeSignedWebhook(FrappeTestCase):
	def setUp(self):
		from payments.drivers.registry import resolve_driver

		name = frappe.db.get_value(
			"Payment Provider",
			{"driver_class": ["like", "payments.drivers.wallee.%"], "enabled": 1},
			"name",
		)
		if not name or not frappe.db.exists(
			"Provider Channel Settings", {"provider": name, "channel": "terminal"}
		):
			self.skipTest("no Wallee terminal binding on this site")
		self.driver = resolve_driver(name, "terminal")
		self.key = ec.generate_private_key(ec.SECP256R1())
		public = self.key.public_key().public_bytes(
			serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
		)
		self.public_b64 = base64.b64encode(public).decode()
		self.key_id = f"t-{uuid.uuid4().hex[:12]}"
		frappe.cache.delete_value("payments:wallee_webhook_key_lookups")
		self.space = frappe.db.get_value("Wallee Settings", {"provider": name}, "space_id")
		self.payload = self.body(self.space)

	def body(self, space) -> bytes:  # noqa: ANN001
		return json.dumps(
			{
				"entityId": 987654321,
				"listenerEntityTechnicalName": "Transaction",
				"state": "FULFILL",
				"spaceId": space,
			}
		).encode()

	def deliver(self, payload: bytes, header: str, fetches=None):
		from wallee.service.webhook_encryption_keys_service import WebhookEncryptionKeysService

		def fetch(service, key_id):  # noqa: ANN001
			if fetches is not None:
				fetches.append(key_id)
			return self.public_b64 if key_id == self.key_id else None

		with (
			patch.object(type(self.driver._wallee_provider), "build_configuration", return_value=None),
			patch.object(WebhookEncryptionKeysService, "__init__", lambda self, configuration: None),
			patch.object(WebhookEncryptionKeysService, "get_webhooks_encryption_keys_id", fetch),
		):
			return self.driver.handle_webhook(payload, {"x-signature": header})

	def test_a_delivery_signed_by_wallee_is_proven(self):
		result = self.deliver(self.payload, _signed(self.payload, self.key, self.key_id))
		self.assertTrue(result.signature_valid)
		self.assertEqual(result.event_type, "transaction.fulfill")

	def test_a_tampered_delivery_is_refused(self):
		header = _signed(self.payload, self.key, self.key_id)
		tampered = self.payload.replace(b"FULFILL", b"DECLINE")
		result = self.deliver(tampered, header)
		self.assertFalse(result.signature_valid)
		self.assertEqual(result.error_code, "invalid_signature")

	def test_a_signature_from_another_key_is_refused(self):
		other = ec.generate_private_key(ec.SECP256R1())
		result = self.deliver(self.payload, _signed(self.payload, other, self.key_id))
		self.assertFalse(result.signature_valid)

	def test_the_public_key_is_asked_of_wallee_once(self):
		fetches = []
		header = _signed(self.payload, self.key, self.key_id)
		self.assertTrue(self.deliver(self.payload, header, fetches).signature_valid)
		self.assertTrue(self.deliver(self.payload, header, fetches).signature_valid)
		self.assertEqual(fetches, [self.key_id])

	def test_forged_key_ids_do_not_turn_into_a_stream_of_calls_to_wallee(self):
		fetches = []
		for _ in range(20):
			forged = f"algorithm=SHA256withECDSA, keyId=f-{uuid.uuid4().hex[:12]}, signature=AAAA"
			self.assertFalse(self.deliver(self.payload, forged, fetches).signature_valid)
		self.assertLessEqual(len(fetches), 6)

	def test_a_delivery_signed_by_wallee_for_another_space_is_refused(self):
		other = self.body(int(self.space) + 1)
		result = self.deliver(other, _signed(other, self.key, self.key_id))
		self.assertFalse(result.signature_valid)
		self.assertEqual(result.error_code, "foreign_space")

	def test_the_hand_set_secret_path_is_unchanged(self):
		with patch.object(type(self.driver._wallee_provider), "webhook_secret", new=None):
			result = self.driver.handle_webhook(self.payload, {"x-signature": "deadbeef"})
		self.assertFalse(result.signature_valid)
		self.assertEqual(result.error_code, "no_webhook_secret")
