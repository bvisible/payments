# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""What the phone relies on from :mod:`payments.api.tap_to_pay`.

Run::

    bench --site <site> run-tests --module payments.api.test_tap_to_pay

Needs the Tap to Pay binding the provisioning patch creates, and any document the
test user can read — a Payment Provider record is used as the reference, since it
exists on every site running this app. No money moves: this channel cannot start
a payment by construction, and every intent created here is deleted again.
"""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from payments.api import tap_to_pay

CHANNEL = "payrexx_tap_to_pay"


class TestTapToPayMobileApi(FrappeTestCase):
	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		cls.binding = tap_to_pay._binding()
		# Any document the caller can read serves as "what the money is for". The
		# provider record is guaranteed to exist wherever the binding does.
		cls.reference = ("Payment Provider", cls.binding.provider) if cls.binding else None
		cls.created: list[str] = []

	@classmethod
	def tearDownClass(cls) -> None:
		for name in cls.created:
			if frappe.db.exists("Payment Intent", name):
				frappe.delete_doc("Payment Intent", name, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDownClass()

	def _start(self, amount: int = 1250, **kwargs) -> dict:
		out = tap_to_pay.mobile_start_payment(
			amount=amount,
			reference_doctype=self.reference[0],
			reference_name=self.reference[1],
			**kwargs,
		)
		self.created.append(out["intent_name"])
		return out

	def test_context_names_the_site_currency_and_the_binding(self) -> None:
		ctx = tap_to_pay.mobile_context()
		self.assertEqual(ctx["channel"], CHANNEL)
		self.assertEqual(ctx["enabled"], bool(self.binding))
		self.assertRegex(ctx["currency"], r"^[A-Z]{3}$")
		self.assertIsInstance(ctx["simulators_enabled"], bool)

	def test_start_returns_the_handoff_the_phone_hands_over(self) -> None:
		if not self.binding:
			self.skipTest("Tap to Pay is not set up on this site")
		out = self._start(amount=1250, payment_method="card")
		self.assertEqual(out["status"], "requires_action")
		self.assertEqual(out["next_action_type"], "native_app_handoff")
		payload = out["next_action_payload"]
		# The one field that ties the Payrexx transaction back to this record.
		self.assertEqual(payload["order_reference"], out["intent_name"])
		self.assertEqual(payload["amount"], 1250)
		self.assertEqual(payload["payment_method"], "CARD")
		self.assertEqual(out["currency"], tap_to_pay._currency())

	def test_start_refuses_a_zero_amount_and_a_missing_document(self) -> None:
		if not self.binding:
			self.skipTest("Tap to Pay is not set up on this site")
		with self.assertRaises(frappe.ValidationError):
			tap_to_pay.mobile_start_payment(
				amount=0, reference_doctype=self.reference[0], reference_name=self.reference[1]
			)
		with self.assertRaises(frappe.ValidationError):
			tap_to_pay.mobile_start_payment(
				amount=100, reference_doctype="Payment Provider", reference_name="does-not-exist"
			)

	def test_payments_for_lists_open_intents_newest_first(self) -> None:
		if not self.binding:
			self.skipTest("Tap to Pay is not set up on this site")
		first = self._start(amount=100)
		second = self._start(amount=200)
		rows = tap_to_pay.mobile_payments_for(*self.reference)
		names = [r["intent_name"] for r in rows]
		self.assertLess(names.index(second["intent_name"]), names.index(first["intent_name"]))
		row = next(r for r in rows if r["intent_name"] == second["intent_name"])
		self.assertTrue(row["open"], "an untapped intent is still open")
		self.assertEqual(row["amount"], 200)

	def test_abandon_closes_an_untapped_intent_as_canceled(self) -> None:
		if not self.binding:
			self.skipTest("Tap to Pay is not set up on this site")
		out = self._start(amount=300)
		closed = tap_to_pay.mobile_abandon_payment(out["intent_name"])
		self.assertEqual(closed["status"], "canceled")
		row = next(
			r
			for r in tap_to_pay.mobile_payments_for(*self.reference)
			if r["intent_name"] == out["intent_name"]
		)
		self.assertFalse(row["open"])

	def test_simulate_is_refused_without_the_site_flag(self) -> None:
		if not self.binding:
			self.skipTest("Tap to Pay is not set up on this site")
		out = self._start(amount=400)
		flag = frappe.conf.get("enable_e2e_simulators")
		frappe.conf["enable_e2e_simulators"] = False
		try:
			with self.assertRaises(frappe.PermissionError):
				tap_to_pay.simulate_success(out["intent_name"])
		finally:
			frappe.conf["enable_e2e_simulators"] = flag

	def test_simulate_finishes_the_intent_where_the_flag_allows_it(self) -> None:
		if not self.binding:
			self.skipTest("Tap to Pay is not set up on this site")
		if not frappe.conf.get("enable_e2e_simulators"):
			self.skipTest("enable_e2e_simulators is off on this site")
		out = self._start(amount=500)
		done = tap_to_pay.simulate_success(out["intent_name"])
		self.assertEqual(done["status"], "succeeded")
		self.assertTrue(done["provider_intent_id"], "a simulated payment still names a transaction")
		row = next(
			r
			for r in tap_to_pay.mobile_payments_for(*self.reference)
			if r["intent_name"] == out["intent_name"]
		)
		self.assertFalse(row["open"])
