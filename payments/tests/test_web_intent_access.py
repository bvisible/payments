# //// Neoffice — added file (no upstream equivalent). maintenance#1731: the guest entry points of the web
# //// checkouts (TWINT dialog, Payrexx state read and return page) read and cancelled ANY Payment Intent a
# //// guest named, till and terminal ones included. Only the browser holding the intent's token may now.
# Copyright (c) 2026, Neoffice and Contributors
# License: MIT. See LICENSE
"""A guest touches only the web intent it created; the buyer still reads and cancels its own."""

from __future__ import annotations

from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import frappe
from frappe.tests.utils import FrappeTestCase

from payments.api.intent_access import web_access_token
from payments.integrations.payrexx import api as payrexx_web
from payments.integrations.twint import api as twint_web
from payments.tests.test_intent_api import _ensure_mock_setup

_CHANNELS = ("twint_web", "payrexx_web", "terminal")
# A signed-in account the desk gives no right on Payment Intent, as a shop customer's.
_PORTAL_USER = "_test-1731-portal@example.com"


def _ensure_channels() -> None:
	_ensure_mock_setup()
	for code in _CHANNELS:
		if not frappe.db.exists("Payment Channel", code):
			frappe.get_doc(
				{
					"doctype": "Payment Channel",
					"channel_code": code,
					"display_label": code,
					"ui_kind": "redirect",
				}
			).insert(ignore_permissions=True)
		if not frappe.db.exists("Provider Channel Settings", {"provider": "mock", "channel": code}):
			frappe.get_doc(
				{"doctype": "Provider Channel Settings", "provider": "mock", "channel": code, "enabled": 1}
			).insert(ignore_permissions=True)


def _intent(channel: str) -> str:
	doc = frappe.get_doc(
		{
			"doctype": "Payment Intent",
			"status": "requires_action",
			"provider": "mock",
			"channel": channel,
			"amount": 1731,
			"currency": "CHF",
			"provider_intent_id": f"mock_pi_1731_{frappe.generate_hash(length=12)}",  # unique key
			"error_message": "kept for the test",
		}
	).insert(ignore_permissions=True)
	return doc.name


