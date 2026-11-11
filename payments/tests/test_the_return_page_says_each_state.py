# //// Neoffice — added file (no upstream equivalent). The page a customer lands on when she comes back from a hosted checkout
# //// (Wallee, Payrexx) says each state in its own words: paid (with what it was for, and the confetti once), confirming, taking
# //// long, refused, left, not found, hold run out, refunded, error. Until then a bare « Payment Successful » with a reference.
# Copyright (c) 2026, Neoffice and Contributors
# License: MIT. See LICENSE
"""The return page of a hosted checkout: its views, its template, its calendar file and the three pages that feed it."""

from __future__ import annotations

from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import frappe
from frappe.tests.utils import FrappeTestCase

from payments.api import payment_return as calendar_api
from payments.api.intent_access import web_access_token
from payments.tests.test_intent_api import _ensure_mock_setup
from payments.tests.test_web_intent_access import _ensure_channels
from payments.utils import payment_return as pr

TEMPLATE = "templates/includes/payment_return.html"

SUMMARY = {
	"title": "Full colour",
	"when": "Wednesday 18 November · 10:15 – 12:00",
	"steps": [
		{"time": "10:15 – 10:45", "label": "Application", "person": "Anna"},
		{"time": "10:45 – 11:15", "pause": "Break, back at 11:15"},
		{"time": "11:15 – 12:00", "label": "Rinse and cut", "person": "Marc"},
	],
	"paid": "CHF 28.50",
	"rest": "CHF 66.50",
	"email": "claire@example.com",
	"first_name": "Claire",
	"retry_url": "/book-online",
	"events": [
		{
			"uid": "BK-1",
			"start": "2026-11-18 10:15:00",
			"end": "2026-11-18 10:45:00",
			"title": "Full colour: Application",
			"description": "With Anna, thanks",
			"place": "Salon; Main street, Lausanne",
		}
	],
}


def an_intent(status="succeeded", amount=5610, error=None):
	return frappe._dict(
		name="PI-2026-00000001", status=status, amount=amount, currency="CHF", error_message=error
	)


def compose(intent, status=None, error=None, summary=None, **kwargs):
	"""The view the controllers' decisions turn into, with what the payment was for as given."""
	context = frappe._dict(intent=intent, status=status, error=error)
	with patch.object(pr, "summary_of", return_value=summary):
		pr.present(context, **kwargs)
	return context


def drawn(view) -> str:
	return frappe.render_template(TEMPLATE, {"view": view})


