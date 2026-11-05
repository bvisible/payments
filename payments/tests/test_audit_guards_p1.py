# //// Neoffice — added file (no upstream equivalent). The guards of the payment return pages, the
# //// Web Form override, the phone's endpoints and the other doors a visitor reaches. An audit of this
# //// app read the code and ran nothing: each guard here is played, with the account or the request
# //// it must refuse AND the one that must still pass, because a guard that stops a buyer or a
# //// cashier costs more than the defect it closes.
# Copyright (c) 2026, Neoffice and Contributors
# License: MIT. See LICENSE
"""A payment page answers what its link may ask, and every real journey still goes through.

Run on a clone, never on a live site::

    bench --site prodclone.local run-tests --module payments.tests.test_audit_guards_p1
"""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.parse import urlencode

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, set_request, today
from frappe.website.page_renderers.template_page import TemplatePage
from frappe.website.serve import get_response

from payments.api import mobile_payments as mp
from payments.api.intent_access import web_access_token
from payments.payment_gateways.doctype.braintree_settings.braintree_settings import BraintreeSettings
from payments.payment_gateways.doctype.stripe_settings import stripe_settings
from payments.templates.pages import payment_cancel, payment_success
from payments.tests.test_intent_api import _ensure_mock_setup

BASE = "https://shop.example.test"
XSS = '"><img src=x onerror=alert(1)></script><script>alert(2)</script>'
OFF_SITE = (
	"https://evil.example/landing",
	"//evil.example/landing",
	"///evil.example/landing",
	"/\\evil.example/landing",
	"\\\\evil.example/landing",
	"https:evil.example",
	"https://shop.example.test@evil.example/",
	"https://shop.example.test.evil.example/",
	"/\t/evil.example",
	"javascript:alert(1)",
	"JaVaScRiPt:alert(1)",
	"data:text/html,<script>alert(1)</script>",
)
ON_SITE = (
	"/thank-you?sales_order=SO-0001",
	"/orders/SO-0001",
	"/my-bookings?token=abc",
	"thank-you?sales_order=SO-0001",
	f"{BASE}/orders/SO-0001",
)

ACCEPT = "frappe.website.doctype.web_form.web_form.accept"
START = "payments.api.mobile_payments.mobile_start_payment"
REFRESH = "payments.api.mobile_payments.mobile_refresh_status"
ABANDON = "payments.api.mobile_payments.mobile_abandon_payment"
RECEIPT = "payments.api.mobile_payments.mobile_receipt"
MAIL = "payments.api.mobile_payments.send_receipt"
CONTEXT = "payments.api.mobile_payments.mobile_context"
ALERTS = (
	"payments.payment_gateways.doctype.twint_bridge_settings.twint_bridge_settings"
	".get_pos_certificate_alerts"
)
STRIPE_PAY = "payments.templates.pages.stripe_checkout.make_payment"
BRAINTREE_PAY = "payments.templates.pages.braintree_checkout.make_payment"
MANDATE = "payments.templates.pages.gocardless_checkout.check_mandate"
CHECKOUT_URL = "payments.utils.utils.get_checkout_url"

PORTAL = "auditp1-portal@yopmail.com"
# The phone's accounts as they are: a plain employee reads the jobs that name him and writes none, a
# technician (Activity User) writes them too. Both are desk accounts.
CASHIER = "auditp1-cashier@yopmail.com"
COLLEAGUE = "auditp1-colleague@yopmail.com"
TECHNICIAN = "auditp1-technician@yopmail.com"
AUDITOR = "auditp1-auditor@yopmail.com"


def _user(email: str, roles: list[str], user_type: str) -> None:
	if frappe.db.exists("User", email):
		frappe.delete_doc("User", email, ignore_permissions=True, force=True)
	frappe.get_doc(
		{
			"doctype": "User",
			"email": email,
			"first_name": email.split("@")[0],
			"send_welcome_email": 0,
			"roles": [{"role": role} for role in roles if frappe.db.exists("Role", role)],
		}
	).insert(ignore_permissions=True)
	frappe.db.set_value("User", email, "user_type", user_type)
	frappe.clear_cache(user=email)


def _call(path: str, **kwargs):
	"""A method as frappe.handler runs it: through the override, whitelist and annotations included."""
	method = frappe.get_attr(frappe.override_whitelisted_method(path))
	frappe.is_whitelisted(method)
	return frappe.call(method, **kwargs)


def _form(**values) -> None:
	frappe.form_dict.clear()
	frappe.form_dict.update(values)


def _page(module, **values):
	"""A page's get_context as the website runs it: a JSON body's list or dict stays in form_dict."""
	_form(**values)
	context = frappe._dict()
	module.get_context(context)
	return context


def _render(route: str, **query) -> tuple[int, str]:
	"""The page as a browser gets it, through the website router."""
	set_request(method="GET", path=f"/{route}", query_string=urlencode(query), base_url=BASE)
	_form(**query)
	response = get_response()
	return response.status_code, response.get_data(as_text=True)


def _leave() -> None:
	frappe.set_user("Administrator")
	frappe.form_dict.clear()
	frappe.local.request = None
	frappe.clear_messages()
	frappe.db.rollback()


def _forget_jinja() -> None:
	frappe.local.jloader = None
	frappe.local.jenv_restricted = frappe.local.jenv_unrestricted = None