class TestAGuestTouchesOnlyTheWebIntentItCreated(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_channels()

	def setUp(self):
		self.sendmail = patch("frappe.sendmail")
		self.sendmail.start()
		self.mine = _intent("twint_web")
		self.someone_elses = _intent("twint_web")
		self.till = _intent("terminal")
		self.payrexx = _intent("payrexx_web")
		frappe.set_user("Guest")

	def tearDown(self):
		frappe.set_user("Administrator")
		self.sendmail.stop()
		for name in (self.mine, self.someone_elses, self.till, self.payrexx):
			frappe.db.delete("Payment Event", {"intent": name})
			frappe.db.delete("Payment Intent", {"name": name})

	def _status(self, name: str) -> str:
		return frappe.db.get_value("Payment Intent", name, "status")

	def test_without_its_token_a_guest_can_neither_read_nor_cancel_an_intent(self):
		for token in (None, "", "0" * 40, web_access_token(self.someone_elses)):
			with self.subTest(token=token):
				with self.assertRaises(frappe.PermissionError):
					twint_web.get_intent_state(self.mine, access_token=token)
				with self.assertRaises(frappe.PermissionError):
					twint_web.cancel_web_transaction(self.mine, access_token=token)
				self.assertEqual(self._status(self.mine), "requires_action")

	def test_a_token_opens_no_other_channel(self):
		# A till payment in flight was the worst case: cancelled by a guest who named it.
		token = web_access_token(self.till)
		with self.assertRaises(frappe.PermissionError):
			twint_web.cancel_web_transaction(self.till, access_token=token)
		with self.assertRaises(frappe.PermissionError):
			twint_web.get_intent_state(self.till, access_token=token)
		with self.assertRaises(frappe.PermissionError):
			payrexx_web.get_intent_state(self.till, access_token=token)
		with self.assertRaises(frappe.PermissionError):
			payrexx_web.get_intent_state(self.mine, access_token=web_access_token(self.mine))
		self.assertEqual(self._status(self.till), "requires_action")

	def test_a_name_that_does_not_exist_gets_the_same_refusal(self):
		# Not a DoesNotExistError: the answer must not tell which names are real.
		for token in (None, web_access_token("PI-NOPE-1731")):
			with self.subTest(token=token):
				with self.assertRaises(frappe.PermissionError):
					twint_web.get_intent_state("PI-NOPE-1731", access_token=token)
				with self.assertRaises(frappe.PermissionError):
					twint_web.cancel_web_transaction("PI-NOPE-1731", access_token=token)

	def test_the_buyer_with_its_token_reads_and_cancels_its_own_intent(self):
		state = twint_web.get_intent_state(self.mine, access_token=web_access_token(self.mine))
		self.assertEqual(state["intent_name"], self.mine)
		self.assertEqual(state["status"], "requires_action")

		result = twint_web.cancel_web_transaction(self.mine, access_token=web_access_token(self.mine))
		self.assertTrue(result["success"], result)
		self.assertEqual(self._status(self.mine), "canceled")
		self.assertEqual(self._status(self.someone_elses), "requires_action")

		state = payrexx_web.get_intent_state(self.payrexx, access_token=web_access_token(self.payrexx))
		self.assertEqual(state["status"], "requires_action")

	def test_a_signed_in_customer_without_desk_rights_needs_the_token_too(self):
		frappe.set_user(_PORTAL_USER)
		try:
			with self.assertRaises(frappe.PermissionError):
				twint_web.get_intent_state(self.mine)
			with self.assertRaises(frappe.PermissionError):
				twint_web.cancel_web_transaction(self.mine)
			self.assertEqual(self._status(self.mine), "requires_action")
		finally:
			frappe.set_user("Guest")

	def test_a_desk_user_allowed_to_read_the_intent_needs_no_token(self):
		frappe.set_user("Administrator")
		self.assertEqual(twint_web.get_intent_state(self.mine)["status"], "requires_action")
		# Still the web channel only, even for the desk.
		with self.assertRaises(frappe.PermissionError):
			twint_web.cancel_web_transaction(self.till)
		self.assertEqual(self._status(self.till), "requires_action")


class TestTheWebPivotsHandTheTokenToTheBrowser(FrappeTestCase):
	"""What create_web_transaction returns must be the token the guarded calls accept."""

	def _pivot(self, module, resolver: str, extra_patches: dict):
		created = {
			"intent_name": "PI-PIVOT-1731",
			"status": "requires_action",
			"next_action_payload": {"url": "x"},
		}
		request = MagicMock(
			payment_request_type="Inward", grand_total=17.31, currency="CHF", subject="t"
		)
		patches = [
			patch("payments.api.intent.create_intent", return_value=created),
			patch.object(module.frappe, "get_doc", return_value=request),
			patch.object(module, resolver, return_value="mock"),
		] + [patch.object(module, k, return_value=v) for k, v in extra_patches.items()]
		for p in patches:
			p.start()
		try:
			return module.create_web_transaction("PR-PIVOT-1731")
		finally:
			for p in reversed(patches):
				p.stop()

	def test_twint_returns_the_intents_token(self):
		result = self._pivot(
			twint_web, "_resolve_twint_provider_name", {"_resolve_merchant_uuid_for_provider": "merchant"}
		)
		self.assertEqual(result["access_token"], web_access_token("PI-PIVOT-1731"))

	def test_payrexx_returns_the_intents_token(self):
		result = self._pivot(payrexx_web, "_resolve_provider_name", {})
		self.assertEqual(result["access_token"], web_access_token("PI-PIVOT-1731"))


class TestThePayrexxReturnPageServesOnlyTheBrowserSentBackToIt(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_channels()

	def setUp(self):
		self.payrexx = _intent("payrexx_web")
		self.till = _intent("terminal")
		frappe.set_user("Guest")

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.form_dict.clear()
		for name in (self.payrexx, self.till):
			frappe.db.delete("Payment Event", {"intent": name})
			frappe.db.delete("Payment Intent", {"name": name})

	def _render(self, intent_name: str, token: str | None):
		from payments.www.payrexx import success

		frappe.form_dict.clear()
		frappe.form_dict.update({"payment_intent": intent_name, "access_token": token})
		context = frappe._dict()
		with patch.object(success, "_refresh_status") as refresh:
			success.get_context(context)
		return context, refresh

	def test_a_guest_naming_an_intent_learns_nothing_and_moves_nothing(self):
		for name, token in (
			(self.payrexx, None),
			(self.payrexx, "0" * 40),
			(self.till, web_access_token(self.till)),
			("PI-NOPE-1731", web_access_token("PI-NOPE-1731")),
		):
			with self.subTest(name=name, token=token):
				context, refresh = self._render(name, token)
				self.assertIsNone(context.intent)
				self.assertTrue(context.error)
				refresh.assert_not_called()
		self.assertEqual(frappe.db.get_value("Payment Intent", self.till, "status"), "requires_action")

	def test_the_buyer_sent_back_with_its_token_sees_its_payment_and_keeps_the_token_on_reload(self):
		token = web_access_token(self.payrexx)
		context, refresh = self._render(self.payrexx, token)
		self.assertEqual(context.intent.name, self.payrexx)
		refresh.assert_called_once()
		self.assertEqual(context.status, "pending")
		query = parse_qs(urlparse(context.retry_url).query)
		self.assertEqual(query["access_token"], [token])
		self.assertEqual(query["payment_intent"], [self.payrexx])
