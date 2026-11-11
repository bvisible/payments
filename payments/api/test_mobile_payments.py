# //// Neoffice — added file (no upstream equivalent). What the phone relies on from
# //// `payments.api.mobile_payments` — the "collect on site" surface where Stripe Tap to
# //// Pay and TWINT QR settle in one place. Upstream has neither surface. The suite sets up
# //// its own collection (maintenance#629: it skipped 16 of its 18 tests on any site without
# //// Stripe keys and a TWINT certificate, the CI's included, and still read green).
# //// Commits: d06eb26 2026-09-03 "feat(mobile): encaisser sur place par Stripe Tap to Pay et par QR TWINT, réglés en un seul endroit"
# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""What the phone relies on from :mod:`payments.api.mobile_payments`.

Run::

    bench --site <site> run-tests --module payments.api.test_mobile_payments

The suite sets up the collection it tests, on any site, the CI's fresh one included: a Stripe and a TWINT Payment
Provider of its own, Mobile Payment Settings switched on for both (its save writes the bindings, the real drivers'
own: StripeTapToPayDriver and TwintMobileDriver), and ``enable_e2e_simulators`` in this process only, never in a
site's configuration. The drivers run as they do on a site; only the two calls that would leave the machine are
answered here, in memory: the Stripe SDK's PaymentIntent and ConnectionToken calls (``_FakeStripe``) and the
instance's TWINT engine (``_fake_twint_engine``). What the PSPs themselves answer is the drivers' own tests'
(drivers/stripe/test_terminal_driver.py, drivers/twint/test_php_bridge_driver.py). Nothing is committed: the class
turns a commit into a savepoint and a rollback into a return to it (what a commit kept survives a rollback, as on a site), and
Frappe's test case rolls everything back at the end. No money moves, no mail leaves.

A Payment Provider record serves as "what the money is for": it exists wherever the
settings do, and the caller can read it.
"""

from __future__ import annotations

import json
import secrets
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from payments.api import mobile_payments as mp

STRIPE_PROVIDER = "_test_mobile_stripe"
TWINT_PROVIDER = "_test_mobile_twint"
LOCATION = "tml_test_mobile_payments"
MERCHANT_UUID = "00000000-0000-4000-8000-000000000629"
SCENERY = "mobile_payments_scenery"


class _PaymentIntent(dict):
	"""A Stripe PaymentIntent as the SDK hands it back: attributes, ``get`` and ``to_dict``."""

	def __getattr__(self, key):  # noqa: ANN001, ANN204
		return self.get(key)

	def to_dict(self) -> dict:
		return dict(self)


class _FakeStripe:
	"""Stripe's side of the calls the Tap to Pay driver and ``connection_token`` make, kept in memory."""

	def __init__(self) -> None:
		self.intents: dict[str, _PaymentIntent] = {}

	def create(
		self, api_key=None, amount=None, currency=None, metadata=None, **_kwargs
	):  # noqa: ANN001, ANN201
		pid = f"pi_test_{secrets.token_hex(8)}"
		self.intents[pid] = _PaymentIntent(
			id=pid,
			object="payment_intent",
			amount=amount,
			currency=currency,
			status="requires_payment_method",
			client_secret=f"{pid}_secret_{secrets.token_hex(6)}",
			metadata=dict(metadata or {}),
		)
		return self.intents[pid]

	def retrieve(self, pid, api_key=None, **_kwargs):  # noqa: ANN001, ANN201
		return self.intents[pid]

	def cancel(self, pid, api_key=None, **_kwargs):  # noqa: ANN001, ANN201
		self.intents[pid]["status"] = "canceled"
		return self.intents[pid]

	def connection_token(self, api_key=None, location=None, **_kwargs):  # noqa: ANN001, ANN201
		return SimpleNamespace(secret=f"pst_test_{secrets.token_hex(8)}", location=location)


def _fake_twint_engine(_driver, command, merchant_uuid, params):  # noqa: ANN001, ANN202
	"""The instance's TWINT engine (payments.twint_engine) as it answers a merchant with a certificate."""
	if command == "register_payment":
		return {
			"success": True,
			"order_id": f"twint-test-{secrets.token_hex(6)}",
			"pairing_token": "629629",
		}
	if command in ("cancel_payment", "monitor_order"):
		return {"success": True, "order_id": params.get("order_id"), "status": "canceled"}
	return {"success": False, "error": f"the test engine knows no {command}"}


