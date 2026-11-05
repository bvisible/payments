# //// Neoffice — added file (no upstream equivalent). Audit of the payments app, second lot: the guest doors
# //// of the money path (webhooks, web checkout pivots, the upstream gateways no client uses). Each guard is
# //// proven from both sides: what must be refused, and the real journey that must still pass.
# Copyright (c) 2026, Neoffice and Contributors
# License: MIT. See LICENSE
"""A guest moves no money: a webhook proves itself, a checkout is its caller's, an idle gateway says nothing.

Nothing here reaches a provider: the SDK and HTTP calls are patched, and every signature is built with the
algorithm the driver checks, from a secret the test made up.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import random
import time
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.roles import as_role
from frappe.tests.utils import FrappeTestCase
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request

from payments.integrations.payrexx import api as payrexx_web
from payments.integrations.twint import api as twint_web
from payments.integrations.wallee import api as wallee_web

try:
	from webshop.controllers.payment_handler import PaymentHandler
	from webshop.webshop.shopping_cart import cart as shop_cart
	from webshop.webshop.shopping_cart.test_loyalty_rights import (
		GATEWAY,
		ITEM,
		STRANGER,
		USER,
		ShopFixture,
		upstream_checks,
	)
except ImportError:  # a site with no shop has no cart to pay
	ShopFixture = None

STRIPE_HANDLE = "payments.api.webhook_stripe.handle"
WALLEE_HANDLE = "payments.api.webhook_wallee.handle"
PAYREXX_HANDLE = "payments.api.webhook_payrexx.handle"
RAZORPAY = "payments.payment_gateways.doctype.razorpay_settings.razorpay_settings"
MPESA = "payments.payment_gateways.doctype.mpesa_settings.mpesa_settings"
PAYPAL = "payments.payment_gateways.doctype.paypal_settings.paypal_settings"

STRIPE_DRIVER = "payments.drivers.stripe.terminal_driver.StripeTerminalDriver"
WALLEE_DRIVER = "payments.drivers.wallee.terminal_driver.WalleeTerminalDriver"
PAYREXX_DRIVER = "payments.drivers.payrexx.web_driver.PayrexxWebDriver"

STRIPE_A = "auditp2_stripe_a"
STRIPE_B = "auditp2_stripe_b"
STRIPE_SECRETS = {STRIPE_A: "whsec_auditp2_a", STRIPE_B: "whsec_auditp2_b"}
WALLEE_SIGNED = "auditp2_wallee"
WALLEE_OPEN = "auditp2_wallee_open"
WALLEE_SECRET = "auditp2-wallee-secret"
PAYREXX_A = "auditp2_payrexx_a"
PAYREXX_B = "auditp2_payrexx_b"
PAYREXX_KEYS = {PAYREXX_A: "auditp2-signing-a", PAYREXX_B: "auditp2-signing-b"}


def fresh_ip() -> str:
	"""A rate limit counts per address and outlives a test."""
	return ".".join(["10", *(str(random.randint(1, 254)) for _ in range(3))])


def http_call(path: str, **kwargs):
	"""Call a method the way the HTTP handler does."""
	method = frappe.get_attr(path)
	frappe.is_whitelisted(method)
	return frappe.call(method, **kwargs)


@contextmanager
def a_request(body: bytes = b"", headers=None, cookie=None, ip=None, form=None):
	"""A POST in flight, with its raw body, its headers and its cookie; yields the response."""
	builder = EnvironBuilder(
		method="POST", data=body, headers=headers or {}, content_type="application/json"
	)
	environ = builder.get_environ()
	if cookie:
		environ["HTTP_COOKIE"] = cookie
	saved = {
		"response": frappe.local.response,
		"form_dict": frappe.local.form_dict,
		"request_ip": getattr(frappe.local, "request_ip", None),
	}
	frappe.local.request = Request(environ)
	frappe.local.request_ip = ip or fresh_ip()
	frappe.local.response = frappe._dict(docs=[])
	frappe.local.form_dict = frappe._dict(form or {})
	try:
		yield frappe.local.response
	finally:
		del frappe.local.request
		frappe.local.response = saved["response"]
		frappe.local.form_dict = saved["form_dict"]
		frappe.local.request_ip = saved["request_ip"]


def stripe_signature(body: bytes, secret: str) -> str:
	"""Stripe's scheme: HMAC-SHA256 of `<timestamp>.<body>`, in a `t=…,v1=…` header."""
	timestamp = int(time.time())
	signed = f"{timestamp}.{body.decode()}".encode()
	digest = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
	return f"t={timestamp},v1={digest}"


def hex_hmac(body: bytes, secret: str) -> str:
	"""The scheme of the Wallee driver and of Payrexx: HMAC-SHA256 of the raw body, in hexadecimal."""
	return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def stripe_event(intent_name: str, event_type: str = "payment_intent.succeeded") -> bytes:
	return json.dumps(
		{
			"id": f"evt_auditp2_{frappe.generate_hash(length=12)}",
			"type": event_type,
			"data": {
				"object": {
					"id": "pi_auditp2",
					"status": "succeeded",
					"metadata": {"frappe_intent_name": intent_name},
				}
			},
		}
	).encode()


def wallee_event(transaction_id: str, state: str = "COMPLETED") -> bytes:
	return json.dumps(
		{
			"entityId": int(transaction_id),
			"listenerEntityTechnicalName": "Transaction",
			"state": state,
			"spaceId": 100,
		}
	).encode()


def payrexx_event(intent_name: str, status: str = "confirmed") -> bytes:
	return json.dumps(
		{
			"transaction": {
				"id": 1,
				"uuid": f"auditp2-{frappe.generate_hash(length=12)}",
				"amount": 1000,
				"referenceId": intent_name,
				"status": status,
				"type": "E-Commerce",
				"invoice": {"currency": "CHF", "refundedAmount": 0},
			}
		}
	).encode()


def ensure_channel(code: str) -> None:
	if not frappe.db.exists("Payment Channel", code):
		frappe.get_doc(
			{
				"doctype": "Payment Channel",
				"channel_code": code,
				"display_label": code,
				"ui_kind": "redirect",
			}
		).insert(ignore_permissions=True)


def ensure_provider(name: str, driver: str, channel: str, credentials: dict) -> None:
	ensure_channel(channel)
	if not frappe.db.exists("Payment Provider", name):
		frappe.get_doc(
			{
				"doctype": "Payment Provider",
				"provider_name": name,
				"display_label": name,
				"enabled": 1,
				"mode": "test",
				"driver_class": driver,
				"credentials_json": json.dumps(credentials),
			}
		).insert(ignore_permissions=True)
	if not frappe.db.exists("Provider Channel Settings", {"provider": name, "channel": channel}):
		frappe.get_doc(
			{"doctype": "Provider Channel Settings", "provider": name, "channel": channel, "enabled": 1}
		).insert(ignore_permissions=True)


def ensure_wallee_settings(provider: str, secret: str | None) -> None:
	if not frappe.db.exists("Wallee Settings", provider):
		frappe.get_doc(
			{
				"doctype": "Wallee Settings",
				"provider": provider,
				"enabled": 1,
				"user_id": 42,
				"authentication_key": "auditp2-key",
				"space_id": 100,
				"api_host": "https://app-wallee.com/api/v2.0",
				"webhook_secret": secret,
			}
		).insert(ignore_permissions=True)