@contextmanager
def _payments_serves_success():
	"""Where webshop comes after payments its template takes the route: play a site without it."""
	find = TemplatePage.get_index_path_options

	def not_in_webshop(search_path):
		return () if "/apps/webshop/" in search_path else find(search_path)

	frappe.local.flags.web_pages_apps = [
		app for app in frappe.get_installed_apps() if app != "webshop"
	]
	_forget_jinja()
	try:
		with patch.object(TemplatePage, "get_index_path_options", staticmethod(not_in_webshop)):
			yield
	finally:
		frappe.local.flags.web_pages_apps = None
		_forget_jinja()


def _link(target: str) -> str:
	"""The Continue link to ``target``, however the template quotes it."""
	return "href=[\"']" + re.escape(target) + "[\"']"


@contextmanager
def _website_cache_on(route: str):
	"""The clone runs in developer mode, where the website cache is off: play a real site."""
	frappe.cache.hdel("website_page", route)
	# An earlier message page of this process leaves the flag up, and the cache stays off without a word.
	frappe.local.no_cache = 0
	try:
		with patch.dict(frappe.local.conf, {"developer_mode": 0}):
			yield
	finally:
		frappe.cache.hdel("website_page", route)


def _integration_request(status: str = "Queued") -> str:
	doc = frappe.get_doc(
		{
			"doctype": "Integration Request",
			"integration_request_service": "PayPal",
			"status": status,
			"data": "{}",
		}
	).insert(ignore_permissions=True)
	return doc.name


def _row(doctype: str, name: str, **values):
	"""A bare row: these tests are about who reads it, not about how it came to be."""
	doc = frappe.get_doc({"doctype": doctype, **values})
	doc.name = name
	doc.db_insert()
	return doc


def _payment_request(**values) -> str:
	name = f"ACC-PRQ-AUDITP1-{frappe.generate_hash(length=8)}"
	_row(
		"Payment Request",
		name,
		**{
			"payment_request_type": "Inward",
			"grand_total": 120,
			"outstanding_amount": 120,
			"currency": "CHF",
			"status": "Requested",
			"docstatus": 1,
			"reference_doctype": "Sales Invoice",
			"reference_name": "ACC-SINV-AUDITP1",
			**values,
		},
	)
	return name


class TestPaymentSuccess(FrappeTestCase):
	def setUp(self):
		self.addCleanup(_leave)
		frappe.set_user("Guest")

	def test_a_filter_in_a_parameter_is_refused_before_anything_is_read(self):
		a_filter = {"name": ["like", "G%"]}
		for values in (
			{"doctype": "Role", "docname": a_filter},
			{"doctype": "Role", "docname": ["Guest"]},
			{"doctype": {"name": "Role"}, "docname": "Guest"},
			{"doctype": "Role", "docname": "Guest", "token": a_filter},
		):
			with self.subTest(values=values):
				with patch("frappe.get_doc", wraps=frappe.get_doc) as get_doc:
					with self.assertRaises(frappe.PermissionError):
						_page(payment_success, **values)
				get_doc.assert_not_called()
				frappe.clear_messages()

	def test_a_name_that_exists_and_one_that_does_not_read_the_same(self):
		for doctype, docname in (
			("Role", "Guest"),
			("Role", "no-such-role"),
			("NoSuchDocType", "anything"),
		):
			with self.subTest(doctype=doctype, docname=docname):
				self.assertEqual(
					_page(payment_success, doctype=doctype, docname=docname).payment_message, ""
				)

	def test_a_document_still_gives_its_own_message(self):
		paid = SimpleNamespace(get_payment_success_message=lambda: "Thank you")
		with patch("frappe.get_doc", return_value=paid):
			context = _page(payment_success, doctype="Role", docname="Guest")
		self.assertEqual(context.payment_message, "Thank you")

	def test_the_page_runs_nothing_and_sends_nobody_off_site(self):
		with _payments_serves_success():
			for target in (XSS, *OFF_SITE):
				with self.subTest(target=target):
					status, html = _render(
						"payment-success", doctype="Role", docname="Guest", redirect_to=target
					)
					self.assertEqual(status, 200)
					self.assertNotIn("<img src=x", html)
					self.assertNotIn("<script>alert(2)", html)
					self.assertNotIn("evil.example", html)
					if target != XSS:  # a bad relative path stays a path: escaped, it leads nowhere
						self.assertNotIn("alert(1)", html)

	def test_the_shop_still_gets_its_buyer_back(self):
		with _payments_serves_success():
			for target in ON_SITE:
				with self.subTest(target=target):
					status, html = _render(
						"payment-success", doctype="Role", docname="Guest", redirect_to=target
					)
					self.assertEqual(status, 200)
					self.assertRegex(html, _link(target))
					self.assertIn("window.location.href", html)
					self.assertRegex(html, "[\"']" + re.escape(target) + "[\"']\\s*[;)]")


