# //// Neoffice — added file (no upstream equivalent). What the phone relies on from
# //// `payments.api.mobile_payments` — the "collect on site" surface where Stripe Tap to
# //// Pay and TWINT QR settle in one place. Talks to Stripe in test mode and registers
# //// then cancels a payment on the TWINT bridge; a method switched off is skipped, not
# //// failed, because a site may offer only one. Upstream has neither surface.
# //// Commits: d06eb26 2026-09-03 "feat(mobile): encaisser sur place par Stripe Tap to Pay et par QR TWINT, réglés en un seul endroit"
# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""What the phone relies on from :mod:`payments.api.mobile_payments`.

Run::

    bench --site <site> run-tests --module payments.api.test_mobile_payments

Needs Mobile Payment Settings switched on for the method under test; a method that
is off is skipped, not failed, because a site is allowed to offer only one. The card
tests talk to Stripe in test mode (a PaymentIntent is created and cancelled again);
the TWINT tests register and cancel a payment on the bridge. No money moves.

A Payment Provider record serves as "what the money is for": it exists wherever the
settings do, and the caller can read it.
"""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from payments.api import mobile_payments as mp


class TestMobilePayments(FrappeTestCase):
	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		cls.ctx = mp.mobile_context()
		provider = cls.ctx["card"]["provider"] or cls.ctx["twint"]["provider"]
		cls.reference = ("Payment Provider", provider) if provider else None
		cls.created: list[str] = []

	@classmethod
	def tearDownClass(cls) -> None:
		for name in cls.created:
			if frappe.db.exists("Payment Intent", name):
				frappe.delete_doc("Payment Intent", name, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDownClass()

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
				other.transition_to(
					"succeeded", event_source="webhook", payload_excerpt="test: webhook first"
				)
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

		resolve_driver(self.ctx["card"]["provider"], mp.CARD_CHANNEL).cancel_intent(out["provider_intent_id"])

	# -------------------------------------------------------------------- twint

	def test_twint_start_hands_the_phone_a_qr(self) -> None:
		self._need("twint")
		out = self._start("twint")
		self.assertEqual(out["status"], "requires_action")
		self.assertEqual(out["next_action_type"], "display_qr_payload")
		payload = out["next_action_payload"]
		self.assertTrue(payload.get("pairing_token"), "the customer can type the token instead of scanning")
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
		out = mp.mobile_start_payment(amount=1250, method=self.ctx["methods"][0], label="Joint de robinet")
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
		meta = frappe.parse_json(frappe.db.get_value("Payment Intent", out["intent_name"], "metadata_json"))
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
			doc.transition_to("failed", event_source="system", error_code="card_declined", error_message="Refusée")
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
		row = next(r for r in mp.mobile_payments_for(*self.reference) if r["intent_name"] == out["intent_name"])
		self.assertFalse(row["open"])