def make_newest(provider: str) -> None:
	"""The receivers used to read the most recently saved provider only."""
	frappe.db.set_value(
		"Payment Provider", provider, "modified", frappe.utils.now_datetime(), update_modified=False
	)
	frappe.db.commit()


def a_row(doctype: str, name: str, owner: str | None = None, children=None, **values) -> str:
	"""A row as the checkout leaves it, with no controller in the way: the decor, not the act."""
	doc = frappe.new_doc(doctype)
	doc.name = name
	doc.update(values)
	doc.db_insert()
	for table, rows in (children or {}).items():
		for idx, row in enumerate(rows, 1):
			child = doc.append(table, row)
			child.idx = idx
			child.db_insert()
	if owner:
		frappe.db.set_value(doctype, name, "owner", owner, update_modified=False)
	return name


class _WithIntents(FrappeTestCase):
	"""Webhooks commit: what a test makes is taken away by hand."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		for provider, secret in STRIPE_SECRETS.items():
			ensure_provider(
				provider,
				STRIPE_DRIVER,
				"terminal",
				{"secret_key": f"sk_test_{provider}", "webhook_secret": secret},
			)
		ensure_provider(WALLEE_SIGNED, WALLEE_DRIVER, "terminal", {})
		ensure_provider(WALLEE_OPEN, WALLEE_DRIVER, "terminal", {})
		ensure_wallee_settings(WALLEE_SIGNED, WALLEE_SECRET)
		ensure_wallee_settings(WALLEE_OPEN, None)
		for provider, key in PAYREXX_KEYS.items():
			ensure_provider(provider, PAYREXX_DRIVER, "payrexx_web", {"webhook_signing_key": key})
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		for provider in (WALLEE_SIGNED, WALLEE_OPEN):
			frappe.db.delete("Wallee Settings", {"name": provider})
			frappe.db.sql(
				"delete from `__Auth` where doctype = 'Wallee Settings' and name = %s", provider
			)
		for provider in (STRIPE_A, STRIPE_B, WALLEE_SIGNED, WALLEE_OPEN, PAYREXX_A, PAYREXX_B):
			frappe.db.delete("Provider Channel Settings", {"provider": provider})
			frappe.db.delete("Payment Provider", {"name": provider})
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		frappe.set_user("Administrator")
		self.intents = []
		self.log_names = []
		frappe.cache.delete_keys("payments_refused_delivery")

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		for name in self.intents:
			frappe.db.delete("Payment Event", {"intent": name})
			frappe.db.delete("Payment Intent", {"name": name})
		frappe.db.delete("Webhook Event Log", {"provider": ["like", "auditp2%"]})
		frappe.db.delete("Webhook Event Log", {"event_id": ["like", "%auditp2%"]})
		frappe.db.commit()

	def intent(self, provider: str, channel: str = "terminal", owner: str | None = None, **values) -> str:
		doc = frappe.get_doc(
			{
				"doctype": "Payment Intent",
				"status": "requires_action",
				"provider": provider,
				"channel": channel,
				"amount": 1000,
				"currency": "CHF",
				"provider_intent_id": f"{int(time.time() * 1000)}{random.randint(1000, 9999)}",
				**values,
			}
		).insert(ignore_permissions=True)
		if owner:
			frappe.db.set_value("Payment Intent", doc.name, "owner", owner, update_modified=False)
		frappe.db.commit()
		self.intents.append(doc.name)
		return doc.name

	def status_of(self, intent: str) -> str:
		return frappe.db.get_value("Payment Intent", intent, "status")

	def deliver(self, path: str, body: bytes, headers=None):
		"""POST a delivery as the provider's server does: no session, a raw body."""
		with a_request(body, headers) as response, patch("frappe.enqueue") as enqueue:
			with as_role("Guest"):
				answer = http_call(path)
		return SimpleNamespace(
			answer=answer, status=response.get("http_status_code", 200), enqueue=enqueue
		)

	def queued_log(self, delivery) -> str:
		self.assertEqual(delivery.status, 200, delivery.answer)
		delivery.enqueue.assert_called_once()
		return delivery.enqueue.call_args.kwargs["log_name"]

	def assert_refused(self, delivery) -> None:
		self.assertEqual(delivery.status, 400, delivery.answer)
		self.assertEqual(delivery.answer, "invalid signature")
		delivery.enqueue.assert_not_called()