class TestPaymentCancel(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.rows = [_integration_request() for _ in range(2)]
		frappe.db.commit()
		self.addCleanup(self._clean)
		frappe.set_user("Guest")

	def _clean(self):
		frappe.set_user("Administrator")
		frappe.db.delete("Integration Request", {"name": ["in", self.rows]})
		frappe.db.commit()
		_leave()

	def _status(self, name: str) -> str:
		return frappe.db.get_value("Integration Request", name, "status")

	def test_a_filter_cancels_nothing(self):
		a_filter = {"name": ["in", self.rows]}
		for token in (a_filter, list(self.rows)):
			with self.subTest(token=token):
				with self.assertRaises(frappe.PermissionError):
					_page(payment_cancel, token=token)
				frappe.clear_messages()
				self.assertEqual([self._status(name) for name in self.rows], ["Queued", "Queued"])

	def test_a_payment_that_moved_on_keeps_its_status(self):
		for status in ("Completed", "Authorized", "Failed"):
			with self.subTest(status=status):
				frappe.db.set_value("Integration Request", self.rows[0], "status", status)
				_page(payment_cancel, token=self.rows[0])
				self.assertEqual(self._status(self.rows[0]), status)

	def test_the_buyer_who_turns_back_cancels_their_own_payment_and_only_that(self):
		_page(payment_cancel, token=self.rows[0])
		self.assertEqual(self._status(self.rows[0]), "Cancelled")
		self.assertEqual(self._status(self.rows[1]), "Queued")

	def test_every_buyer_who_turns_back_reaches_the_cancel(self):
		# The page used to be cached by path: the second visitor got the first one's page, and no cancel.
		with _website_cache_on("payment-cancel"):
			for name in self.rows:
				self.assertEqual(_render("payment-cancel", token=name)[0], 200)
		self.assertEqual([self._status(name) for name in self.rows], ["Cancelled", "Cancelled"])

	def test_the_continue_link_stays_on_the_site(self):
		for target in (XSS, *OFF_SITE):
			with self.subTest(target=target):
				html = _render("payment-cancel", redirect_to=target)[1]
				self.assertNotIn("<img src=x", html)
				self.assertNotIn("evil.example", html)
				if target != XSS:  # a bad relative path stays a path: escaped, it leads nowhere
					self.assertNotIn("alert(1)", html)
		for target in ON_SITE:
			with self.subTest(target=target):
				self.assertRegex(_render("payment-cancel", redirect_to=target)[1], _link(target))


class TestPaymentFailed(FrappeTestCase):
	def setUp(self):
		self.addCleanup(_leave)
		frappe.set_user("Guest")

	def test_the_continue_link_stays_on_the_site(self):
		for target in (XSS, *OFF_SITE):
			with self.subTest(target=target):
				html = _render("payment-failed", redirect_to=target)[1]
				self.assertNotIn("<img src=x", html)
				self.assertNotIn("evil.example", html)
				if target != XSS:  # a bad relative path stays a path: escaped, it leads nowhere
					self.assertNotIn("alert(1)", html)
		for target in ON_SITE:
			with self.subTest(target=target):
				self.assertRegex(_render("payment-failed", redirect_to=target)[1], _link(target))

	def test_the_site_configured_host_counts_as_this_site(self):
		with patch.dict(frappe.local.conf, {"host_name": "https://erp.example.test"}):
			html = _render("payment-failed", redirect_to="https://erp.example.test/orders/SO-1")[1]
		self.assertRegex(html, _link("https://erp.example.test/orders/SO-1"))

	def test_one_visitors_link_is_never_served_to_the_next(self):
		with _website_cache_on("payment-failed"):
			_render("payment-failed", redirect_to="/orders/first-visitor")
			html = _render("payment-failed", redirect_to="/orders/second-visitor")[1]
		self.assertIn("/orders/second-visitor", html)
		self.assertNotIn("/orders/first-visitor", html)


class TestCheckoutPagesPrintNoMarkup(FrappeTestCase):
	"""What a link carries is text wherever a page prints it: in the page and in its script."""

	def setUp(self):
		self.addCleanup(_leave)
		frappe.set_user("Administrator")
		self.request = _payment_request()
		frappe.set_user("Guest")

	def _assert_inert(self, html: str) -> None:
		self.assertNotIn("<img src=x", html)
		self.assertNotIn("<script>alert(2)", html)

	def test_stripe(self):
		status, html = _render(
			"stripe_checkout",
			amount="12",
			title=XSS,
			description=XSS,
			reference_doctype="Payment Request",
			reference_docname=self.request,
			payer_name=XSS,
			payer_email=XSS,
			currency=XSS,
			payment_gateway=XSS,
		)
		self.assertEqual(status, 200)
		self._assert_inert(html)
		self.assertIn("&lt;img src=x", html)

	def test_stripe_still_shows_the_buyer_their_details_and_posts_them_back(self):
		_html = _render(
			"stripe_checkout",
			amount="12",
			title="Müller & Söhne",
			description="Invoice 12",
			reference_doctype="Payment Request",
			reference_docname=self.request,
			payer_name="Anne O'Neil",
			payer_email="anne@example.test",
			currency="CHF",
			payment_gateway="Stripe-Test",
		)[1]
		self.assertIn("Müller &amp; Söhne", _html)
		self.assertIn("Invoice 12", _html)
		self.assertIn("anne@example.test", _html)
		self.assertIn(f'"reference_docname": "{self.request}"', _html)
		self.assertIn('"payment_gateway": "Stripe-Test"', _html)

	def test_a_filter_on_the_stripe_page_is_refused(self):
		values = {
			"amount": "12",
			"title": "t",
			"description": "d",
			"reference_doctype": "Payment Request",
			"reference_docname": {"name": ["like", "%"]},
			"payer_name": "p",
			"payer_email": "e@example.test",
			"currency": "CHF",
			"payment_gateway": "",
		}
		from payments.templates.pages import stripe_checkout

		with self.assertRaises(frappe.PermissionError):
			_page(stripe_checkout, **values)

	def test_braintree(self):
		with (
			patch("payments.templates.pages.braintree_checkout.get_client_token", return_value="tok"),
			patch("payments.templates.pages.braintree_checkout.get_gateway_controller"),
		):
			status, html = _render(
				"braintree_checkout",
				amount="12",
				title=XSS,
				description=XSS,
				reference_doctype="Payment Request",
				reference_docname=self.request,
				payer_name=XSS,
				payer_email=XSS,
				order_id=XSS,
				currency=XSS,
			)
		self.assertEqual(status, 200)
		self._assert_inert(html)

	def test_gocardless_checkout(self):
		with patch("payments.templates.pages.gocardless_checkout.get_gateway_controller"):
			status, html = _render(
				"gocardless_checkout",
				amount="12",
				title=XSS,
				description=XSS,
				reference_doctype="Payment Request",
				reference_docname=self.request,
				payer_name=XSS,
				payer_email=XSS,
				order_id=XSS,
				currency=XSS,
			)
		self.assertEqual(status, 200)
		self._assert_inert(html)

	def test_gocardless_confirmation_prints_nothing_of_the_link_into_its_script(self):
		status, html = _render(
			"gocardless_confirmation",
			redirect_flow_id=XSS,
			reference_doctype=XSS,
			reference_docname=XSS,
		)
		self.assertEqual(status, 200)
		self._assert_inert(html)

	def test_razorpay(self):
		frappe.set_user("Administrator")
		details = dict.fromkeys(
			(
				"title",
				"description",
				"reference_doctype",
				"reference_docname",
				"payer_name",
				"payer_email",
				"order_id",
				"currency",
			),
			XSS,
		)
		token = (
			frappe.get_doc(
				{
					"doctype": "Integration Request",
					"integration_request_service": "Razorpay",
					"status": "Queued",
					"data": json.dumps({"amount": 12, **details}),
				}
			)
			.insert(ignore_permissions=True)
			.name
		)
		frappe.set_user("Guest")
		status, html = _render("razorpay_checkout", token=token, note=XSS)
		self.assertEqual(status, 200)
		self._assert_inert(html)

	def test_paytm(self):
		frappe.set_user("Administrator")
		token = _integration_request()
		frappe.set_user("Guest")
		with (
			patch(
				"payments.templates.pages.paytm_checkout.get_paytm_config",
				return_value=frappe._dict(url="https://paytm.example.test"),
			),
			patch(
				"payments.templates.pages.paytm_checkout.get_paytm_params",
				return_value={"CUST_ID": XSS, XSS: "1"},
			),
		):
			status, html = _render("paytm_checkout", order_id=token)
		self.assertEqual(status, 200)
		self._assert_inert(html)


class TestWebFormOverride(FrappeTestCase):
	def setUp(self):
		self.addCleanup(_leave)
		frappe.set_user("Administrator")
		_user(PORTAL, ["Customer"], "Website User")
		if not frappe.db.exists("Payment Gateway", "auditp1"):
			frappe.get_doc({"doctype": "Payment Gateway", "gateway": "auditp1"}).insert(
				ignore_permissions=True
			)

	def _web_form(self, **values):
		name = f"auditp1-{frappe.generate_hash(length=8)}"
		return frappe.get_doc(
			{
				"doctype": "Web Form",
				"title": name,
				"route": name,
				"doc_type": "Note",
				"module": "Website",
				"is_standard": 0,
				"published": 1,
				"web_form_fields": [
					{"fieldname": "title", "fieldtype": "Data", "label": "Title", "reqd": 1},
					{"fieldname": "content", "fieldtype": "Text Editor", "label": "Content"},
				],
				**values,
			}
		).insert(ignore_permissions=True)

	def _title(self) -> str:
		return f"auditp1 {frappe.generate_hash(length=10)}"

	def _browser_data(self, **values) -> str:
		"""What web_form.js posts: the document, its doctype included."""
		return json.dumps({"doctype": "Note", **values})

	def test_the_http_path_still_comes_here(self):
		self.assertEqual(
			frappe.override_whitelisted_method(ACCEPT), "payments.overrides.payment_webform.accept"
		)

	def test_the_form_decides_the_doctype_not_the_request(self):
		form = self._web_form()
		title = self._title()
		frappe.set_user("Guest")
		doc = _call(ACCEPT, web_form=form.name, data=json.dumps({"doctype": "Email Group", "title": title}))
		frappe.set_user("Administrator")
		self.assertEqual(doc.doctype, "Note")
		self.assertFalse(frappe.db.exists("Email Group", title))

	def test_an_unpublished_form_takes_nothing(self):
		form = self._web_form(published=0)
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			_call(ACCEPT, web_form=form.name, data=self._browser_data(title=self._title()))

	def test_a_form_that_wants_a_key_takes_nothing_without_it(self):
		form = self._web_form(key_required=1)
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			_call(ACCEPT, web_form=form.name, data=self._browser_data(title=self._title()))

	def test_an_anonymous_form_keeps_nobodys_name(self):
		form = self._web_form(anonymous=1)
		frappe.set_user(PORTAL)
		doc = _call(ACCEPT, web_form=form.name, data=self._browser_data(title=self._title()))
		self.assertEqual(doc.owner, "Guest")

	def test_a_form_takes_a_post_only(self):
		method = frappe.get_attr(frappe.override_whitelisted_method(ACCEPT))
		self.assertEqual(
			sorted(frappe.allowed_http_methods_for_whitelisted_func[method]), ["POST", "PUT"]
		)

	def test_a_form_keeps_the_limit_of_core(self):
		form = self._web_form()
		set_request(method="POST", path="/api/method/" + ACCEPT, base_url=BASE)
		frappe.local.request_ip = "203.0.113.78"
		frappe.form_dict.clear()
		frappe.form_dict.update({"cmd": ACCEPT, "web_form": form.name})
		frappe.cache.delete_keys("rl:")
		self.addCleanup(lambda: frappe.cache.delete_keys("rl:"))
		accept = frappe.get_attr(frappe.override_whitelisted_method(ACCEPT))
		frappe.set_user("Guest")
		for _ in range(10):
			accept(form.name, self._browser_data(title=self._title()))
		with self.assertRaises(frappe.RateLimitExceededError):
			accept(form.name, self._browser_data(title=self._title()))

	def test_a_visitor_still_sends_a_public_form(self):
		form = self._web_form()
		title = self._title()
		frappe.set_user("Guest")
		doc = _call(ACCEPT, web_form=form.name, data=self._browser_data(title=title, content="Hello"))
		self.assertEqual((doc.doctype, doc.title), ("Note", title))

	def test_a_customer_still_edits_their_own_and_only_that(self):
		form = self._web_form(allow_edit=1, login_required=1)
		frappe.set_user(PORTAL)
		doc = _call(ACCEPT, web_form=form.name, data=self._browser_data(title=self._title()))
		updated = _call(
			ACCEPT,
			web_form=form.name,
			data=self._browser_data(name=doc.name, title=doc.title, content="changed"),
		)
		self.assertEqual(updated.content, "changed")
		frappe.set_user("Administrator")
		foreign = frappe.get_doc({"doctype": "Note", "title": self._title()}).insert()
		frappe.set_user(PORTAL)
		with self.assertRaises(frappe.PermissionError):
			_call(
				ACCEPT,
				web_form=form.name,
				data=self._browser_data(name=foreign.name, title=foreign.title, content="x"),
			)

	def _paid_form(self):
		return self._web_form(
			accept_payment=1,
			payment_gateway="auditp1",
			amount=25,
			currency="CHF",
			success_url="/thanks",
		)

	def test_a_paid_form_still_sends_the_visitor_to_the_gateway(self):
		form = self._paid_form()
		title = self._title()
		controller = MagicMock()
		controller.get_payment_url.return_value = "https://gateway.example.test/pay"
		frappe.set_user("Guest")
		with patch("payments.overrides.payment_webform.get_payment_gateway_controller", return_value=controller):
			url = _call(ACCEPT, web_form=form.name, data=self._browser_data(title=title), for_payment="true")
		self.assertEqual(url, "https://gateway.example.test/pay")
		details = controller.get_payment_url.call_args.kwargs
		self.assertEqual(details["reference_doctype"], "Note")
		self.assertEqual(frappe.db.get_value("Note", details["reference_docname"], "title"), title)
		self.assertEqual(details["amount"], 25)
		self.assertEqual(details["currency"], "CHF")

	def test_a_paid_form_refuses_a_missing_field(self):
		form = self._paid_form()
		frappe.set_user("Guest")
		with self.assertRaises(frappe.ValidationError):
			_call(ACCEPT, web_form=form.name, data=self._browser_data(content="x"), for_payment="true")

	def test_a_form_without_payment_ignores_the_flag(self):
		form = self._web_form()
		title = self._title()
		frappe.set_user("Guest")
		doc = _call(ACCEPT, web_form=form.name, data=self._browser_data(title=title), for_payment="true")
		self.assertEqual(doc.title, title)


class TestPhonePayments(FrappeTestCase):
	def setUp(self):
		if not frappe.db.exists("DocType", "Activity"):
			self.skipTest("the phone collects for an Activity: neoffice_activity is not on this site")
		self.addCleanup(_leave)
		frappe.set_user("Administrator")
		_ensure_mock_setup()
		for channel in mp.MOBILE_CHANNELS:
			if not frappe.db.exists("Payment Channel", channel):
				frappe.get_doc(
					{"doctype": "Payment Channel", "channel_code": channel, "display_label": channel}
				).insert(ignore_permissions=True)
			if not frappe.db.exists("Provider Channel Settings", {"provider": "mock", "channel": channel}):
				frappe.get_doc(
					{"doctype": "Provider Channel Settings", "provider": "mock", "channel": channel, "enabled": 1}
				).insert(ignore_permissions=True)
		self.activity = f"ACT-AUDITP1-{frappe.generate_hash(length=8)}"
		_row("Activity", self.activity, title="Boiler", status="Open")
		for user in (CASHIER, COLLEAGUE, TECHNICIAN):
			# First the Employee, then the account: hrms takes the role away from an account without one.
			employee = f"HR-EMP-AUDITP1-{frappe.generate_hash(length=8)}"
			_row("Employee", employee, first_name=user, status="Active", user_id=user)
			roles = ["Employee", "Activity User"] if user == TECHNICIAN else ["Employee"]
			_user(user, roles, "System User")
			row = frappe.get_doc(
				{
					"doctype": "Activity Employee",
					"parent": self.activity,
					"parenttype": "Activity",
					"parentfield": "assigned_employees",
					"employee": employee,
				}
			)
			row.name = frappe.generate_hash(length=10)
			row.db_insert()
		_user(PORTAL, ["Customer"], "Website User")
		_user(AUDITOR, ["Accounts User"], "System User")
		settings = frappe._dict(
			enable_tap_to_pay=1,
			tap_to_pay_provider="mock",
			stripe_location="tml_auditp1",
			enable_twint=1,
			twint_provider="mock",
		)
		patcher = patch.object(mp, "_settings", return_value=settings)
		patcher.start()
		self.addCleanup(patcher.stop)

	def _start(self, method: str = "card", on_the_job: bool = False) -> str:
		reference = (
			{"reference_doctype": "Activity", "reference_name": self.activity} if on_the_job else {}
		)
		return _call(START, amount=500, method=method, **reference)["intent_name"]

	def test_a_portal_account_starts_nothing_on_a_document_it_can_read(self):
		frappe.set_user(PORTAL)
		self.assertTrue(frappe.has_permission("Country", "read", doc="Switzerland"))
		before = frappe.db.count("Payment Intent")
		with self.assertRaises(frappe.PermissionError):
			_call(
				START,
				amount=500,
				method="card",
				reference_doctype="Country",
				reference_name="Switzerland",
			)
		self.assertEqual(frappe.db.count("Payment Intent"), before)

	def test_a_portal_account_follows_no_phone_payment(self):
		frappe.set_user(CASHIER)
		name = _call(
			START, amount=500, method="card", reference_doctype="Country", reference_name="Switzerland"
		)["intent_name"]
		frappe.set_user(PORTAL)
		for path in (REFRESH, ABANDON):
			with self.subTest(path=path):
				with self.assertRaises(frappe.PermissionError):
					_call(path, intent_name=name)
				frappe.clear_messages()

	def test_the_phones_accounts_still_take_a_payment_both_ways(self):
		for user in (CASHIER, TECHNICIAN):
			for method in ("card", "twint"):
				for on_the_job in (True, False):
					with self.subTest(user=user, method=method, on_the_job=on_the_job):
						frappe.set_user(user)
						name = self._start(method, on_the_job)
						self.assertEqual(frappe.db.get_value("Payment Intent", name, "owner"), user)

	def test_the_phone_still_follows_abandons_and_mails_its_own_payment(self):
		frappe.set_user(CASHIER)
		for on_the_job in (True, False):
			with self.subTest(on_the_job=on_the_job):
				name = self._start(on_the_job=on_the_job)
				self.assertTrue(_call(REFRESH, intent_name=name)["client_secret"])
				self.assertEqual(_call(RECEIPT, intent_name=name)["intent_name"], name)
				self.assertEqual(_call(ABANDON, intent_name=name)["status"], "canceled")
				with patch("frappe.sendmail"):
					self.assertTrue(
						_call(MAIL, intent_name=name, email="customer@example.test")["sent"]
					)

	def test_a_colleague_cannot_touch_a_payment_taken_on_its_own(self):
		frappe.set_user(CASHIER)
		name = self._start()
		frappe.set_user(COLLEAGUE)
		for path, kwargs in (
			(REFRESH, {}),
			(RECEIPT, {}),
			(ABANDON, {}),
			(MAIL, {"email": "someone@example.test"}),
		):
			with self.subTest(path=path):
				with self.assertRaises(frappe.PermissionError):
					_call(path, intent_name=name, **kwargs)
				frappe.clear_messages()
		frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value("Payment Intent", name, "status"), "requires_action")

	def test_an_account_the_doctype_permits_still_reads_it(self):
		frappe.set_user(CASHIER)
		name = self._start()
		frappe.set_user(AUDITOR)
		self.assertEqual(_call(REFRESH, intent_name=name)["intent_name"], name)
		with self.assertRaises(frappe.PermissionError):
			_call(ABANDON, intent_name=name)

	def test_reading_the_job_is_no_leave_to_abandon_its_payment(self):
		frappe.set_user(CASHIER)
		name = self._start(on_the_job=True)
		frappe.set_user(COLLEAGUE)
		self.assertTrue(frappe.has_permission("Activity", "read", doc=self.activity))
		self.assertFalse(frappe.has_permission("Activity", "write", doc=self.activity))
		with self.assertRaises(frappe.PermissionError):
			_call(ABANDON, intent_name=name)
		frappe.clear_messages()
		with self.assertRaises(frappe.PermissionError):
			_call(MAIL, intent_name=name, email="someone@example.test")
		frappe.clear_messages()
		frappe.set_user(TECHNICIAN)
		self.assertTrue(frappe.has_permission("Activity", "write", doc=self.activity))
		self.assertEqual(_call(ABANDON, intent_name=name)["status"], "canceled")

	def test_a_reader_of_the_job_follows_the_payment_without_its_secrets(self):
		frappe.set_user(CASHIER)
		name = self._start(on_the_job=True)
		frappe.set_user(COLLEAGUE)
		seen = _call(REFRESH, intent_name=name)
		self.assertEqual(seen["status"], "requires_action")
		self.assertFalse(seen.get("client_secret"))
		self.assertFalse(seen.get("next_action_payload"))
		self.assertFalse(seen.get("provider_intent_id"))

	def test_a_portal_account_reads_no_on_site_configuration(self):
		frappe.set_user(PORTAL)
		context = _call(CONTEXT)
		self.assertEqual(context["methods"], [])
		self.assertEqual(context["card"], {"enabled": False, "provider": None, "location_id": None})
		self.assertEqual(context["twint"], {"enabled": False, "provider": None})

	def test_the_phone_still_reads_what_it_may_offer(self):
		frappe.set_user(CASHIER)
		context = _call(CONTEXT)
		self.assertEqual(context["methods"], ["card", "twint"])
		self.assertEqual(context["card"]["location_id"], "tml_auditp1")
		self.assertEqual(context["twint"]["provider"], "mock")

	def test_the_twint_alert_is_for_the_people_who_work_here(self):
		rows = [
			frappe._dict(
				name="merchant",
				display_label="Main till",
				certificate_expires_on=add_days(today(), 3),
			)
		]
		frappe.set_user(PORTAL)
		with patch("frappe.get_all", return_value=rows):
			self.assertEqual(_call(ALERTS), [])
		frappe.set_user(CASHIER)
		with patch("frappe.get_all", return_value=rows):
			alerts = _call(ALERTS)
		self.assertEqual([alert["merchant"] for alert in alerts], ["Main till"])