def _set_up_collection_on_site() -> None:
	"""A Stripe and a TWINT provider of the suite's own, and on-site collection switched on for both."""
	for name, driver_class, credentials in (
		(
			STRIPE_PROVIDER,
			"payments.drivers.stripe.web_driver.StripeWebDriver",
			{"secret_key": "sk_test_mobile_payments_suite"},
		),
		(
			TWINT_PROVIDER,
			"payments.drivers.twint.web_driver.TwintWebDriver",
			# The engine is never reached (_fake_twint_engine answers): the provider still names where it would be
			{"default_merchant_uuid": MERCHANT_UUID, "service_url": "https://twint-engine.invalid"},
		),
	):
		if not frappe.db.exists("Payment Provider", name):
			frappe.get_doc(
				{
					"doctype": "Payment Provider",
					"provider_name": name,
					"display_label": name,
					"enabled": 1,
					"mode": "test",
					"driver_class": driver_class,
					"credentials_json": json.dumps(credentials),
				}
			).insert(ignore_permissions=True)
	settings = frappe.get_doc("Mobile Payment Settings")
	settings.update(
		{
			"enable_tap_to_pay": 1,
			"tap_to_pay_provider": STRIPE_PROVIDER,
			"stripe_location": LOCATION,
			"enable_twint": 1,
			"twint_provider": TWINT_PROVIDER,
		}
	)
	settings.flags.ignore_permissions = True
	settings.save()