class TestTheStripeWebhookTrustsOnlyItsSignature(_WithIntents):
	def setUp(self):
		super().setUp()
		make_newest(STRIPE_A)

	def run_worker(self, log_name: str) -> None:
		with as_role("Guest"):
			frappe.get_attr("payments.api.webhook_stripe.process_event")(log_name)

	def test_no_signature_moves_nothing_and_stores_nothing(self):
		intent = self.intent(STRIPE_A)
		body = stripe_event(intent)
		delivery = self.deliver(STRIPE_HANDLE, body)
		self.assert_refused(delivery)
		self.assertEqual(frappe.db.count("Webhook Event Log", {"provider": ["like", "auditp2%"]}), 0)
		self.assertEqual(self.status_of(intent), "requires_action")

	def test_an_empty_signature_is_no_signature(self):
		intent = self.intent(STRIPE_A)
		delivery = self.deliver(STRIPE_HANDLE, stripe_event(intent), {"Stripe-Signature": ""})
		self.assert_refused(delivery)
		self.assertEqual(self.status_of(intent), "requires_action")

	def test_a_wrong_signature_stores_no_row_that_says_it_was_valid(self):
		intent = self.intent(STRIPE_A)
		body = stripe_event(intent)
		delivery = self.deliver(
			STRIPE_HANDLE, body, {"Stripe-Signature": stripe_signature(body, "whsec_not_ours")}
		)
		self.assert_refused(delivery)
		self.assertEqual(frappe.db.count("Webhook Event Log", {"signature_valid": 1, "intent": intent}), 0)
		self.assertEqual(frappe.db.count("Webhook Event Log", {"provider": ["like", "auditp2%"]}), 0)

	def test_the_driver_itself_never_takes_a_body_without_its_signature(self):
		from payments.drivers.registry import resolve_driver

		driver = resolve_driver(STRIPE_A, "terminal")
		result = driver.handle_webhook(stripe_event("PI-ANY"), {})
		self.assertFalse(result.signature_valid)

	def test_a_real_signature_moves_the_intent_of_its_provider(self):
		make_newest(STRIPE_A)
		intent = self.intent(STRIPE_A)
		body = stripe_event(intent)
		delivery = self.deliver(
			STRIPE_HANDLE, body, {"Stripe-Signature": stripe_signature(body, STRIPE_SECRETS[STRIPE_A])}
		)
		log = self.queued_log(delivery)
		self.assertEqual(frappe.db.get_value("Webhook Event Log", log, "signature_valid"), 1)
		self.assertEqual(frappe.db.get_value("Webhook Event Log", log, "provider"), STRIPE_A)
		self.run_worker(log)
		self.assertEqual(self.status_of(intent), "succeeded")
		self.assertEqual(frappe.db.get_value("Webhook Event Log", log, "status"), "Processed")

	def test_the_provider_that_holds_the_intent_is_asked_not_the_last_one_saved(self):
		make_newest(STRIPE_A)
		intent = self.intent(STRIPE_B)
		body = stripe_event(intent)
		delivery = self.deliver(
			STRIPE_HANDLE, body, {"Stripe-Signature": stripe_signature(body, STRIPE_SECRETS[STRIPE_B])}
		)
		log = self.queued_log(delivery)
		self.assertEqual(frappe.db.get_value("Webhook Event Log", log, "provider"), STRIPE_B)
		self.run_worker(log)
		self.assertEqual(self.status_of(intent), "succeeded")

	def test_an_event_signed_for_one_provider_does_not_move_an_intent_of_another(self):
		make_newest(STRIPE_A)
		intent = self.intent(STRIPE_B)
		body = stripe_event(intent)
		delivery = self.deliver(
			STRIPE_HANDLE, body, {"Stripe-Signature": stripe_signature(body, STRIPE_SECRETS[STRIPE_A])}
		)
		log = self.queued_log(delivery)
		self.run_worker(log)
		self.assertEqual(self.status_of(intent), "requires_action")
		self.assertEqual(frappe.db.get_value("Webhook Event Log", log, "status"), "Skipped")

	def test_a_row_stored_without_a_valid_signature_moves_nothing(self):
		intent = self.intent(STRIPE_A)
		log = frappe.get_doc(
			{
				"doctype": "Webhook Event Log",
				"event_id": f"evt_auditp2_{frappe.generate_hash(length=12)}",
				"provider": STRIPE_A,
				"event_type": "payment_intent.succeeded",
				"signature_valid": 0,
				"status": "Queued",
				"raw_payload": stripe_event(intent).decode(),
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		self.run_worker(log.name)
		self.assertEqual(self.status_of(intent), "requires_action")

	def test_a_reader_event_is_captured_with_the_provider_that_signed_it(self):
		make_newest(STRIPE_B)
		body = json.dumps(
			{
				"id": f"evt_auditp2_{frappe.generate_hash(length=12)}",
				"type": "terminal.reader.action_succeeded",
				"data": {
					"object": {
						"id": "tmr_auditp2",
						"action": {"process_payment_intent": {"payment_intent": "pi_auditp2_reader"}},
					}
				},
			}
		).encode()
		delivery = self.deliver(
			STRIPE_HANDLE, body, {"Stripe-Signature": stripe_signature(body, STRIPE_SECRETS[STRIPE_A])}
		)
		log = self.queued_log(delivery)
		with patch("stripe.PaymentIntent.capture") as capture:
			capture.return_value = {"id": "pi_auditp2_reader"}
			self.run_worker(log)
		self.assertEqual(capture.call_args.args[0], "pi_auditp2_reader")
		self.assertEqual(capture.call_args.kwargs["api_key"], f"sk_test_{STRIPE_A}")

	def test_a_site_with_no_stripe_answers_like_a_wrong_signature_and_logs_once(self):
		frappe.db.delete("Error Log", {"method": ["like", "Stripe webhook%"]})
		frappe.db.commit()
		frappe.db.sql(
			"update `tabPayment Provider` set enabled = 0 where driver_class like 'payments.drivers.stripe.%'"
		)
		try:
			for _ in range(3):
				self.assert_refused(self.deliver(STRIPE_HANDLE, b"{}"))
			rows = frappe.db.count("Error Log", {"method": ["like", "Stripe webhook%"]})
		finally:
			frappe.db.rollback()
			frappe.db.delete("Error Log", {"method": ["like", "Stripe webhook%"]})
			frappe.db.commit()
		self.assertLessEqual(rows, 1)


class TestTheWalleeWebhookTrustsOnlyItsSignature(_WithIntents):
	def run_worker(self, log_name: str) -> None:
		with as_role("Guest"):
			frappe.get_attr("payments.api.webhook_wallee.process_event")(log_name)

	def test_a_test_provider_with_no_secret_refuses_instead_of_skipping_the_check(self):
		make_newest(WALLEE_OPEN)
		intent = self.intent(WALLEE_OPEN)
		transaction = frappe.db.get_value("Payment Intent", intent, "provider_intent_id")
		delivery = self.deliver(WALLEE_HANDLE, wallee_event(transaction))
		self.assert_refused(delivery)
		self.assertEqual(self.status_of(intent), "requires_action")
		self.assertEqual(frappe.db.count("Webhook Event Log", {"provider": ["like", "auditp2%"]}), 0)

	def test_no_signature_and_a_wrong_one_are_refused(self):
		make_newest(WALLEE_SIGNED)
		intent = self.intent(WALLEE_SIGNED)
		transaction = frappe.db.get_value("Payment Intent", intent, "provider_intent_id")
		body = wallee_event(transaction)
		self.assert_refused(self.deliver(WALLEE_HANDLE, body))
		self.assert_refused(
			self.deliver(WALLEE_HANDLE, body, {"X-Signature": hex_hmac(body, "not-the-secret")})
		)
		self.assertEqual(self.status_of(intent), "requires_action")

	def test_a_signature_that_is_not_ascii_is_a_refusal_not_a_crash(self):
		make_newest(WALLEE_SIGNED)
		self.assert_refused(self.deliver(WALLEE_HANDLE, wallee_event("1"), {"X-Signature": "caf\xe9"}))

	def test_a_real_signature_moves_the_intent_through_the_worker(self):
		make_newest(WALLEE_SIGNED)
		intent = self.intent(WALLEE_SIGNED)
		transaction = frappe.db.get_value("Payment Intent", intent, "provider_intent_id")
		body = wallee_event(transaction)
		delivery = self.deliver(WALLEE_HANDLE, body, {"X-Signature": hex_hmac(body, WALLEE_SECRET)})
		log = self.queued_log(delivery)
		self.run_worker(log)
		self.assertEqual(self.status_of(intent), "succeeded")
		self.assertEqual(frappe.db.get_value("Webhook Event Log", log, "intent"), intent)

	def test_the_provider_with_a_secret_is_found_when_another_was_saved_last(self):
		make_newest(WALLEE_OPEN)
		intent = self.intent(WALLEE_SIGNED)
		transaction = frappe.db.get_value("Payment Intent", intent, "provider_intent_id")
		body = wallee_event(transaction)
		delivery = self.deliver(WALLEE_HANDLE, body, {"X-Signature": hex_hmac(body, WALLEE_SECRET)})
		self.run_worker(self.queued_log(delivery))
		self.assertEqual(self.status_of(intent), "succeeded")

	def test_a_transaction_id_does_not_reach_an_intent_of_another_psp(self):
		make_newest(WALLEE_SIGNED)
		foreign = self.intent(STRIPE_A)
		transaction = frappe.db.get_value("Payment Intent", foreign, "provider_intent_id")
		body = wallee_event(transaction)
		headers = {"X-Signature": hex_hmac(body, WALLEE_SECRET)}
		from payments.drivers.registry import resolve_driver

		self.assertIsNone(resolve_driver(WALLEE_SIGNED, "terminal").handle_webhook(body, headers).intent_name)
		self.run_worker(self.queued_log(self.deliver(WALLEE_HANDLE, body, headers)))
		self.assertEqual(self.status_of(foreign), "requires_action")


class TestThePayrexxWebhookTrustsOnlyItsSignature(_WithIntents):
	def run_worker(self, log_name: str) -> None:
		with as_role("Guest"):
			frappe.get_attr("payments.api.webhook_payrexx.process_event")(log_name)

	def deliver_for(self, provider, body, headers=None):
		with patch("payments.api.webhook_payrexx._payrexx_provider_name", return_value=provider):
			return self.deliver(PAYREXX_HANDLE, body, headers)

	def test_no_signature_and_a_wrong_one_are_refused(self):
		intent = self.intent(PAYREXX_A, "payrexx_web")
		body = payrexx_event(intent)
		self.assert_refused(self.deliver_for(PAYREXX_A, body))
		self.assert_refused(
			self.deliver_for(PAYREXX_A, body, {"X-Webhook-Signature": hex_hmac(body, "not-the-key")})
		)
		self.assertEqual(self.status_of(intent), "requires_action")

	def test_a_real_signature_moves_the_intent(self):
		intent = self.intent(PAYREXX_A, "payrexx_web")
		body = payrexx_event(intent)
		delivery = self.deliver_for(
			PAYREXX_A, body, {"X-Webhook-Signature": hex_hmac(body, PAYREXX_KEYS[PAYREXX_A])}
		)
		self.run_worker(self.queued_log(delivery))
		self.assertEqual(self.status_of(intent), "succeeded")

	def test_a_missing_key_or_provider_reads_like_a_wrong_signature(self):
		body = payrexx_event("PI-ANY")
		with patch("payments.api.webhook_payrexx._signing_key", return_value=None):
			self.assert_refused(self.deliver_for(PAYREXX_A, body))
		self.assert_refused(self.deliver_for(None, body))

	def test_a_delivery_of_one_account_does_not_move_an_intent_of_another(self):
		intent = self.intent(PAYREXX_B, "payrexx_web")
		body = payrexx_event(intent)
		delivery = self.deliver_for(
			PAYREXX_A, body, {"X-Webhook-Signature": hex_hmac(body, PAYREXX_KEYS[PAYREXX_A])}
		)
		log = self.queued_log(delivery)
		self.run_worker(log)
		self.assertEqual(self.status_of(intent), "requires_action")
		self.assertEqual(frappe.db.get_value("Webhook Event Log", log, "status"), "Skipped")

	def test_a_driver_with_no_key_does_not_read_a_body_as_proven(self):
		from payments.api.webhook_payrexx import parse_delivery

		self.assertFalse(parse_delivery(payrexx_event("PI-ANY"), {}, signing_key=None).signature_valid)


class TestTheStatusEventReachesWhoStartedThePayment(_WithIntents):
	def worker_publishes(self, owner: str) -> list:
		make_newest(STRIPE_A)
		intent = self.intent(STRIPE_A, owner=owner)
		body = stripe_event(intent)
		delivery = self.deliver(
			STRIPE_HANDLE, body, {"Stripe-Signature": stripe_signature(body, STRIPE_SECRETS[STRIPE_A])}
		)
		log = self.queued_log(delivery)
		with patch("frappe.publish_realtime") as publish:
			with as_role("Guest"):
				frappe.get_attr("payments.api.webhook_stripe.process_event")(log)
		self.assertEqual(self.status_of(intent), "succeeded")
		return [call for call in publish.call_args_list if "payment.intent." in str(call)]

	def test_the_cashier_hears_of_their_payment_and_nobody_else(self):
		calls = self.worker_publishes("cashier-auditp2@example.com")
		self.assertEqual(len(calls), 1)
		self.assertEqual(calls[0].kwargs.get("user"), "cashier-auditp2@example.com")

	def test_a_guest_payment_is_published_to_no_room_of_all_guests(self):
		# Every guest socket sits in the room of the user Guest; their page polls with its token.
		self.assertEqual(self.worker_publishes("Guest"), [])

	def test_the_twint_poller_publishes_the_same_way(self):
		from payments.api import twint

		doc = SimpleNamespace(name="PI-AUDITP2", owner="cashier-auditp2@example.com")
		with patch("frappe.publish_realtime") as publish:
			twint._publish_intent_update(doc, "qr_bridge", "succeeded")
		self.assertEqual(publish.call_args.kwargs.get("user"), "cashier-auditp2@example.com")


class TestAWebCheckoutIsTheCallersOwn(FrappeTestCase):
	"""The three pivots of the shop: a cart's request, opened by the browser that holds that cart."""

	PIVOTS = {
		"twint": ("payments.integrations.twint.api.create_web_transaction", twint_web, "twint_web"),
		"payrexx": ("payments.integrations.payrexx.api.create_web_transaction", payrexx_web, "payrexx_web"),
		"wallee": ("payments.integrations.wallee.api.create_web_transaction", wallee_web, "wallee_web"),
	}
	CREATED = {
		"intent_name": "PI-AUDITP2",
		"status": "requires_action",
		"provider_intent_id": "1",
		"next_action_payload": {"url": "https://pay.example/x", "pairing_token": "1", "qr_svg": "<svg/>"},
	}

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if "webshop" not in frappe.get_installed_apps():
			raise unittest.SkipTest("a cart belongs to the webshop app")

	def setUp(self):
		frappe.set_user("Administrator")
		self.marker = frappe.generate_hash(length=10)
		self.session_id = f"auditp2-{self.marker}"
		self.quotation = a_row(
			"Quotation",
			f"AUDITP2-QTN-{self.marker}",
			guest_session_id=self.session_id,
			quotation_to="Customer",
			currency="CHF",
			grand_total=30,
			children={
				"items": [{"item_code": "AUDITP2", "item_name": "Thing", "qty": 2, "amount": 30}],
			},
		)
		self.own = self.request("OWN", reference_doctype="Quotation", reference_name=self.quotation)
		self.foreign = self.request(
			"FOREIGN", reference_doctype="Sales Invoice", reference_name="AUDITP2-SI", status="Requested"
		)

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def request(self, suffix: str, owner: str | None = None, **values) -> str:
		defaults = {
			"payment_request_type": "Inward",
			"grand_total": 30,
			"currency": "CHF",
			"status": "Draft",
			"from_checkout": 1,
		}
		return a_row(
			"Payment Request", f"AUDITP2-PR-{suffix}-{self.marker}", owner=owner, **{**defaults, **values}
		)

	def pivot(self, name: str, request: str, cookie: str | None = None, ip: str | None = None, **kwargs):
		path, module, _channel = self.PIVOTS[name]
		resolvers = {
			"twint": "_resolve_twint_provider_name",
			"payrexx": "_resolve_provider_name",
			"wallee": "_resolve_wallee_provider_name",
		}
		patches = [
			patch("payments.api.intent.create_intent", return_value=self.CREATED),
			patch.object(module, resolvers[name], return_value="mock"),
		]
		if name == "twint":
			patches.append(patch.object(module, "_resolve_merchant_uuid_for_provider", return_value="m"))
		mocks = [p.start() for p in patches]
		self.created = mocks[0]
		try:
			with a_request(cookie=cookie, ip=ip, form={"cmd": path}):
				return http_call(path, payment_request_id=request, **kwargs)
		finally:
			for p in reversed(patches):
				p.stop()

	def cookie(self) -> str:
		return f"guest_session_id={self.session_id}"

	def test_the_guest_who_holds_the_cart_pays(self):
		for name in self.PIVOTS:
			with self.subTest(pivot=name), as_role("Guest"):
				result = self.pivot(name, self.own, cookie=self.cookie())
				self.assertEqual(result["status"], "success")
				self.created.assert_called_once()
				self.assertEqual(self.created.call_args.kwargs["amount"], 3000)

	def test_the_request_on_the_carts_placeholder_order_is_the_buyers_too(self):
		"""The shop raises its request on `PENDING-<quotation>`: upstream refuses a Quotation."""
		placeholder = a_row(
			"Sales Order",
			f"PENDING-{self.quotation}",
			currency="CHF",
			children={
				"items": [{"item_code": "AUDITP2", "item_name": "Thing", "qty": 2, "amount": 30}],
			},
		)
		held = self.request("HELD", reference_doctype="Sales Order", reference_name=placeholder)
		for name in self.PIVOTS:
			with self.subTest(pivot=name), as_role("Guest"):
				self.assertEqual(self.pivot(name, held, cookie=self.cookie())["status"], "success")
				with self.assertRaises(frappe.PermissionError):
					self.pivot(name, held, cookie="guest_session_id=someone-else")

	def test_another_browser_is_refused_before_anything_is_written(self):
		for name in self.PIVOTS:
			for cookie in (None, "guest_session_id=someone-else"):
				with self.subTest(pivot=name, cookie=cookie), as_role("Guest"):
					with self.assertRaises(frappe.PermissionError):
						self.pivot(name, self.own, cookie=cookie)
					self.created.assert_not_called()

	def test_a_request_raised_from_the_desk_is_not_a_guests(self):
		for name in self.PIVOTS:
			with self.subTest(pivot=name), as_role("Guest"):
				with self.assertRaises(frappe.PermissionError):
					self.pivot(name, self.foreign, cookie=self.cookie())
				self.created.assert_not_called()

	def test_a_name_that_exists_and_one_that_does_not_answer_the_same(self):
		for name in self.PIVOTS:
			with self.subTest(pivot=name), as_role("Guest"):
				with self.assertRaises(frappe.PermissionError):
					self.pivot(name, f"AUDITP2-NO-SUCH-{self.marker}", cookie=self.cookie())
				self.created.assert_not_called()

	def test_a_filter_in_place_of_a_name_is_refused_on_live_traffic_too(self):
		# Live traffic only reports a wrong type on a module with deferred annotations, it does not refuse it.
		with patch("frappe.utils.typing_validations._enforce_deferred_annotations", return_value=False):
			for name in self.PIVOTS:
				with self.subTest(pivot=name), as_role("Guest"):
					with self.assertRaises(frappe.PermissionError):
						self.pivot(name, {"status": "Draft"}, cookie=self.cookie())
					self.created.assert_not_called()

	def test_an_outward_request_is_refused_whoever_asks(self):
		outward = self.request("OUT", payment_request_type="Outward")
		for name in self.PIVOTS:
			with self.subTest(pivot=name):
				with self.assertRaises(frappe.PermissionError):
					self.pivot(name, outward)

	def test_a_closed_request_tells_its_owner_and_starts_nothing(self):
		for status in ("Paid", "Cancelled"):
			closed = self.request(
				status, reference_doctype="Quotation", reference_name=self.quotation, status=status
			)
			for name in self.PIVOTS:
				with self.subTest(pivot=name, status=status), as_role("Guest"):
					result = self.pivot(name, closed, cookie=self.cookie())
					self.assertEqual(result["status"], "error")
					self.created.assert_not_called()

	def test_a_signed_in_buyer_pays_the_request_of_their_own_session(self):
		with as_role("portal_customer") as user:
			mine = self.request(
				"MINE", owner=user, reference_doctype="Sales Invoice", reference_name="AUDITP2-SI"
			)
			for name in self.PIVOTS:
				with self.subTest(pivot=name):
					self.assertEqual(self.pivot(name, mine)["status"], "success")

	def test_a_signed_in_stranger_is_refused(self):
		with as_role("portal_customer"):
			for name in self.PIVOTS:
				with self.subTest(pivot=name):
					with self.assertRaises(frappe.PermissionError):
						self.pivot(name, self.foreign)

	def test_a_desk_user_allowed_to_read_the_request_opens_it(self):
		with as_role("accountant"):
			for name in self.PIVOTS:
				with self.subTest(pivot=name):
					self.assertEqual(self.pivot(name, self.foreign)["status"], "success")

	def test_a_guest_does_not_choose_the_provider(self):
		for name in self.PIVOTS:
			with self.subTest(pivot=name), as_role("Guest"):
				with self.assertRaises(frappe.PermissionError):
					self.pivot(name, self.own, cookie=self.cookie(), provider="mock")
				self.created.assert_not_called()

	def test_an_address_hammering_a_pivot_is_stopped(self):
		for name in self.PIVOTS:
			ip = fresh_ip()
			with self.subTest(pivot=name), as_role("Guest"):
				with self.assertRaises(frappe.RateLimitExceededError):
					for _ in range(60):
						self.pivot(name, self.own, cookie=self.cookie(), ip=ip)

	def test_a_request_with_too_many_open_attempts_starts_no_more(self):
		for number in range(25):
			a_row(
				"Payment Intent",
				f"AUDITP2-PI-{number}-{self.marker}",
				status="requires_action",
				provider="mock",
				channel="twint_web",
				amount=3000,
				currency="CHF",
				reference_doctype="Payment Request",
				reference_name=self.own,
				provider_intent_id=f"auditp2-{number}-{self.marker}",
			)
		with as_role("Guest"):
			result = self.pivot("twint", self.own, cookie=self.cookie())
		self.assertEqual(result["status"], "error")
		self.created.assert_not_called()

		# An attempt that ended gives its place back: the buyer who cancelled a QR tries again.
		frappe.db.set_value(
			"Payment Intent",
			{"reference_name": self.own, "name": ["like", f"AUDITP2-PI-1_-{self.marker}"]},
			"status",
			"canceled",
		)
		with as_role("Guest"):
			self.assertEqual(self.pivot("twint", self.own, cookie=self.cookie())["status"], "success")

	# ---------------------------------------------- what Wallee is told to charge

	def test_the_buyer_does_not_write_the_lines_wallee_charges(self):
		forged = [
			{"name": "x", "unique_id": "x", "quantity": 1, "amount_including_tax": 0.01, "type": "PRODUCT"}
		]
		with as_role("Guest"):
			self.pivot("wallee", self.own, cookie=self.cookie(), line_items=forged)
		metadata = self.created.call_args.kwargs["metadata"]
		self.assertEqual([row["amount_including_tax"] for row in metadata["line_items"]], [30.0])
		self.assertEqual(self.created.call_args.kwargs["amount"], 3000)

	def test_lines_that_do_not_add_up_to_the_request_give_way_to_its_amount(self):
		short = a_row(
			"Quotation",
			f"AUDITP2-QTN2-{self.marker}",
			guest_session_id=self.session_id,
			quotation_to="Customer",
			currency="CHF",
			children={"items": [{"item_code": "AUDITP2", "item_name": "Thing", "qty": 1, "amount": 25}]},
		)
		request = self.request("SHORT", reference_doctype="Quotation", reference_name=short)
		with as_role("Guest"):
			self.pivot("wallee", request, cookie=self.cookie())
		self.assertFalse(self.created.call_args.kwargs["metadata"]["line_items"])
		self.assertEqual(self.created.call_args.kwargs["amount"], 3000)

	def test_the_return_addresses_stay_on_this_site(self):
		with as_role("Guest"):
			self.pivot(
				"wallee",
				self.own,
				cookie=self.cookie(),
				success_url="https://elsewhere.example/wallee/success",
				failed_url="http://localhost/wallee/failed?payment_request=SOMEONE-ELSES",
			)
		metadata = self.created.call_args.kwargs["metadata"]
		self.assertNotIn("elsewhere.example", metadata["success_url"])
		self.assertTrue(metadata["success_url"].endswith(f"/wallee/success?payment_request={self.own}"))
		self.assertEqual(metadata["failed_url"], f"http://localhost/wallee/failed?payment_request={self.own}")

	# ---------------------------------------------- the Wallee script addresses

	def script_url(self, transaction_id, cookie=None, **kwargs):
		service = MagicMock()
		service.return_value.get_payment_transactions_id_lightbox_javascript_url.return_value = "https://js"
		path = "payments.integrations.wallee.api.get_lightbox_javascript_url"
		with (
			patch("wallee.TransactionsService", service),
			patch.object(wallee_web, "_provider", return_value=MagicMock(space_id=1)),
			a_request(cookie=cookie, form={"cmd": path}),
		):
			try:
				return http_call(path, transaction_id=transaction_id, **kwargs)
			finally:
				self.wallee_calls = service.return_value.method_calls

	def test_an_anonymous_transaction_id_makes_no_call_to_wallee(self):
		for transaction_id in (1, "abc", {"a": 1}):
			with self.subTest(transaction_id=transaction_id), as_role("Guest"):
				with patch(
					"frappe.utils.typing_validations._enforce_deferred_annotations", return_value=False
				), self.assertRaises(frappe.PermissionError):
					self.script_url(transaction_id, cookie=self.cookie())
				self.assertEqual(self.wallee_calls, [])

	def test_the_buyer_reads_the_script_of_their_own_transaction(self):
		a_row(
			"Payment Intent",
			f"AUDITP2-PI-{self.marker}",
			status="requires_action",
			provider="mock",
			channel="wallee_web",
			amount=3000,
			currency="CHF",
			reference_doctype="Payment Request",
			reference_name=self.own,
			provider_intent_id="987654321",
		)
		with as_role("Guest"):
			self.assertEqual(self.script_url(987654321, cookie=self.cookie()), "https://js")
			with self.assertRaises(frappe.PermissionError):
				self.script_url(987654321, cookie="guest_session_id=someone-else")


@unittest.skipIf(ShopFixture is None, "a cart belongs to the webshop app")
class TestARealCartReachesItsPivot(ShopFixture or FrappeTestCase):
	"""The whole road of a buyer: the cart, the shop's own handler, then the pivot the tile calls."""

	def pay(self, pivot: str, request: str):
		path, module, _channel = TestAWebCheckoutIsTheCallersOwn.PIVOTS[pivot]
		created = TestAWebCheckoutIsTheCallersOwn.CREATED
		resolver = {
			"twint": "_resolve_twint_provider_name",
			"payrexx": "_resolve_provider_name",
			"wallee": "_resolve_wallee_provider_name",
		}[pivot]
		merchant = "_resolve_merchant_uuid_for_provider" if pivot == "twint" else resolver
		with (
			patch("payments.api.intent.create_intent", return_value=created),
			patch.object(module, resolver, return_value="mock"),
			patch.object(module, merchant, return_value="mock"),
			a_request(form={"cmd": path}),
		):
			return http_call(path, payment_request_id=request)

	def test_the_signed_in_buyer_pays_the_request_the_shop_raised_and_nobody_else_does(self):
		frappe.set_user(USER)
		with upstream_checks():
			shop_cart.update_cart(ITEM, 1)
			shop_cart.get_cart_quotation()
			quotation = shop_cart._get_cart_quotation()
			raised = PaymentHandler().create_payment_request(
				quotation_id=quotation.name, payment_gateway=GATEWAY
			)
		self.assertEqual(raised["status"], "success", raised)
		request = raised["payment_request_id"]
		for pivot in ("twint", "payrexx", "wallee"):
			with self.subTest(pivot=pivot):
				self.assertEqual(self.pay(pivot, request)["status"], "success")

		frappe.set_user(STRANGER)
		for pivot in ("twint", "payrexx", "wallee"):
			with self.subTest(pivot=pivot), self.assertRaises(frappe.PermissionError):
				self.pay(pivot, request)

		frappe.set_user("Guest")
		for pivot in ("twint", "payrexx", "wallee"):
			with self.subTest(pivot=pivot), self.assertRaises(frappe.PermissionError):
				self.pay(pivot, request)
		frappe.set_user("Administrator")


class TestAGatewayNoOneConfiguredAnswersNothing(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.assertFalse(frappe.db.get_single_value("Razorpay Settings", "api_key"))
		self.assertFalse(frappe.db.get_single_value("PayPal Settings", "api_username"))

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def errors(self, title: str) -> int:
		return frappe.db.count("Error Log", {"method": title})

	def test_razorpay_order_failure_writes_nothing(self):
		before = self.errors("Razorpay Payment Failure")
		with as_role("Guest"), a_request(form={"cmd": f"{RAZORPAY}.order_payment_failure"}):
			with self.assertRaises(frappe.PermissionError):
				http_call(f"{RAZORPAY}.order_payment_failure", integration_request="x", params='{"a": 1}')
		self.assertEqual(self.errors("Razorpay Payment Failure"), before)

	def test_razorpay_success_and_order_are_refused(self):
		with as_role("Guest"), a_request(form={"cmd": f"{RAZORPAY}.order_payment_success"}):
			with self.assertRaises(frappe.PermissionError):
				http_call(f"{RAZORPAY}.order_payment_success", integration_request="x", params="{}")
		with as_role("Guest"), a_request(form={"cmd": f"{RAZORPAY}.get_order"}):
			with self.assertRaises(frappe.PermissionError):
				http_call(f"{RAZORPAY}.get_order", doctype="User", docname="Administrator")

	def test_a_filter_in_place_of_a_name_stops_at_the_annotation(self):
		with as_role("Guest"), a_request(form={"cmd": f"{RAZORPAY}.order_payment_failure"}):
			with self.assertRaises(frappe.FrappeTypeError):
				http_call(
					f"{RAZORPAY}.order_payment_failure",
					integration_request={"name": "no-such-request"},
					params="null",
				)

	def test_razorpay_subscription_callback_calls_no_one(self):
		form = {
			"cmd": f"{RAZORPAY}.razorpay_subscription_callback",
			"payload": {"subscription": {"entity": {"id": "sub_x"}}},
		}
		with patch(f"{RAZORPAY}.make_get_request") as outbound:
			with as_role("Guest"), a_request(form=form):
				with self.assertRaises(frappe.PermissionError):
					http_call(f"{RAZORPAY}.razorpay_subscription_callback")
		outbound.assert_not_called()

	def test_an_anonymous_address_cannot_hammer_razorpay(self):
		path = f"{RAZORPAY}.order_payment_failure"
		with as_role("Guest"), a_request(form={"cmd": path}):
			with self.assertRaises(frappe.RateLimitExceededError):
				for _ in range(120):
					try:
						http_call(path, integration_request="x", params="{}")
					except frappe.PermissionError:
						pass

	def test_paypal_pages_and_ipn_call_no_one(self):
		before = self.errors("Error in PayPal payment processing")
		with patch(f"{PAYPAL}.make_post_request") as outbound, as_role("Guest"):
			for method, kwargs in (
				("get_express_checkout_details", {"token": "x"}),
				("confirm_payment", {"token": "x"}),
				("create_recurring_profile", {"token": "x", "payerid": "y"}),
				("ipn_handler", {}),
			):
				form = {"cmd": f"{PAYPAL}.{method}", "recurring_payment_id": "I-X"}
				with self.subTest(method=method), a_request(form=form):
					with self.assertRaises(frappe.PermissionError):
						http_call(f"{PAYPAL}.{method}", **kwargs)
		outbound.assert_not_called()
		self.assertEqual(self.errors("Error in PayPal payment processing"), before)

	def test_a_payment_url_writes_no_request_for_an_idle_gateway(self):
		with (
			patch(f"{RAZORPAY}.create_request_log") as logged,
			patch(f"{PAYPAL}.make_post_request") as outbound,
		):
			for doctype in ("Razorpay Settings", "PayPal Settings"):
				with self.subTest(doctype=doctype), self.assertRaises(frappe.PermissionError):
					frappe.get_doc(doctype).get_payment_url(amount=1, currency="CHF", title="t")
		logged.assert_not_called()
		outbound.assert_not_called()


def authorized(run_method) -> int:
	"""How many times the settled document was told; `run_method` also carries every save's hooks."""
	return sum(1 for call in run_method.call_args_list if call.args[:1] == ("on_payment_authorized",))


class _WithIntegrationRequests(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.marker = frappe.generate_hash(length=10)
		self.made = []
		self.request_row = a_row(
			"Payment Request",
			f"AUDITP2-PR-{self.marker}",
			payment_request_type="Inward",
			grand_total=500,
			currency="KES",
			status="Requested",
			reference_doctype="Sales Invoice",
			reference_name="AUDITP2-SI",
		)
		frappe.db.commit()
		frappe.flags.integration_request = SimpleNamespace(status_code=200)

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		for name in self.made:
			frappe.db.delete("Integration Request", {"name": name})
		frappe.db.delete("Payment Request", {"name": self.request_row})
		frappe.db.commit()

	def integration_request(self, service: str, data=None, status: str = "Queued") -> str:
		doc = frappe.get_doc(
			{
				"doctype": "Integration Request",
				"integration_request_service": service,
				"status": status,
				"reference_doctype": "Payment Request",
				"reference_docname": self.request_row,
				"data": json.dumps(data or {}),
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		self.made.append(doc.name)
		return doc.name


class TestRazorpayWhenItIsConfigured(_WithIntegrationRequests):
	SECRET = "auditp2-razorpay-secret"

	def setUp(self):
		super().setUp()
		conf = patch.dict(
			frappe.local.conf, {"sandbox_api_key": "rzp_test_auditp2", "sandbox_api_secret": self.SECRET}
		)
		conf.start()
		self.addCleanup(conf.stop)

	def order_request(self, **values) -> str:
		data = {
			"amount": 50000,
			"currency": "INR",
			"razorpay_order_id": "order_auditp2",
			"use_sandbox": 1,
			"reference_doctype": "Payment Request",
			"reference_docname": self.request_row,
			**values,
		}
		return self.integration_request("Razorpay", data)

	def signed_params(self, payment_id: str = "pay_auditp2", order_id: str = "order_auditp2", **extra) -> str:
		signature = hmac.new(
			self.SECRET.encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256
		).hexdigest()
		return json.dumps(
			{
				"razorpay_payment_id": payment_id,
				"razorpay_order_id": order_id,
				"razorpay_signature": signature,
				**extra,
			}
		)

	def succeed(self, name: str, params: str, payment: dict | None = None):
		payment = payment or {
			"status": "captured",
			"amount": 50000,
			"currency": "INR",
			"order_id": "order_auditp2",
		}
		path = f"{RAZORPAY}.order_payment_success"
		with (
			patch(f"{RAZORPAY}.make_get_request", return_value=payment) as outbound,
			patch("frappe.model.document.Document.run_method") as run_method,
			as_role("Guest"),
			a_request(form={"cmd": path}),
		):
			http_call(path, integration_request=name, params=params)
		return outbound, run_method

	def test_a_real_signature_settles_the_request_it_was_made_for(self):
		name = self.order_request()
		_outbound, run_method = self.succeed(name, self.signed_params())
		self.assertEqual(authorized(run_method), 1)
		self.assertEqual(frappe.db.get_value("Integration Request", name, "status"), "Completed")

	def test_a_wrong_signature_settles_nothing(self):
		name = self.order_request()
		params = json.dumps({"razorpay_payment_id": "pay_auditp2", "razorpay_signature": "forged"})
		with self.assertRaises(frappe.PermissionError):
			self.succeed(name, params)
		self.assertEqual(frappe.db.get_value("Integration Request", name, "status"), "Queued")

	def test_a_payment_of_another_amount_settles_nothing(self):
		name = self.order_request()
		small = {"status": "captured", "amount": 100, "currency": "INR", "order_id": "order_auditp2"}
		_outbound, run_method = self.succeed(name, self.signed_params(), small)
		self.assertEqual(authorized(run_method), 0)
		self.assertEqual(frappe.db.get_value("Integration Request", name, "status"), "Queued")

	def test_the_caller_does_not_rewrite_what_the_request_settles(self):
		name = self.order_request()
		params = self.signed_params(reference_docname="SOMEONE-ELSES", amount=1, redirect_to="https://x")
		self.succeed(name, params)
		stored = json.loads(frappe.db.get_value("Integration Request", name, "data"))
		self.assertEqual(stored["reference_docname"], self.request_row)
		self.assertEqual(stored["amount"], 50000)
		self.assertNotIn("redirect_to", stored)

	def test_a_concluded_request_is_not_settled_twice(self):
		name = self.order_request()
		self.succeed(name, self.signed_params())
		with self.assertRaises(frappe.PermissionError):
			self.succeed(name, self.signed_params())

	def test_an_integration_request_of_another_gateway_is_refused(self):
		name = self.integration_request("PayPal", {"amount": 1})
		with self.assertRaises(frappe.PermissionError):
			self.succeed(name, self.signed_params())

	def test_the_checkout_page_settles_only_its_own_token(self):
		name = self.integration_request(
			"Razorpay",
			{
				"amount": 500,
				"currency": "INR",
				"use_sandbox": 1,
				"reference_doctype": "Payment Request",
				"reference_docname": self.request_row,
			},
		)
		payment = {"status": "captured", "amount": 50000, "currency": "INR", "notes": {"token": name}}
		settings = frappe.get_doc("Razorpay Settings")
		with (
			patch(f"{RAZORPAY}.make_get_request", return_value=payment),
			patch("frappe.model.document.Document.run_method") as run_method,
		):
			settings.create_request(
				{
					"token": name,
					"razorpay_payment_id": "pay_auditp2",
					"reference_doctype": "Payment Request",
					"reference_docname": "SOMEONE-ELSES",
					"amount": 1,
				}
			)
		self.assertEqual(authorized(run_method), 1)
		stored = json.loads(frappe.db.get_value("Integration Request", name, "data"))
		self.assertEqual(stored["reference_docname"], self.request_row)
		self.assertEqual(stored["amount"], 500)

	def test_a_payment_made_for_another_token_settles_nothing_on_the_checkout_page(self):
		name = self.integration_request(
			"Razorpay",
			{"amount": 500, "currency": "INR", "use_sandbox": 1, "reference_doctype": "Payment Request",
			 "reference_docname": self.request_row},
		)
		payment = {"status": "captured", "amount": 50000, "currency": "INR", "notes": {"token": "another"}}
		with (
			patch(f"{RAZORPAY}.make_get_request", return_value=payment),
			patch("frappe.model.document.Document.run_method") as run_method,
		):
			frappe.get_doc("Razorpay Settings").create_request(
				{
					"token": name,
					"razorpay_payment_id": "pay_auditp2",
					"reference_doctype": "Payment Request",
					"reference_docname": self.request_row,
				}
			)
		self.assertEqual(authorized(run_method), 0)
		self.assertEqual(frappe.db.get_value("Integration Request", name, "status"), "Queued")


class TestTheMpesaCallbacks(_WithIntegrationRequests):
	def setUp(self):
		super().setUp()
		if not frappe.db.exists("Mpesa Settings", {"consumer_key": ["is", "set"]}):
			a_row(
				"Mpesa Settings",
				f"auditp2-{self.marker}",
				payment_gateway_name=f"auditp2-{self.marker}",
				consumer_key="k",
				till_number="1",
			)
			frappe.db.commit()
			self.addCleanup(
				lambda: (
					frappe.db.delete("Mpesa Settings", {"name": f"auditp2-{self.marker}"}),
					frappe.db.commit(),
				)
			)

	def callback(self, request_id, result_code=0, amount=500):
		return {
			"Body": {
				"stkCallback": {
					"MerchantRequestID": "19465-780693-1",
					"CheckoutRequestID": request_id,
					"ResultCode": result_code,
					"ResultDesc": "auditp2",
					"CallbackMetadata": {
						"Item": [
							{"Name": "Amount", "Value": amount},
							{"Name": "MpesaReceiptNumber", "Value": "AUDITP2"},
						]
					},
				}
			}
		}

	def send(self, **kwargs):
		with (
			patch("frappe.model.document.Document.run_method") as run_method,
			patch("frappe.publish_realtime"),
			as_role("Guest"),
			a_request(form={"cmd": f"{MPESA}.verify_transaction"}),
		):
			http_call(f"{MPESA}.verify_transaction", **kwargs)
		return run_method

	def test_a_request_of_another_gateway_is_refused(self):
		name = self.integration_request("PayPal", {"payment_reference": "x"})
		with self.assertRaises(frappe.PermissionError):
			self.send(**self.callback(name))
		self.assertEqual(frappe.db.get_value("Integration Request", name, "status"), "Queued")

	def test_a_real_mpesa_request_is_concluded(self):
		name = self.integration_request("Mpesa", {"payment_reference": "x", "request_amount": 500})
		run_method = self.send(**self.callback(name))
		self.assertEqual(authorized(run_method), 1)
		self.assertEqual(frappe.db.get_value("Integration Request", name, "status"), "Completed")

	def test_the_amount_is_what_this_site_asked_for(self):
		name = self.integration_request("Mpesa", {"payment_reference": "x", "request_amount": 10})
		run_method = self.send(**self.callback(name, amount=500))
		self.assertEqual(authorized(run_method), 0)

	def test_a_concluded_request_is_not_replayed(self):
		name = self.integration_request("Mpesa", {"payment_reference": "x"}, status="Completed")
		with self.assertRaises(frappe.PermissionError):
			self.send(**self.callback(name))

	def test_a_junk_body_is_refused_not_a_crash(self):
		for kwargs in ({}, {"Body": "x"}, {"Body": {"stkCallback": "x"}}):
			with self.subTest(kwargs=kwargs), self.assertRaises(frappe.PermissionError):
				self.send(**kwargs)
		with self.assertRaises(frappe.ValidationError):  # upstream's own answer to a missing id
			self.send(Body={"stkCallback": {}})

	def test_a_site_with_no_mpesa_account_refuses(self):
		frappe.db.sql("update `tabMpesa Settings` set consumer_key = null")
		try:
			with self.assertRaises(frappe.PermissionError):
				self.send(**self.callback("no-such-request"))
		finally:
			frappe.db.rollback()

	def test_a_balance_callback_of_another_gateway_is_refused(self):
		name = self.integration_request("PayPal", {})
		result = {"Result": {"ConversationID": name, "ResultCode": 1}}
		with as_role("Guest"), a_request(form={"cmd": f"{MPESA}.process_balance_info"}):
			with self.assertRaises(frappe.PermissionError):
				http_call(f"{MPESA}.process_balance_info", **result)
		self.assertEqual(frappe.db.get_value("Integration Request", name, "status"), "Queued")

	def test_a_real_balance_callback_passes(self):
		name = self.integration_request("Mpesa", {})
		result = {"Result": {"ConversationID": name, "ResultCode": 1}}
		with as_role("Guest"), a_request(form={"cmd": f"{MPESA}.process_balance_info"}):
			http_call(f"{MPESA}.process_balance_info", **result)
		self.assertEqual(frappe.db.get_value("Integration Request", name, "status"), "Failed")


class TestPayPalWhenItIsConfigured(_WithIntegrationRequests):
	def setUp(self):
		super().setUp()
		conf = patch.dict(frappe.local.conf, {"sandbox_api_username": "auditp2"})
		conf.start()
		self.addCleanup(conf.stop)

	def test_a_junk_token_writes_no_error_row(self):
		before = frappe.db.count("Error Log", {"method": "Error in PayPal payment processing"})
		with patch(f"{PAYPAL}.make_post_request") as outbound, as_role("Guest"):
			with a_request(form={"cmd": f"{PAYPAL}.confirm_payment"}):
				with self.assertRaises(frappe.PermissionError):
					http_call(f"{PAYPAL}.confirm_payment", token="no-such-token")
		outbound.assert_not_called()
		self.assertEqual(
			frappe.db.count("Error Log", {"method": "Error in PayPal payment processing"}), before
		)

	def test_a_request_of_another_gateway_is_refused(self):
		name = self.integration_request("Stripe", {"amount": 1, "currency": "CHF"})
		with patch(f"{PAYPAL}.make_post_request") as outbound, as_role("Guest"):
			with a_request(form={"cmd": f"{PAYPAL}.confirm_payment"}):
				with self.assertRaises(frappe.PermissionError):
					http_call(f"{PAYPAL}.confirm_payment", token=name)
		outbound.assert_not_called()

	def test_the_buyer_back_from_paypal_passes(self):
		name = self.integration_request(
			"PayPal", {"amount": 12, "currency": "CHF", "use_sandbox": 1, "payerid": "PAYER"}
		)
		answer = {"ACK": ["Success"], "PAYMENTINFO_0_TRANSACTIONID": ["T"], "CORRELATIONID": ["C"]}
		with (
			patch(f"{PAYPAL}.make_post_request", return_value=answer) as outbound,
			as_role("Guest"),
			a_request(form={"cmd": f"{PAYPAL}.confirm_payment"}),
		):
			http_call(f"{PAYPAL}.confirm_payment", token=name)
		outbound.assert_called_once()
		self.assertEqual(frappe.db.get_value("Integration Request", name, "status"), "Completed")