class TestTheViewOfEachState(FrappeTestCase):
	def test_each_state_the_controllers_leave_is_one_view(self):
		cases = [
			("not_found", compose(None), "mut"),
			("success", compose(an_intent(), "success"), "ok"),
			("pending", compose(an_intent("processing"), "pending"), "warn"),
			("unconfirmed", compose(an_intent("requires_action"), "unconfirmed"), "warn"),
			("failed", compose(an_intent("failed"), "failed", "Declined"), "bad"),
			("canceled", compose(an_intent("canceled"), "failed", "Declined"), "mut"),
			("refunded", compose(an_intent("refunded"), "success"), "ok"),
			("error", compose(an_intent(), "success", "Error creating order"), "bad"),
		]
		for kind, context, tone in cases:
			with self.subTest(kind=kind):
				self.assertEqual((context.view["kind"], context.view["tone"]), (kind, tone))
				self.assertEqual(context.title, context.view["title"])
				self.assertTrue(
					context.view["title"] and context.view["text"], "every state says what happened"
				)

	def test_the_page_opens_on_its_own_composition_with_no_title_band_or_breadcrumb_of_the_site(self):
		for context in (
			compose(None),
			compose(an_intent(), "success"),
			compose(an_intent("failed"), "failed"),
		):
			self.assertIs(context.show_page_header, False)
			self.assertEqual(context.no_breadcrumbs, 1)

	def test_only_a_payment_that_arrived_throws_confetti(self):
		for context in (
			compose(None),
			compose(an_intent("processing"), "pending"),
			compose(an_intent("failed"), "failed"),
			compose(an_intent("refunded"), "success"),
			compose(an_intent(), "success", "Error creating order"),
		):
			self.assertFalse(context.view["celebrate"], context.view["kind"])
		self.assertTrue(compose(an_intent(), "success").view["celebrate"])

	def test_the_amount_is_written_with_its_currency_where_the_site_hides_currency_symbols(self):
		self.assertEqual(pr.money(56.1, "CHF"), "CHF 56.10")
		self.assertEqual(compose(an_intent(amount=5610), "success").view["amount"], "CHF 56.10")
		self.assertEqual(pr.money(2, None), "CHF 2.00", "a round amount keeps its two decimals")
		self.assertEqual(pr.money(39, "CHF"), "CHF 39.00")

	def test_a_payment_with_no_summary_says_only_that_it_arrived(self):
		view = compose(an_intent(), "success").view
		self.assertEqual(view["title"], "Payment received")
		self.assertIn("CHF 56.10", view["text"])
		self.assertEqual(view["primary"]["url"], "/")

	def test_an_appointment_paid_says_so_and_offers_her_calendar(self):
		view = compose(an_intent(), "success", summary=SUMMARY).view
		self.assertEqual(view["title"], "Your appointment is confirmed")
		self.assertIn("Claire", view["text"])
		self.assertEqual(view["primary"]["label"], "Add to my calendar")
		query = parse_qs(urlparse(view["primary"]["url"]).query)
		self.assertEqual(query["payment_intent"], ["PI-2026-00000001"])
		self.assertEqual(view["secondary"]["url"], "/")

	def test_the_calendar_button_carries_the_token_the_page_was_opened_with(self):
		with patch.object(frappe, "form_dict", frappe._dict(access_token="abc123")):
			view = compose(an_intent(), "success", summary=SUMMARY).view
		self.assertEqual(parse_qs(urlparse(view["primary"]["url"]).query)["access_token"], ["abc123"])

	def test_an_appointment_that_is_gone_is_never_called_paid_whatever_the_state(self):
		gone = {"cancelled": True, "title": "Full colour", "retry_url": "/book-online"}
		for status, state in (
			("succeeded", "success"),
			("processing", "pending"),
			("failed", "failed"),
			("canceled", "failed"),
		):
			with self.subTest(status=status):
				view = compose(an_intent(status), state, summary=gone).view
				self.assertEqual(view["kind"], "released")
				self.assertEqual(view["title"], "This time is no longer held")
				self.assertEqual(view["primary"]["url"], "/book-online")
		refunded = compose(an_intent("refunded"), "success", summary=gone).view
		self.assertEqual(refunded["kind"], "released")
		self.assertIn("given back", refunded["note"])

	def test_the_hold_is_counted_only_while_there_is_something_to_pay(self):
		held = dict(SUMMARY, hold_seconds=761)
		for state, status in (("failed", "failed"), ("pending", "processing")):
			self.assertEqual(compose(an_intent(status), state, summary=held).view["hold_seconds"], 761)
		self.assertIsNone(compose(an_intent(), "success", summary=held).view["hold_seconds"])
		self.assertIsNone(compose(an_intent("refunded"), "success", summary=held).view["hold_seconds"])

	def test_a_gateways_words_are_shown_small_and_only_where_they_explain_a_failure(self):
		failed = compose(an_intent("failed"), "failed", "Card declined by the issuer").view
		self.assertEqual(failed["detail"], "Card declined by the issuer")
		self.assertIsNone(compose(an_intent(), "success").view["detail"])
		given = compose(an_intent("failed"), "failed", detail="Insufficient funds").view
		self.assertEqual(given["detail"], "Insufficient funds")

	def test_a_payment_not_found_says_what_to_do_and_does_not_say_nothing_was_paid(self):
		view = compose(None, error="Payment reference not found").view
		self.assertEqual(view["title"], "We cannot find this payment here")
		self.assertEqual(len(view["todo"]), 3)
		self.assertIn("does not mean nothing was paid", view["text"])
		self.assertIsNone(view["reference"])

	def test_the_waiting_page_tells_how_to_reload_and_the_slow_words_it_swaps_to(self):
		view = compose(an_intent("processing"), "pending", reload_url="/payrexx/success?try=3").view
		self.assertEqual(view["reload_url"], "/payrexx/success?try=3")
		for key in ("slow_title", "slow_text", "slow_note"):
			self.assertTrue(view[key])

	def test_a_payment_still_unconfirmed_can_be_checked_again_or_paid_again(self):
		view = compose(
			an_intent("requires_action"), "unconfirmed", check_url="/payrexx/success?try=0"
		).view
		self.assertEqual(view["primary"], {"label": "Check now", "url": "/payrexx/success?try=0"})
		self.assertEqual(view["secondary"]["label"], "Try paying again")
		self.assertIn("do not pay a second time", view["note"])