class TestStripeMakePayment(FrappeTestCase):
	def setUp(self):
		self.addCleanup(_leave)
		frappe.set_user("Guest")

	def _pay(self, doctype, docname):
		return _call(
			STRIPE_PAY,
			stripe_token_id="tok_auditp1",
			data="{}",
			reference_doctype=doctype,
			reference_docname=docname,
			payment_gateway="",
		)

	def test_a_guest_cannot_tell_which_document_exists(self):
		for doctype, docname in (
			("User", "Administrator"),
			("User", "no-such-user@example.invalid"),
			("NoSuchDocType", "anything"),
		):
			with self.subTest(doctype=doctype, docname=docname):
				with self.assertRaises(frappe.PermissionError):
					self._pay(doctype, docname)
				frappe.clear_messages()

	def test_a_filter_never_reaches_the_query(self):
		with self.assertRaises(frappe.FrappeTypeError):
			self._pay("User", {"name": ["like", "A%"]})

	def test_a_request_still_finds_its_gateway(self):
		frappe.set_user("Administrator")
		gateway = f"Stripe-auditp1-{frappe.generate_hash(length=6)}"
		_row("Payment Gateway", gateway, gateway=gateway, gateway_controller="auditp1")
		request = _payment_request(payment_gateway=gateway)
		frappe.set_user("Guest")
		self.assertEqual(stripe_settings.get_gateway_controller("Payment Request", request), "auditp1")