class TestMobilePayments(FrappeTestCase):
	@classmethod
	def setUpClass(cls) -> None:
		# Registered first, so it runs last, after the rollback: the cached settings are the site's again
		cls.addClassCleanup(
			frappe.clear_document_cache, "Mobile Payment Settings", "Mobile Payment Settings"
		)
		super().setUpClass()
		cls.stripe = _FakeStripe()
		import stripe

		from payments.drivers.twint.php_bridge_driver import TwintPHPBridgeDriver

		for patcher in (
			# Nothing of the suite stays: Frappe's test case rolls the class back. A commit of the code under test (the webhook's, the
			# simulator's) only moves the point a rollback goes back to, as a commit does: what came before it survives a rollback
			patch.object(frappe.db, "commit", lambda *_args, **_kwargs: frappe.db.savepoint(SCENERY)),
			# The simulators, in this process only
			patch.dict(frappe.conf, {"enable_e2e_simulators": 1}),
			patch.object(stripe.PaymentIntent, "create", cls.stripe.create),
			patch.object(stripe.PaymentIntent, "retrieve", cls.stripe.retrieve),
			patch.object(stripe.PaymentIntent, "cancel", cls.stripe.cancel),
			patch.object(stripe.terminal.ConnectionToken, "create", cls.stripe.connection_token),
			patch.object(TwintPHPBridgeDriver, "_call_bridge", _fake_twint_engine),
			# A receipt is mailed to nobody
			patch.object(frappe, "sendmail"),
		):
			patcher.start()
			cls.addClassCleanup(patcher.stop)
		_set_up_collection_on_site()
		# A rollback of the code under test (the poll that lost to the webhook rolls its stale copy back) goes back to the last commit, or
		# to the collection just set up, as it would on a site where those are committed: the scenery stays for the next tests
		frappe.db.savepoint(SCENERY)
		real_rollback = frappe.db.rollback

		def rollback_to_the_scenery(*args, save_point=None, **kwargs):  # noqa: ANN001, ANN202
			return real_rollback(*args, save_point=save_point or SCENERY, **kwargs)

		rollback = patch.object(frappe.db, "rollback", rollback_to_the_scenery)
		rollback.start()
		cls.addClassCleanup(rollback.stop)
		cls.ctx = mp.mobile_context()
		# Both methods are on offer, or nothing below measures anything: a suite that skips is not green
		assert cls.ctx["methods"] == ["card", "twint"], cls.ctx
		provider = cls.ctx["card"]["provider"] or cls.ctx["twint"]["provider"]
		cls.reference = ("Payment Provider", provider) if provider else None
		cls.created: list[str] = []

	def _start(self, method: str, amount: int = 150) -> dict:
		out = mp.mobile_start_payment(
			amount=amount,
			reference_doctype=self.reference[0],
			reference_name=self.reference[1],
			method=method,
		)
		self.created.append(out["intent_name"])
		return out

	def _need(self, method: str) -> None:
		if not self.reference:
			self.skipTest("no on-site payment method is set up on this site")
		if method not in self.ctx["methods"]:
			self.skipTest(f"{method} is not enabled in Mobile Payment Settings")

	# ------------------------------------------------------------------ context

	def test_context_lists_the_enabled_methods_and_the_site_currency(self) -> None:
		ctx = mp.mobile_context()
		self.assertRegex(ctx["currency"], r"^[A-Z]{3}$")
		self.assertIsInstance(ctx["simulators_enabled"], bool)
		for key in ("card", "twint"):
			self.assertIn("enabled", ctx[key])
			self.assertIn("provider", ctx[key])
		self.assertEqual(
			ctx["methods"],
			[m for m in ("card", "twint") if ctx[m]["enabled"]],
			"methods must mirror the two enabled flags, in that order",
		)
		if ctx["card"]["enabled"]:
			self.assertTrue(str(ctx["card"]["location_id"]).startswith("tml_"))

	def test_unknown_method_is_refused(self) -> None:
		if not self.reference:
			self.skipTest("no on-site payment method is set up on this site")
		with self.assertRaises(frappe.ValidationError):
			mp.mobile_start_payment(
				amount=100,
				reference_doctype=self.reference[0],
				reference_name=self.reference[1],
				method="cash",
			)

	# --------------------------------------------------------------------- card

	def test_card_start_hands_the_phone_a_client_secret(self) -> None:
		self._need("card")
		out = self._start("card")
		self.assertEqual(out["status"], "requires_action")
		self.assertEqual(out["next_action_type"], "native_app_handoff")
		self.assertTrue(str(out["provider_intent_id"]).startswith("pi_"))
		self.assertTrue(str(out["client_secret"]).startswith("pi_"), "the SDK confirms with this")
		payload = out["next_action_payload"]
		self.assertEqual(payload["handoff"], "stripe_terminal")
		self.assertEqual(payload["location_id"], self.ctx["card"]["location_id"])
		# Abandon closes it at Stripe too — an unconfirmed PaymentIntent cancels cleanly.
		closed = mp.mobile_abandon_payment(out["intent_name"])
		self.assertEqual(closed["status"], "canceled")

	def test_connection_token_is_scoped_to_the_location(self) -> None:
		self._need("card")
		token = mp.connection_token()
		self.assertTrue(str(token["secret"]).startswith("pst_"))
		self.assertEqual(token["location_id"], self.ctx["card"]["location_id"])

	def test_refresh_status_reads_stripe_for_an_open_card_intent(self) -> None:
		self._need("card")
		out = self._start("card", amount=600)
		# Untouched at Stripe: still waiting on the phone, and the read must say so
		# without inventing a settlement.
		fresh = mp.mobile_refresh_status(out["intent_name"])
		self.assertEqual(fresh["status"], "requires_action")
		# Cancel at Stripe behind the server's back, then refresh: the read moves
		# the intent to canceled, which is exactly the case a late webhook leaves.
		closed = mp.mobile_abandon_payment(out["intent_name"])
		self.assertEqual(closed["status"], "canceled")
		again = mp.mobile_refresh_status(out["intent_name"])
		self.assertEqual(again["status"], "canceled")

	def test_refresh_status_yields_when_the_webhook_settles_first(self) -> None:
		"""The poll reads Stripe while the webhook writes the same settlement.

		Its copy of the intent is then stale, and saving it would raise a timestamp
		mismatch straight to the phone. The poll must report the webhook's result
		instead, and the intent must carry exactly one settlement event.
		"""
		self._need("card")
		out = self._start("card", amount=700)
		name = out["intent_name"]

		class _WebhookWins:
			def get_status(self, provider_intent_id):
				# What the webhook does, in the middle of the poll's read.
				other = frappe.get_doc("Payment Intent", name)
				other.transition_to("succeeded", event_source="webhook", payload_excerpt="test: webhook first")
				frappe.db.commit()
				from payments.drivers.base import DriverResponse

				return DriverResponse(status="succeeded", provider_intent_id=provider_intent_id)

		with patch.object(mp, "resolve_driver", return_value=_WebhookWins()):
			fresh = mp.mobile_refresh_status(name)
		self.assertEqual(fresh["status"], "succeeded")
		events = frappe.get_all(
			"Payment Event", filters={"intent": name, "to_status": "succeeded"}, pluck="event_source"
		)
		self.assertEqual(events, ["webhook"], "the poll must not add a second settlement")
		# Stripe still holds an unconfirmed PaymentIntent for it: close it there.
		from payments.drivers.registry import resolve_driver

		resolve_driver(self.ctx["card"]["provider"], mp.CARD_CHANNEL).cancel_intent(
			out["provider_intent_id"]
		)

	# -------------------------------------------------------------------- twint

	def test_twint_start_hands_the_phone_a_qr(self) -> None:
		self._need("twint")
		out = self._start("twint")
		self.assertEqual(out["status"], "requires_action")
		self.assertEqual(out["next_action_type"], "display_qr_payload")
		payload = out["next_action_payload"]
		self.assertTrue(
			payload.get("pairing_token"), "the customer can type the token instead of scanning"
		)
		self.assertIn("<svg", payload.get("qr_svg", ""), "the QR is drawn by the phone")
		closed = mp.mobile_abandon_payment(out["intent_name"])
		self.assertIn(closed["status"], ("canceled", "requires_action"))

	# ---------------------------------------------------------- standalone

	def test_standalone_start_needs_no_document_and_keeps_its_label(self) -> None:
		"""The Collect tool: an amount, a method, a label — no document behind it."""
		if not self.reference:
			self.skipTest("no on-site payment method is set up on this site")
		method = self.ctx["methods"][0]
		out = mp.mobile_start_payment(amount=350, method=method, label="  Dépannage chaudière  ")
		self.created.append(out["intent_name"])
		self.assertEqual(out["status"], "requires_action")
		doc = frappe.get_doc("Payment Intent", out["intent_name"])
		self.assertFalse(doc.reference_doctype)
		self.assertFalse(doc.reference_name)
		rows = mp.mobile_recent_payments()
		row = next(r for r in rows if r["intent_name"] == out["intent_name"])
		self.assertEqual(row["label"], "Dépannage chaudière", "trimmed, and kept")
		self.assertTrue(row["open"])
		self.assertEqual(row["method"], method)
		mp.mobile_abandon_payment(out["intent_name"])

	def test_standalone_start_is_refused_to_a_portal_account(self) -> None:
		"""No document means no permission to lean on: staff only."""
		if not self.reference:
			self.skipTest("no on-site payment method is set up on this site")
		with patch.object(mp, "_is_staff", return_value=False):
			with self.assertRaises(frappe.PermissionError):
				mp.mobile_start_payment(amount=350, method=self.ctx["methods"][0])
			with self.assertRaises(frappe.PermissionError):
				mp.mobile_recent_payments()

	def test_recent_payments_leave_out_the_ones_taken_for_a_document(self) -> None:
		if not self.reference:
			self.skipTest("no on-site payment method is set up on this site")
		against = self._start(self.ctx["methods"][0], amount=360)
		names = [r["intent_name"] for r in mp.mobile_recent_payments()]
		self.assertNotIn(against["intent_name"], names)
		mp.mobile_abandon_payment(against["intent_name"])

	# ------------------------------------------------------------- receipts

	def test_context_says_who_may_accept_apple_terms_and_the_merchant_name(self) -> None:
		ctx = mp.mobile_context()
		self.assertIsInstance(ctx["can_accept_terms"], bool)
		self.assertTrue(ctx["can_accept_terms"], "the test runner is Administrator")
		self.assertTrue(ctx["merchant_display_name"])

	def test_receipt_is_refused_while_the_payment_is_open(self) -> None:
		if not self.reference:
			self.skipTest("no on-site payment method is set up on this site")
		out = self._start(self.ctx["methods"][0], amount=1250)
		with self.assertRaises(frappe.ValidationError):
			mp.send_receipt(out["intent_name"], "client@example.com")
		mp.mobile_abandon_payment(out["intent_name"])

	def test_receipt_carries_the_payment_and_remembers_the_address(self) -> None:
		if not self.reference:
			self.skipTest("no on-site payment method is set up on this site")
		if not frappe.conf.get("enable_e2e_simulators"):
			self.skipTest("enable_e2e_simulators is off on this site")
		out = mp.mobile_start_payment(
			amount=1250, method=self.ctx["methods"][0], label="Joint de robinet"
		)
		self.created.append(out["intent_name"])
		mp.simulate_success(out["intent_name"])
		with self.assertRaises(frappe.ValidationError):
			mp.send_receipt(out["intent_name"], "not-an-address")
		sent = mp.send_receipt(out["intent_name"], "client@example.com")
		self.assertTrue(sent["sent"])
		receipt = sent["receipt"]
		self.assertEqual(receipt["amount_display"], f"{self.ctx['currency']} 12.50")
		self.assertTrue(receipt["approved"])
		self.assertEqual(receipt["label"], "Joint de robinet")
		self.assertTrue(receipt["merchant"])
		meta = frappe.parse_json(
			frappe.db.get_value("Payment Intent", out["intent_name"], "metadata_json")
		)
		self.assertEqual(meta["receipt_sent_to"], "client@example.com")
		# The screen reads the same thing back.
		again = mp.mobile_receipt(out["intent_name"])
		self.assertEqual(again["intent_name"], out["intent_name"])

	def test_a_declined_mobile_payment_pushes_the_phone(self) -> None:
		if not self.reference:
			self.skipTest("no on-site payment method is set up on this site")
		out = self._start(self.ctx["methods"][0], amount=1300)
		with patch.object(mp, "_push_to_owner") as push:
			doc = frappe.get_doc("Payment Intent", out["intent_name"])
			doc.transition_to(
				"failed", event_source="system", error_code="card_declined", error_message="Refusée"
			)
			push.assert_called_once()
			self.assertEqual(push.call_args.args[0].name, out["intent_name"])

	# //// Neoffice — covers `paid_total_for`/`mobile_paid_total`: open intents must not be counted in the collected total (137488f "feat(mobile): ce qu'un document a déjà encaissé sur place — paid_total_for")
	def test_paid_total_counts_only_settled_intents(self) -> None:
		if not self.reference:
			self.skipTest("no on-site payment method is set up on this site")
		if not frappe.conf.get("enable_e2e_simulators"):
			self.skipTest("enable_e2e_simulators is off on this site")
		before = mp.paid_total_for(*self.reference)
		open_one = self._start(self.ctx["methods"][0], amount=900)
		paid_one = self._start(self.ctx["methods"][0], amount=1100)
		mp.simulate_success(paid_one["intent_name"])
		after = mp.paid_total_for(*self.reference)
		self.assertEqual(after["paid_total"], before["paid_total"] + 1100, "the open one is not counted")
		self.assertEqual(after["count"], before["count"] + 1)
		self.assertGreaterEqual(after["open"], 1)
		self.assertTrue(after["last_paid_at"])
		# The phone reads the same thing, behind the document's read permission.
		self.assertEqual(mp.mobile_paid_total(*self.reference)["paid_total"], after["paid_total"])
		mp.mobile_abandon_payment(open_one["intent_name"])

	# --------------------------------------------------------------- common

	def test_payments_for_lists_open_intents_newest_first_with_their_method(self) -> None:
		if not self.reference:
			self.skipTest("no on-site payment method is set up on this site")
		method = self.ctx["methods"][0]
		first = self._start(method, amount=100)
		second = self._start(method, amount=200)
		rows = mp.mobile_payments_for(*self.reference)
		names = [r["intent_name"] for r in rows]
		self.assertLess(names.index(second["intent_name"]), names.index(first["intent_name"]))
		row = next(r for r in rows if r["intent_name"] == second["intent_name"])
		self.assertEqual(row["method"], method)
		self.assertTrue(row["open"])
		for name in (first["intent_name"], second["intent_name"]):
			mp.mobile_abandon_payment(name)

	def test_simulate_is_refused_without_the_site_flag(self) -> None:
		if not self.reference:
			self.skipTest("no on-site payment method is set up on this site")
		out = self._start(self.ctx["methods"][0], amount=400)
		flag = frappe.conf.get("enable_e2e_simulators")
		frappe.conf["enable_e2e_simulators"] = False
		try:
			with self.assertRaises(frappe.PermissionError):
				mp.simulate_success(out["intent_name"])
		finally:
			frappe.conf["enable_e2e_simulators"] = flag
		mp.mobile_abandon_payment(out["intent_name"])

	def test_simulate_finishes_the_intent_where_the_flag_allows_it(self) -> None:
		if not self.reference:
			self.skipTest("no on-site payment method is set up on this site")
		if not frappe.conf.get("enable_e2e_simulators"):
			self.skipTest("enable_e2e_simulators is off on this site")
		out = self._start(self.ctx["methods"][0], amount=500)
		done = mp.simulate_success(out["intent_name"])
		self.assertEqual(done["status"], "succeeded")
		self.assertTrue(done["provider_intent_id"])
		row = next(
			r for r in mp.mobile_payments_for(*self.reference) if r["intent_name"] == out["intent_name"]
		)
		self.assertFalse(row["open"])
