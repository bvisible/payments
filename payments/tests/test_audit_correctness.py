# //// Neoffice — added file (no upstream equivalent). Audit of the payments app, track Correctness (maintenance#2296):
# //// a payment is booked whole or not at all, its trail has no gap, a provider event or a refund is applied once and
# //// from fresh data, a charge is the amount asked, the provider of a payment does not depend on the last save, and a
# //// certificate whose date is unknown says so.
# Copyright (c) 2026, Neoffice and Contributors
# License: MIT. See LICENSE
"""What a captured payment leaves in the books, and how the money path survives a failure half-way.

No provider is reached: the SDK and HTTP calls are patched, the provider in the payment tests is the app's mock.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from unittest.mock import patch

import frappe
from frappe.model.document import Document
from frappe.tests.roles import as_role
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt, nowdate

from payments.api import intent as intent_api
from payments.api import reconciliation
from payments.tests.test_audit_guards_p2 import (
	STRIPE_A,
	STRIPE_HANDLE,
	STRIPE_SECRETS,
	_WithIntents,
	ensure_provider,
	make_newest,
	stripe_event,
	stripe_signature,
)
from payments.tests.test_intent_api import _ensure_mock_setup

APP = Path(frappe.get_app_path("payments"))
MARK = "auditc"


# ------------------------------------------------------------------ 20, 22: a booking whole, a trail with no gap
class TestACardBookedWholeOrNotAtAll(FrappeTestCase):
	"""A card captured for a submitted invoice is a Payment Entry against it (api/reconciliation._payment_entry)."""

	def setUp(self):
		frappe.set_user("Administrator")
		_ensure_mock_setup()
		company = frappe.defaults.get_global_default("company")
		currency = company and frappe.get_cached_value("Company", company, "default_currency")
		accounts = company and frappe.get_all(
			"Mode of Payment Account",
			filters={"default_account": ["is", "set"], "parenttype": "Mode of Payment", "company": company},
			fields=["parent", "company", "default_account"],
			order_by="parent",
		)
		self.mop = next(
			(
				row
				for row in accounts or []
				if frappe.db.get_value("Account", row.default_account, "account_currency") == currency
			),
			None,
		)
		customers = frappe.get_all(
			"Customer",
			filters={"disabled": 0},
			or_filters=[["default_currency", "is", "not set"], ["default_currency", "=", currency]]
			if currency
			else None,
			pluck="name",
			order_by="name",
			limit=1,
		)
		item = frappe.db.get_value(
			"Item", {"disabled": 0, "is_stock_item": 0, "is_sales_item": 1, "has_variants": 0}, "name"
		)
		if not (self.mop and customers and item):
			self.skipTest("a mode of payment with an account, a customer and a service item are needed")
		self.intents = []
		# The office raises and sends the invoice; the card is taken later, at the handover.
		with as_role("head_accountant"):
			self.si = frappe.get_doc(
				{
					"doctype": "Sales Invoice",
					"company": self.mop.company,
					"customer": customers[0],
					"posting_date": nowdate(),
					"due_date": nowdate(),
					"items": [{"item_code": item, "qty": 1, "rate": 100}],
				}
			)
			self.si.insert()
			self.si.submit()

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		for pe in frappe.get_all(
			"Payment Entry Reference", filters={"reference_name": self.si.name}, pluck="parent"
		):
			doc = frappe.get_doc("Payment Entry", pe)
			if doc.docstatus == 1:
				doc.cancel()
			frappe.delete_doc("Payment Entry", pe, force=True, ignore_permissions=True)
		for name in self.intents:
			frappe.db.delete("Payment Event", {"intent": name})
			frappe.delete_doc("Payment Intent", name, force=True, ignore_permissions=True)
		self.si.reload()
		if self.si.docstatus == 1:
			self.si.cancel()
		frappe.delete_doc("Sales Invoice", self.si.name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def an_intent(self):
		out = intent_api.create_intent(
			provider="mock",
			channel="terminal",
			amount=int(round(flt(self.si.outstanding_amount) * 100)),
			currency=self.si.currency,
			reference_doctype="Sales Invoice",
			reference_name=self.si.name,
			metadata={"mode_of_payment": self.mop.parent},
		)
		self.intents.append(out["intent_name"])
		frappe.db.commit()
		return frappe.get_doc("Payment Intent", out["intent_name"])

	def entries(self, intent):
		return frappe.get_all(
			"Payment Entry",
			filters={"reference_no": intent.provider_intent_id},
			fields=["name", "docstatus"],
		)

	def test_a_payment_entry_refused_at_submit_leaves_nothing_and_is_booked_at_the_next_save(self):
		from erpnext.accounts.doctype.payment_entry.payment_entry import PaymentEntry

		intent = self.an_intent()
		# The books refuse the entry once it is written (frozen books, no fiscal year): its on_submit raises.
		with patch.object(PaymentEntry, "on_submit", side_effect=frappe.ValidationError("books frozen")):
			with as_role("Guest"):  # the provider's webhook has no session
				intent.transition_to("succeeded", event_source="webhook")
		frappe.db.commit()  # the job ends: what it wrote is committed

		self.assertEqual(self.entries(intent), [], "a refused booking left a Payment Entry behind")
		self.assertEqual(frappe.db.get_value("Payment Intent", intent.name, "status"), "succeeded")
		self.assertNotIn("reconciled_at", frappe.get_doc("Payment Intent", intent.name).get_metadata())
		self.assertEqual(
			flt(frappe.db.get_value("Sales Invoice", self.si.name, "outstanding_amount")),
			flt(self.si.outstanding_amount),
		)
		# The trail of the move is whole all the same: the payment was captured.
		self.assertTrue(frappe.db.exists("Payment Event", {"intent": intent.name, "to_status": "succeeded"}))

		# The books open again: the next save of the intent books it.
		with as_role("Guest"):
			frappe.get_doc("Payment Intent", intent.name).save(ignore_permissions=True)
		booked = self.entries(intent)
		self.assertEqual([row.docstatus for row in booked], [1])
		self.assertEqual(flt(frappe.db.get_value("Sales Invoice", self.si.name, "outstanding_amount")), 0)

	def test_the_move_is_committed_with_its_payment_event(self):
		intent = self.an_intent()
		events_at_commit = []
		real_commit = frappe.db.commit

		def commit(*args, **kwargs):
			events_at_commit.append(
				frappe.db.count("Payment Event", {"intent": intent.name, "to_status": "succeeded"})
			)
			return real_commit(*args, **kwargs)

		with patch.object(frappe.db, "commit", side_effect=commit):
			with as_role("Guest"):
				intent.transition_to("succeeded", event_source="webhook")
		self.assertTrue(events_at_commit, "a settled payment was not committed")
		self.assertNotIn(0, events_at_commit, "the move was committed before its Payment Event was written")
		self.assertEqual([row.docstatus for row in self.entries(intent)], [1])

	def test_the_doc_event_commits_nothing(self):
		intent = self.an_intent()
		frappe.db.set_value("Payment Intent", intent.name, "status", "succeeded")
		intent.reload()
		with patch.object(frappe.db, "commit") as commit, as_role("Guest"):
			reconciliation.on_payment_intent_after_update(intent)
		commit.assert_not_called()
		self.assertEqual([row.docstatus for row in self.entries(intent)], [1])

	def test_a_return_page_move_is_logged(self):
		# The two return pages move an intent with event_source="redirect", which the Select did not hold.
		meta = frappe.get_meta("Payment Event")
		self.assertIn("redirect", (meta.get_field("event_source").options or "").split("\n"))
		intent = self.an_intent()
		with as_role("Guest"):
			intent.transition_to("succeeded", event_source="redirect")
		self.assertTrue(
			frappe.db.exists(
				"Payment Event",
				{"intent": intent.name, "to_status": "succeeded", "event_source": "redirect"},
			)
		)


# ------------------------------------------------------------------------------ 21: a gateway's success page
class TestTheGatewayHookIsBookedWholeOrNotAtAll(FrappeTestCase):
	"""`on_payment_authorized` of the request a gateway settles: what it wrote before it failed is undone."""

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		frappe.db.delete("ToDo", {"description": ["like", f"{MARK}%"]})
		frappe.db.delete("Integration Request", {"request_description": MARK})
		frappe.db.commit()

	def a_hook_that_fails_half_way(self):
		def run_method(doc, method, *args, **kwargs):
			if method != "on_payment_authorized":
				return None
			frappe.get_doc({"doctype": "ToDo", "description": f"{MARK} half a booking"}).insert(
				ignore_permissions=True
			)
			raise frappe.ValidationError("the invoice could not be made")

		return patch.object(Document, "run_method", autospec=True, side_effect=run_method)

	def a_reference(self):
		return frappe.get_doc({"doctype": "ToDo", "description": f"{MARK} reference"}).insert(
			ignore_permissions=True
		)

	def half_bookings(self):
		return frappe.db.count("ToDo", {"description": f"{MARK} half a booking"})

	def test_stripe(self):
		from payments.payment_gateways.doctype.stripe_settings.stripe_settings import StripeSettings

		reference = self.a_reference()
		settings = frappe.new_doc("Stripe Settings")
		settings.data = frappe._dict(reference_doctype="ToDo", reference_docname=reference.name)
		settings.integration_request = frappe._dict(status="Completed")
		settings.flags.status_changed_to = "Completed"
		settings.redirect_url = None
		with self.a_hook_that_fails_half_way(), as_role("Guest"):
			answer = StripeSettings.finalize_request(settings)
		self.assertEqual(self.half_bookings(), 0, "what the hook wrote before it failed stayed")
		self.assertIn("payment-success", answer["redirect_to"], "the buyer was charged: the page says so")

	def test_paytm(self):
		from payments.payment_gateways.doctype.paytm_settings import paytm_settings

		reference = self.a_reference()
		request = frappe.get_doc(
			{
				"doctype": "Integration Request",
				"integration_request_service": "Paytm",
				"request_description": MARK,
				"status": "Queued",
				"data": json.dumps({"reference_doctype": "ToDo", "reference_docname": reference.name}),
			}
		).insert(ignore_permissions=True)
		with self.a_hook_that_fails_half_way(), as_role("Guest"):
			paytm_settings.finalize_request(request.name, {"STATUS": "TXN_SUCCESS"})
		self.assertEqual(self.half_bookings(), 0, "what the hook wrote before it failed stayed")

	def test_the_other_gateways_book_in_a_savepoint(self):
		# Razorpay, Braintree and GoCardless need their SDK's answer to reach the hook: their call is read here.
		for path in (
			"payment_gateways/doctype/razorpay_settings/razorpay_settings.py",
			"payment_gateways/doctype/braintree_settings/braintree_settings.py",
			"payment_gateways/doctype/gocardless_settings/gocardless_settings.py",
		):
			lines = (APP / path).read_text().splitlines()
			calls = [i for i, line in enumerate(lines) if '"on_payment_authorized"' in line]
			with self.subTest(path=path):
				self.assertTrue(calls)
				for i in calls:
					self.assertTrue(
						any("with all_or_nothing(" in line for line in lines[max(0, i - 4) : i]),
						f"{path}:{i + 1} books outside a savepoint",
					)


# ------------------------------------------------------------------------------------- 23: Mpesa's settings
class TestMpesaSettingsSaveWhole(FrappeTestCase):
	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def test_the_save_commits_nothing_half_way(self):
		from payments.payment_gateways.doctype.mpesa_settings import mpesa_settings

		doc = frappe.new_doc("Mpesa Settings")
		doc.payment_gateway_name = f"{MARK}-gateway"
		with (
			patch.object(frappe.db, "commit") as commit,
			patch.object(mpesa_settings, "create_custom_pos_fields"),
			patch.object(mpesa_settings, "call_hook_method"),
			patch.object(mpesa_settings, "create_mode_of_payment") as create_mode_of_payment,
			patch("payments.utils.create_payment_gateway"),
		):
			doc.on_update()
		commit.assert_not_called()
		create_mode_of_payment.assert_called_once()

	def test_the_mode_of_payment_takes_the_company_of_its_account(self):
		from payments.payment_gateways.doctype.mpesa_settings import mpesa_settings

		companies = frappe.get_all("Company", pluck="name", order_by="creation")
		account = frappe.db.get_value(
			"Account",
			{"account_type": "Bank", "is_group": 0, "disabled": 0},
			["name", "company"],
			as_dict=True,
		)
		other = next((c for c in companies if account and c != account.company), None)
		if not (account and other):
			self.skipTest("two companies and a bank account are needed")
		gateway = f"Mpesa-{MARK}"
		if not frappe.db.exists("Payment Gateway", gateway):
			frappe.get_doc({"doctype": "Payment Gateway", "gateway": gateway}).insert(ignore_permissions=True)
		frappe.get_doc(
			{
				"doctype": "Payment Gateway Account",
				"name": f"{gateway} - {MARK}",
				"payment_gateway": gateway,
				"payment_account": account.name,
				"currency": frappe.db.get_value("Account", account.name, "account_currency")
				or frappe.get_cached_value("Company", account.company, "default_currency"),
			}
		).db_insert()
		# The one who saves the settings works in another company than the site's.
		with as_role("head_accountant") as user:
			frappe.defaults.set_user_default("company", other, user)
			try:
				mode = mpesa_settings.create_mode_of_payment(gateway, payment_type="Phone")
			finally:
				frappe.defaults.clear_user_default("company", user)
		self.assertEqual(
			[(row.company, row.default_account) for row in mode.accounts], [(account.company, account.name)]
		)


# ------------------------------------------------------------------- 24, 25: a provider event, once and fresh
class TestAWebhookEventIsAppliedOnceAndFresh(_WithIntents):
	def setUp(self):
		super().setUp()
		make_newest(STRIPE_A)

	def a_delivery(self, intent):
		body = stripe_event(intent)
		return body, {"Stripe-Signature": stripe_signature(body, STRIPE_SECRETS[STRIPE_A])}

	def run_worker(self, log_name):
		with as_role("Guest"):
			frappe.get_attr("payments.api.webhook_stripe.process_event")(log_name)

	def test_the_worker_reads_the_intent_under_lock(self):
		intent = self.intent(STRIPE_A)
		log = self.queued_log(self.deliver(STRIPE_HANDLE, *self.a_delivery(intent)))
		real = frappe.get_doc
		locked = []

		def get_doc(*args, **kwargs):
			if args[:1] == ("Payment Intent",):
				locked.append(kwargs.get("for_update"))
			return real(*args, **kwargs)

		with patch.object(frappe, "get_doc", side_effect=get_doc):
			self.run_worker(log)
		self.assertTrue(locked and locked[0], "the worker read the intent with no lock")
		self.assertEqual(self.status_of(intent), "succeeded")

	def test_a_move_that_fails_leaves_its_row_failed_not_queued(self):
		from payments.payments.doctype.payment_intent.payment_intent import PaymentIntent

		intent = self.intent(STRIPE_A)
		log = self.queued_log(self.deliver(STRIPE_HANDLE, *self.a_delivery(intent)))
		with patch.object(
			PaymentIntent, "transition_to", side_effect=frappe.TimestampMismatchError("stale copy")
		):
			self.run_worker(log)
		frappe.db.commit()  # the job's own commit
		row = frappe.db.get_value("Webhook Event Log", log, ["status", "error"], as_dict=True)
		self.assertEqual(row.status, "Failed")
		self.assertIn("stale copy", row.error)
		self.assertEqual(self.status_of(intent), "requires_action")

	def test_a_retry_of_a_row_its_job_never_finished_queues_it_again(self):
		intent = self.intent(STRIPE_A)
		body, headers = self.a_delivery(intent)
		log = self.queued_log(self.deliver(STRIPE_HANDLE, body, headers))
		frappe.db.commit()
		# The queue was down, or the job died: the row stays « Queued ». The provider sends it again.
		again = self.deliver(STRIPE_HANDLE, body, headers)
		self.assertEqual(self.queued_log(again), log)
		self.run_worker(log)
		self.assertEqual(self.status_of(intent), "succeeded")
		# Once applied, a retry is told « ok » and nothing more.
		frappe.db.commit()
		third = self.deliver(STRIPE_HANDLE, body, headers)
		self.assertEqual((third.status, third.answer), (200, "ok"))
		third.enqueue.assert_not_called()


# ------------------------------------------------------------------------------------------- 26: a refund once
class TestARefundReadsItsIntentUnderLock(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		_ensure_mock_setup()
		out = intent_api.create_intent(provider="mock", channel="terminal", amount=1000, currency="CHF")
		self.intent = out["intent_name"]
		frappe.get_doc("Payment Intent", self.intent).transition_to("succeeded")

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		frappe.db.delete("Payment Event", {"intent": self.intent})
		frappe.db.delete("Payment Intent", {"name": self.intent})
		frappe.db.commit()

	def test_the_second_refund_waits_for_the_first_and_finds_it_done(self):
		real = frappe.get_doc
		locked = []

		def get_doc(*args, **kwargs):
			if args[:1] == ("Payment Intent",) and len(args) > 1:
				locked.append(kwargs.get("for_update"))
			return real(*args, **kwargs)

		with as_role("till_manager"), patch.object(frappe, "get_doc", side_effect=get_doc):
			intent_api.refund_intent(self.intent, amount=400)
		self.assertTrue(locked and locked[0], "the intent was read with no lock before the provider was asked")
		with as_role("till_manager"), self.assertRaises(frappe.ValidationError):
			intent_api.refund_intent(self.intent, amount=400)


# ----------------------------------------------------------------------------------- 27: the amount charged
class TestAStripeChargeIsTheAmountAsked(FrappeTestCase):
	def test_the_amount_in_cents_is_rounded(self):
		from payments.utils.stripe_receipt import stripe_minor_units

		for amount, cents in ((4.35, 435), (1.15, 115), (19.90, 1990), (0.29, 29), (10, 1000), (8.5, 850)):
			with self.subTest(amount=amount):
				self.assertEqual(stripe_minor_units(amount), cents)

	def test_the_charge_asks_stripe_for_the_whole_amount(self):
		import stripe

		from payments.payment_gateways.doctype.stripe_settings.stripe_settings import StripeSettings

		settings = frappe.new_doc("Stripe Settings")
		settings.data = frappe._dict(
			amount=19.90, currency="CHF", stripe_token_id="tok", description="x", payer_email="a@b.c"
		)
		settings.integration_request = frappe._dict(db_set=lambda *a, **k: None, status="Failed")
		answer = frappe._dict(captured=False, failure_message="test")
		with (
			patch.object(stripe.Charge, "create", return_value=answer) as create,
			patch.object(StripeSettings, "finalize_request", return_value={}),
			as_role("Guest"),
		):
			settings.create_charge_on_stripe()
		self.assertEqual(create.call_args.kwargs["amount"], 1990)


# ---------------------------------------------------------------------------- 28: the provider of a payment
class TestTheProviderOfAPaymentDoesNotDependOnTheLastSave(FrappeTestCase):
	TEST = f"{MARK}_twint_test"
	LIVE = f"{MARK}_twint_live"
	DRIVER = "payments.drivers.twint.web_driver.TwintWebDriver"

	def setUp(self):
		frappe.set_user("Administrator")
		ensure_provider(self.LIVE, self.DRIVER, "twint_web", {})
		ensure_provider(self.TEST, self.DRIVER, "twint_web", {})
		frappe.db.set_value("Payment Provider", self.LIVE, "mode", "live")
		# A site that has these in use keeps them; only the test's own records compete here.
		self.others = frappe.get_all(
			"Payment Provider",
			filters={
				"driver_class": ["like", "payments.drivers.twint.%"],
				"enabled": 1,
				"name": ["not like", f"{MARK}%"],
			},
			pluck="name",
		)
		for name in self.others:
			frappe.db.set_value("Payment Provider", name, "enabled", 0, update_modified=False)

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def test_the_live_provider_takes_the_payment_whichever_was_saved_last(self):
		from payments.integrations.twint import api as twint_web

		for newest in (self.TEST, self.LIVE, self.TEST):
			frappe.db.set_value(
				"Payment Provider", newest, "modified", frappe.utils.now_datetime(), update_modified=False
			)
			with self.subTest(saved_last=newest), as_role("Guest"):
				self.assertEqual(twint_web._resolve_twint_provider_name(), self.LIVE)

	def test_a_disabled_merchant_never_takes_a_payment(self):
		from payments.integrations.twint import api as twint_web

		for uuid, enabled in ((f"{MARK}-merchant-on", 1), (f"{MARK}-merchant-off", 0)):
			doc = frappe.new_doc("Twint Bridge Settings")
			doc.update(
				{
					"name": uuid,
					"merchant_uuid": uuid,
					"store_uuid": "s",
					"enabled": enabled,
					"linked_payment_provider": self.LIVE,
				}
			)
			doc.db_insert()
		# the disabled one was saved last
		later = frappe.utils.add_to_date(frappe.utils.now_datetime(), days=1)
		frappe.db.set_value(
			"Twint Bridge Settings", f"{MARK}-merchant-off", "modified", later, update_modified=False
		)
		with as_role("Guest"):
			self.assertEqual(twint_web._resolve_merchant_uuid_for_provider(self.LIVE), f"{MARK}-merchant-on")


# --------------------------------------------------------------------------------- 29: the certificate's date
class TestATwintCertificateDateIsReadOrSaidUnknown(FrappeTestCase):
	UUID = f"{MARK}-cert-merchant"

	@staticmethod
	def a_certificate(password: str, expires: datetime.date) -> bytes:
		from cryptography import x509
		from cryptography.hazmat.primitives import hashes, serialization
		from cryptography.hazmat.primitives.asymmetric import ec
		from cryptography.hazmat.primitives.serialization import pkcs12
		from cryptography.x509.oid import NameOID

		key = ec.generate_private_key(ec.SECP256R1())
		name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "auditc merchant")])
		cert = (
			x509.CertificateBuilder()
			.subject_name(name)
			.issuer_name(name)
			.public_key(key.public_key())
			.serial_number(x509.random_serial_number())
			.not_valid_before(datetime.datetime(2026, 1, 1))
			.not_valid_after(datetime.datetime.combine(expires, datetime.time(12)))
			.sign(key, hashes.SHA256())
		)
		return pkcs12.serialize_key_and_certificates(
			b"auditc", key, cert, None, serialization.BestAvailableEncryption(password.encode())
		)

	def setUp(self):
		from frappe.utils.password import set_encrypted_password

		from payments.drivers.twint import local_engine

		frappe.set_user("Administrator")
		self.expires = datetime.date(2028, 3, 31)
		# A renewal: the new certificate is kept on the instance, the password typed with it did not open it, and
		# the record still holds the date of the certificate it replaced.
		doc = frappe.new_doc("Twint Bridge Settings")
		doc.update(
			{
				"name": self.UUID,
				"merchant_uuid": self.UUID,
				"display_label": "auditc merchant",
				"store_uuid": "store",
				"enabled": 1,
				"certificate_deployed": 1,
				"certificate_expires_on": "2026-12-31",
			}
		)
		doc.db_insert()
		set_encrypted_password("Twint Bridge Settings", self.UUID, "a typo", "p12_password")
		local_engine.store_certificate(self.UUID, self.a_certificate("right", self.expires))

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def save_with_password(self, password):
		with as_role("client_admin"):
			doc = frappe.get_doc("Twint Bridge Settings", self.UUID)
			doc.p12_password = password
			doc.save()
		return frappe.db.get_value("Twint Bridge Settings", self.UUID, "certificate_expires_on")

	def test_a_corrected_password_reads_the_date_again(self):
		self.assertEqual(self.save_with_password("right"), self.expires)

	def test_a_password_that_does_not_open_it_says_the_date_is_unknown(self):
		title = "TWINT certificate expiry not read"
		before = frappe.db.count("Error Log", {"method": title})
		self.assertIsNone(self.save_with_password("still wrong"))
		self.assertEqual(frappe.db.count("Error Log", {"method": title}), before + 1)