class TestBraintree(FrappeTestCase):
	def setUp(self):
		self.addCleanup(_leave)
		frappe.set_user("Administrator")
		self.gateway = f"auditp1-{frappe.generate_hash(length=6)}"
		_row("Braintree Settings", self.gateway, gateway_name=self.gateway)
		_row(
			"Payment Gateway",
			f"Braintree-{self.gateway}",
			gateway=f"Braintree-{self.gateway}",
			gateway_controller=self.gateway,
		)
		self.request = _payment_request(payment_gateway=f"Braintree-{self.gateway}")
		self.other = _payment_request(payment_gateway=f"Braintree-{self.gateway}", grand_total=999)
		frappe.set_user("Guest")

	def _pay(self, **data):
		with patch.object(
			BraintreeSettings,
			"create_payment_request",
			return_value={"redirect_to": "payment-success", "status": "Completed"},
		) as charge:
			_call(
				BRAINTREE_PAY,
				payload_nonce="nonce",
				data=json.dumps(data),
				reference_doctype="Payment Request",
				reference_docname=self.request,
			)
		return charge.call_args.args[0]

	def test_the_amount_and_the_document_are_the_servers(self):
		charged = self._pay(amount="0.01", reference_doctype="Payment Request", reference_docname=self.other)
		self.assertEqual(float(charged["amount"]), 120.0)
		self.assertEqual(charged["currency"], "CHF")
		self.assertEqual(charged["reference_doctype"], "Payment Request")
		self.assertEqual(charged["reference_docname"], self.request)

	def test_what_is_left_to_pay_is_what_is_charged(self):
		frappe.set_user("Administrator")
		frappe.db.set_value("Payment Request", self.request, "outstanding_amount", 45)
		frappe.set_user("Guest")
		self.assertEqual(float(self._pay(amount="120")["amount"]), 45.0)

	def test_a_paid_request_takes_no_second_charge(self):
		frappe.set_user("Administrator")
		frappe.db.set_value("Payment Request", self.request, "status", "Paid")
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			self._pay(amount="120")

	def test_a_filter_never_reaches_the_query(self):
		with self.assertRaises(frappe.FrappeTypeError):
			_call(
				BRAINTREE_PAY,
				payload_nonce="nonce",
				data="{}",
				reference_doctype="Payment Request",
				reference_docname={"status": "Requested"},
			)