class TestWhatTheHooksSay(FrappeTestCase):
	def test_the_first_hook_that_answers_wins_and_a_hook_that_fails_never_breaks_the_page(self):
		answers = {
			"a.fails": RuntimeError("boom"),
			"b.silent": None,
			"c.says": {"title": "From C"},
			"d.late": {"title": "From D"},
		}

		def attr(method):
			def call(intent_name):
				answer = answers[method]
				if isinstance(answer, Exception):
					raise answer
				return answer

			return call

		with patch.object(frappe, "get_hooks", return_value=list(answers)), patch.object(
			frappe, "get_attr", side_effect=attr
		), patch.object(frappe, "log_error") as logged:
			self.assertEqual(pr.summary_of(an_intent()), {"title": "From C"})
		self.assertEqual(logged.call_count, 1, "the failure is logged, once")

	def test_no_intent_and_no_hook_mean_no_summary(self):
		self.assertIsNone(pr.summary_of(None))
		with patch.object(frappe, "get_hooks", return_value=[]):
			self.assertIsNone(pr.summary_of(an_intent()))


class TestTheTemplate(FrappeTestCase):
	def test_a_paid_appointment_shows_its_steps_its_pause_and_what_is_left_to_pay(self):
		page = drawn(compose(an_intent(), "success", summary=SUMMARY).view)
		for text in (
			"Your appointment is confirmed",
			"Full colour",
			"Wednesday 18 November",
			"10:15 – 10:45",
			"Application",
			"Anna",
			"Break, back at 11:15",
			"Rinse and cut",
			"Down payment paid",
			"CHF 28.50",
			"Left to pay at the salon",
			"CHF 66.50",
			"claire@example.com",
			"Add to my calendar",
			"Back to the site",
		):
			self.assertIn(text, page)
		self.assertIn('id="pr-confetti"', page)
		self.assertNotIn('data-reload="', page)

	def test_an_appointment_paid_in_full_says_paid_and_leaves_no_rest(self):
		page = drawn(compose(an_intent(), "success", summary=dict(SUMMARY, rest=None)).view)
		self.assertNotIn("Left to pay at the salon", page)
		self.assertNotIn("Down payment paid", page)
		self.assertIn("CHF 28.50", page)

	def test_money_is_told_only_once_the_payment_has_arrived(self):
		page = drawn(compose(an_intent("processing"), "pending", summary=SUMMARY).view)
		self.assertIn("Full colour", page)
		self.assertNotIn("Down payment paid", page)
		self.assertNotIn("CHF 66.50", page)
		self.assertNotIn('id="pr-confetti"', page, "no confetti while waiting")

	def test_the_waiting_page_reloads_itself_and_has_the_words_for_when_it_takes_long(self):
		page = drawn(
			compose(an_intent("processing"), "pending", reload_url="/payrexx/success?try=2").view
		)
		self.assertIn('data-reload="/payrexx/success?try=2"', page)
		self.assertIn("We are confirming your payment", page)
		self.assertIn("This is taking longer than expected", page)

	def test_a_waiting_page_with_no_address_reloads_the_same_one(self):
		self.assertIn('data-reload=""', drawn(compose(an_intent("processing"), "pending").view))

	def test_a_failed_payment_shows_the_count_down_of_the_hold_and_a_way_to_pay_again(self):
		page = drawn(
			compose(an_intent("failed"), "failed", "Declined", summary=dict(SUMMARY, hold_seconds=761)).view
		)
		self.assertIn('data-seconds="761"', page)
		self.assertIn("Try paying again", page)
		self.assertIn('href="/book-online"', page)
		self.assertIn("Declined", page)
		self.assertNotIn('id="pr-confetti"', page)

	def test_a_page_with_no_hold_has_no_count_down(self):
		self.assertNotIn('id="pr-hold"', drawn(compose(an_intent("failed"), "failed").view))

	def test_a_payment_not_found_lists_what_to_do_and_shows_no_reference(self):
		page = drawn(compose(None, error="Payment reference not found").view)
		self.assertIn("We cannot find this payment here", page)
		self.assertIn("Did you pay?", page)
		self.assertIn("Write to us", page)
		self.assertNotIn("PI-2026", page)

	def test_the_old_words_are_gone(self):
		for context in (
			compose(an_intent(), "success"),
			compose(an_intent("failed"), "failed"),
			compose(None),
		):
			page = drawn(context.view)
			for old in ("Return to Cart", "Payment Successful", "Payment Failed", "Contact Support"):
				self.assertNotIn(old, page)

	def test_what_a_customer_wrote_is_never_drawn_as_markup(self):
		hostile = dict(
			SUMMARY,
			first_name="<script>alert(1)</script>",
			email="<img src=x onerror=alert(2)>@example.com",
			title="<b>Colour</b>",
			steps=[{"time": "10:15", "label": "<i>Step</i>", "person": "<u>Anna</u>"}],
		)
		page = drawn(compose(an_intent(), "success", summary=hostile).view)
		for markup in ("<script>alert(1)", "<img src=x", "<b>Colour</b>", "<i>Step</i>", "<u>Anna</u>"):
			self.assertNotIn(markup, page)
		self.assertIn("&lt;script&gt;alert(1)", page)

	def test_the_confetti_is_skipped_for_a_visitor_who_asks_for_less_motion_and_never_replayed(self):
		page = drawn(compose(an_intent(), "success").view)
		self.assertIn("prefers-reduced-motion", page)
		self.assertIn("sessionStorage", page)
		self.assertIn("pr-confetti-", page)


class TestTheCalendarFile(FrappeTestCase):
	def test_an_event_is_written_in_the_salons_own_time_with_its_text_escaped(self):
		text = pr.calendar_file(SUMMARY)
		lines = text.split("\r\n")
		self.assertEqual(lines[0], "BEGIN:VCALENDAR")
		self.assertEqual(lines[-2:], ["END:VCALENDAR", ""])
		self.assertIn("DTSTART:20261118T101500", lines)
		self.assertIn("DTEND:20261118T104500", lines)
		self.assertIn("SUMMARY:Full colour: Application", lines)
		self.assertIn("DESCRIPTION:With Anna\\, thanks", lines)
		self.assertIn("LOCATION:Salon\\; Main street\\, Lausanne", lines)
		self.assertEqual(text.count("BEGIN:VEVENT"), 1)

	def test_a_chain_gives_one_event_a_step(self):
		two = dict(
			SUMMARY,
			events=[
				SUMMARY["events"][0],
				dict(SUMMARY["events"][0], uid="BK-2", start="2026-11-18 11:15:00", end="2026-11-18 12:00:00"),
			],
		)
		text = pr.calendar_file(two)
		self.assertEqual(text.count("BEGIN:VEVENT"), 2)
		self.assertEqual(text.count("END:VEVENT"), 2)
		self.assertIn("UID:BK-2-20261118T111500@payment-return", text)