class TestGoCardlessMandate(FrappeTestCase):
	def setUp(self):
		self.addCleanup(_leave)
		frappe.set_user("Administrator")
		suffix = frappe.generate_hash(length=8)
		self.payer = f"auditp1 payer {suffix}"
		self.victim = f"auditp1 victim {suffix}"
		for customer, first_name in ((self.payer, "PayerFirst"), (self.victim, "VictimFirst")):
			contact = f"auditp1-contact-{frappe.generate_hash(length=8)}"
			_row(
				"Contact",
				contact,
				first_name=first_name,
				last_name="Family",
				email_id=f"{first_name.lower()}@example.test",
			)
			_row(
				"Customer",
				customer,
				customer_name=customer,
				customer_type="Individual",
				customer_primary_contact=contact,
			)
		self.request = _payment_request(party_type="Customer", party=self.payer)
		frappe.set_user("Guest")

	def _mandate(self, **data):
		client = MagicMock()
		client.redirect_flows.create.return_value.redirect_url = "https://gocardless.example.test/flow"
		with patch(
			"payments.templates.pages.gocardless_checkout.gocardless_initialization", return_value=client
		):
			out = _call(
				MANDATE,
				data=json.dumps(data),
				reference_doctype="Payment Request",
				reference_docname=self.request,
			)
		return out, client.redirect_flows.create.call_args.kwargs["params"]

	def test_a_guest_reads_no_customer_through_the_payer_name(self):
		for payer_name in (self.victim, {"customer_name": ["like", "auditp1 victim%"]}):
			with self.subTest(payer_name=payer_name):
				_out, params = self._mandate(payer_name=payer_name, amount=1, currency="EUR")
				self.assertNotIn("Victim", json.dumps(params))
				self.assertNotIn("victim", json.dumps(params))

	def test_the_amount_said_to_the_payer_is_the_servers(self):
		_out, params = self._mandate(payer_name=self.payer, amount=1, currency="EUR")
		self.assertIn("120", params["description"])
		self.assertIn("CHF", params["description"])

	def test_a_visitor_on_the_payment_link_still_reaches_the_gateway(self):
		out, params = self._mandate(payer_name=self.payer, amount=120, currency="CHF")
		self.assertEqual(out, {"redirect_to": "https://gocardless.example.test/flow"})
		self.assertNotIn("PayerFirst", json.dumps(params))

	def test_who_may_read_the_customer_still_gets_the_form_filled_in(self):
		frappe.set_user("Administrator")
		_out, params = self._mandate(payer_name=self.payer, amount=120, currency="CHF")
		self.assertEqual(params["prefilled_customer"]["given_name"], "PayerFirst")
		self.assertEqual(params["prefilled_customer"]["email"], "payerfirst@example.test")

	def test_a_filter_never_reaches_the_query(self):
		with self.assertRaises(frappe.FrappeTypeError):
			_call(
				MANDATE,
				data="{}",
				reference_doctype="Payment Request",
				reference_docname={"status": "Requested"},
			)