class TestThePagesThatFeedTheView(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_mock_setup()
		_ensure_channels()
		for code in ("wallee_web",):
			if not frappe.db.exists("Payment Channel", code):
				frappe.get_doc(
					{
						"doctype": "Payment Channel",
						"channel_code": code,
						"display_label": code,
						"ui_kind": "redirect",
					}
				).insert(ignore_permissions=True)

	def setUp(self):
		self.made = []
		frappe.set_user("Guest")
		self.addCleanup(self.clean)

	def clean(self):
		frappe.set_user("Administrator")
		for name in self.made:
			frappe.db.delete("Payment Event", {"intent": name})
			frappe.db.delete("Payment Intent", {"name": name})

	def an_intent(self, channel, status):
		frappe.set_user("Administrator")
		doc = frappe.get_doc(
			{
				"doctype": "Payment Intent",
				"status": status,
				"provider": "mock",
				"channel": channel,
				"amount": 5610,
				"currency": "CHF",
				"error_message": "The card was declined" if status in ("failed", "canceled") else None,
			}
		).insert(ignore_permissions=True)
		frappe.set_user("Guest")
		self.made.append(doc.name)
		return doc.name

	def page(self, module, **form):
		context = frappe._dict()
		with patch.object(frappe, "form_dict", frappe._dict(form)), patch.object(
			pr, "summary_of", return_value=None
		):
			if module == "wallee_success":
				from payments.www.wallee.success import get_context
			elif module == "wallee_failed":
				from payments.www.wallee.failed import get_context
			else:
				from payments.www.payrexx.success import get_context
			get_context(context)
		return context

	def test_the_wallee_success_page_celebrates_a_payment_that_arrived(self):
		name = self.an_intent("wallee_web", "succeeded")
		context = self.page("wallee_success", payment_intent=name, access_token=web_access_token(name))
		self.assertEqual((context.view["kind"], context.view["celebrate"]), ("success", True))

	def test_the_wallee_success_page_waits_for_one_that_is_still_in_flight(self):
		name = self.an_intent("wallee_web", "requires_action")
		context = self.page("wallee_success", payment_intent=name, access_token=web_access_token(name))
		self.assertEqual(context.view["kind"], "pending")

	def test_a_payment_that_is_not_this_browsers_reads_as_not_found(self):
		name = self.an_intent("wallee_web", "succeeded")
		for token in (None, "0" * 40, web_access_token("PI-OTHER")):
			with self.subTest(token=token):
				for module in ("wallee_success", "wallee_failed"):
					context = self.page(module, payment_intent=name, access_token=token)
					self.assertEqual(context.view["kind"], "not_found")
					self.assertIsNone(
						context.view["reference"], "a refusal does not repeat the name it was given"
					)

	def test_no_reference_at_all_is_not_found_too(self):
		self.assertEqual(self.page("wallee_success").view["kind"], "not_found")
		self.assertEqual(self.page("wallee_failed").view["kind"], "not_found")
		self.assertEqual(self.page("payrexx").view["kind"], "not_found")

	def test_the_wallee_failed_page_says_failed_or_left_and_gives_the_gateways_words(self):
		failed = self.an_intent("wallee_web", "failed")
		context = self.page(
			"wallee_failed", payment_intent=failed, access_token=web_access_token(failed)
		)
		self.assertEqual(context.view["kind"], "failed")
		self.assertEqual(context.view["detail"], "The card was declined")
		left = self.an_intent("wallee_web", "canceled")
		context = self.page("wallee_failed", payment_intent=left, access_token=web_access_token(left))
		self.assertEqual(context.view["kind"], "canceled")

	def test_the_wallee_failed_page_says_failed_for_a_payment_the_gateway_has_not_yet_told_us_about(
		self,
	):
		name = self.an_intent("wallee_web", "requires_action")
		context = self.page("wallee_failed", payment_intent=name, access_token=web_access_token(name))
		self.assertEqual(context.view["kind"], "failed")

	def test_a_buyer_on_the_failure_address_whose_payment_arrived_is_sent_to_the_success_page_with_her_token(
		self,
	):
		name = self.an_intent("wallee_web", "succeeded")
		token = web_access_token(name)
		with self.assertRaises(frappe.Redirect):
			self.page("wallee_failed", payment_intent=name, access_token=token)
		target = urlparse(frappe.local.flags.redirect_location)
		self.assertEqual(target.path, "/wallee/success")
		self.assertEqual(parse_qs(target.query), {"payment_intent": [name], "access_token": [token]})

	def test_the_payrexx_page_reloads_while_waiting_then_asks_to_check_or_pay_again(self):
		name = self.an_intent("payrexx_web", "requires_action")
		token = web_access_token(name)
		waiting = self.page("payrexx", payment_intent=name, access_token=token, **{"try": "2"})
		self.assertEqual(waiting.view["kind"], "pending")
		query = parse_qs(urlparse(waiting.view["reload_url"]).query)
		self.assertEqual((query["try"], query["access_token"]), (["3"], [token]))
		tired = self.page("payrexx", payment_intent=name, access_token=token, **{"try": "15"})
		self.assertEqual(tired.view["kind"], "unconfirmed")
		check = parse_qs(urlparse(tired.view["primary"]["url"]).query)
		self.assertEqual(
			(check["try"], check["access_token"]),
			(["0"], [token]),
			"checking again starts the count over, with the token",
		)
		self.assertEqual(tired.view["secondary"]["label"], "Try paying again")

	def test_the_payrexx_page_title_follows_the_state_not_the_route(self):
		name = self.an_intent("payrexx_web", "requires_action")
		token = web_access_token(name)
		self.assertEqual(
			self.page("payrexx", payment_intent=name, access_token=token, **{"try": "15"}).title,
			"This is taking longer than expected",
		)
		self.assertEqual(
			self.page("payrexx", payment_intent=name, access_token=token).title,
			"We are confirming your payment",
		)


class TestTheCalendarDownload(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_mock_setup()
		_ensure_channels()

	def setUp(self):
		frappe.set_user("Administrator")
		self.intent = frappe.get_doc(
			{
				"doctype": "Payment Intent",
				"status": "succeeded",
				"provider": "mock",
				"channel": "payrexx_web",
				"amount": 5610,
				"currency": "CHF",
			}
		).insert(ignore_permissions=True)
		self.till = frappe.get_doc(
			{
				"doctype": "Payment Intent",
				"status": "succeeded",
				"provider": "mock",
				"channel": "terminal",
				"amount": 5610,
				"currency": "CHF",
			}
		).insert(ignore_permissions=True)
		frappe.set_user("Guest")
		self.addCleanup(self.clean)
		frappe.response.clear()

	def clean(self):
		frappe.set_user("Administrator")
		for doc in (self.intent, self.till):
			frappe.db.delete("Payment Event", {"intent": doc.name})
			frappe.db.delete("Payment Intent", {"name": doc.name})
		frappe.response.clear()

	def download(self, name, token, summary=SUMMARY):
		with patch.object(pr, "summary_of", return_value=summary):
			return calendar_api.calendar(name, token)

	def test_the_browser_that_paid_downloads_its_appointment(self):
		self.download(self.intent.name, web_access_token(self.intent.name))
		self.assertEqual(frappe.response["type"], "download")
		self.assertEqual(frappe.response["filename"], "appointment.ics")
		self.assertTrue(frappe.response["filecontent"].startswith("BEGIN:VCALENDAR"))
		self.assertIn("text/calendar", frappe.response["content_type"])

	def test_nobody_else_downloads_it(self):
		for token in (None, "", "0" * 40, web_access_token(self.till.name)):
			with self.subTest(token=token):
				with self.assertRaises(frappe.PermissionError):
					self.download(self.intent.name, token)
		with self.assertRaises(frappe.PermissionError):
			self.download(self.till.name, web_access_token(self.till.name))
		with self.assertRaises(frappe.PermissionError):
			self.download("PI-NOPE-0000", web_access_token("PI-NOPE-0000"))
		self.assertNotIn("filecontent", frappe.response)

	def test_a_payment_that_bought_no_appointment_or_a_cancelled_one_has_no_file(self):
		token = web_access_token(self.intent.name)
		for summary in (None, {"title": "x"}, dict(SUMMARY, cancelled=True), dict(SUMMARY, events=[])):
			with self.subTest(summary=summary):
				with self.assertRaises(frappe.DoesNotExistError):
					self.download(self.intent.name, token, summary)