class TestPayrexxReturnPage(FrappeTestCase):
	def setUp(self):
		self.addCleanup(_leave)
		frappe.set_user("Administrator")
		_ensure_mock_setup()
		if not frappe.db.exists("Payment Channel", "payrexx_web"):
			frappe.get_doc(
				{
					"doctype": "Payment Channel",
					"channel_code": "payrexx_web",
					"display_label": "payrexx_web",
					"ui_kind": "redirect",
				}
			).insert(ignore_permissions=True)
		self.intent = (
			frappe.get_doc(
				{
					"doctype": "Payment Intent",
					"status": "requires_action",
					"provider": "mock",
					"channel": "payrexx_web",
					"amount": 1900,
					"currency": "CHF",
					"provider_intent_id": f"mock_pi_auditp1_{frappe.generate_hash(length=12)}",
				}
			)
			.insert(ignore_permissions=True)
			.name
		)
		self.token = web_access_token(self.intent)
		self.addCleanup(self._clean)
		frappe.set_user("Guest")

	def _clean(self):
		frappe.set_user("Administrator")
		frappe.db.delete("Payment Event", {"intent": self.intent})
		frappe.db.delete("Payment Intent", {"name": self.intent})
		frappe.cache.delete_value(f"payrexx_return_read:{self.intent}")
		frappe.db.commit()

	def _load(self, status: str, **query):
		from payments.www.payrexx import success

		driver = MagicMock()
		driver.get_status.return_value = SimpleNamespace(status=status)
		with (
			patch("payments.drivers.registry.resolve_driver", return_value=driver),
			patch("time.sleep") as sleep,
		):
			context = _page(
				success, payment_intent=self.intent, access_token=self.token, **query
			)
		return context, driver, sleep

	def test_a_load_holds_no_worker(self):
		context, driver, sleep = self._load("requires_action")
		sleep.assert_not_called()
		self.assertEqual(driver.get_status.call_count, 1)
		self.assertEqual(context.status, "pending")
		self.assertIn("try=1", context.retry_url)

	def test_a_flood_of_loads_asks_the_gateway_once(self):
		self._load("requires_action")
		_context, driver, _sleep = self._load("requires_action")
		driver.get_status.assert_not_called()

	def test_a_settled_payment_shows_at_once(self):
		context, _driver, sleep = self._load("succeeded")
		sleep.assert_not_called()
		self.assertEqual(context.status, "success")

	def test_the_page_still_tries_as_long_as_it_did(self):
		from payments.www.payrexx import success

		# Fifteen reloads, three seconds apart: the 45 s the page has always given a late settlement.
		self.assertGreaterEqual(success._MAX_PENDING_LOADS * 3, 45)
		context, _driver, _sleep = self._load("requires_action", **{"try": success._MAX_PENDING_LOADS})
		self.assertEqual(context.status, "unconfirmed")


class TestCheckoutUrl(FrappeTestCase):
	def setUp(self):
		self.addCleanup(_leave)
		frappe.set_user("Administrator")
		frappe.db.delete("Payment Gateway", {"name": "Razorpay"})
		self.query = dict(
			payment_gateway="Razorpay",
			reference_doctype="Role",
			reference_docname="Guest",
			amount=1,
			title="t",
			description="d",
			payer_name="p",
			payer_email="p@example.test",
			order_id="o",
			currency="INR",
		)
		frappe.set_user("Guest")

	def test_a_gateway_this_site_never_set_up_writes_nothing(self):
		before = frappe.db.count("Integration Request")
		_call(CHECKOUT_URL, **self.query)
		self.assertEqual(frappe.db.count("Integration Request"), before)

	def test_a_gateway_that_is_set_up_still_answers(self):
		frappe.set_user("Administrator")
		_row("Payment Gateway", "Razorpay", gateway="Razorpay")
		frappe.set_user("Guest")
		with patch(
			"payments.payment_gateways.doctype.razorpay_settings.razorpay_settings"
			".RazorpaySettings.get_payment_url",
			return_value="https://site.example.test/razorpay_checkout?token=abc",
		):
			self.assertIn("razorpay_checkout", _call(CHECKOUT_URL, **self.query))

	def test_the_door_counts_its_callers(self):
		set_request(method="POST", path="/api/method/" + CHECKOUT_URL, base_url=BASE)
		frappe.local.request_ip = "203.0.113.77"
		frappe.form_dict.clear()
		frappe.form_dict.cmd = CHECKOUT_URL
		frappe.cache.delete_keys("rl:")
		self.addCleanup(lambda: frappe.cache.delete_keys("rl:"))
		with self.assertRaises(frappe.RateLimitExceededError):
			for _ in range(50):
				frappe.get_attr(CHECKOUT_URL)(**self.query)
