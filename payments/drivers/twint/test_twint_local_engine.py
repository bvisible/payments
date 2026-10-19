# //// Neoffice — added file (no upstream equivalent). The till's TWINT calls on the instance's own engine
# //// (maintenance#1721): the site's switch, the certificate kept encrypted on the instance, the one fetch from the
# //// hub, and the way back to the hub when there is no certificate. TWINT is payments' fake server
# //// (twint_engine/testing), the hub a mock: nothing leaves the process.
# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE

import base64
import os
import shutil
import tempfile
from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils.password import set_encrypted_password

from payments.drivers.twint import local_engine
from payments.drivers.twint.php_bridge_driver import TwintPHPBridgeDriver
from payments.twint_engine import client as engine_client
from payments.twint_engine.testing import cases as cases_module
from payments.twint_engine.testing.fake_twint import FakeTwint
from payments.twint_engine.testing.pki import make_pki

MERCHANT = "bench-merchant-local-engine"
REGISTER = {"amount": 1995, "currency": "CHF", "merchant_reference": "PI-LOCAL-1"}


def _fault_answer():
	from payments.twint_engine.testing.fake_twint import Answer, fault

	return Answer(
		fault("8_7", "InvalidMerchant", "1100", "INVALID_MERCHANT", "Merchant is not valid"), 500
	)


def _hub_answer(message, ok=True, status=200):
	response = mock.Mock(ok=ok, status_code=status)
	response.json.return_value = {"message": message}
	return response


class TestTheSwitch(FrappeTestCase):
	def test_what_the_setting_means(self):
		for setting, expected in (
			(None, True),
			(False, False),
			("off", False),
			("hub", False),
			("", False),
			(True, True),
			("all", True),
			("on", True),
			([MERCHANT], True),
			(["someone-else"], False),
			(f"someone-else, {MERCHANT}", True),
		):
			with self.subTest(setting=setting), mock.patch.dict(
				frappe.conf, {"twint_local_engine": setting}
			):
				self.assertIs(local_engine.runs_locally(MERCHANT), expected)

	def test_absent_is_the_engine(self):
		# The default since 19.10.2026: an updated instance speaks TWINT itself, `false` sends it back to the hub.
		conf = {key: value for key, value in frappe.conf.items() if key != "twint_local_engine"}
		with mock.patch.object(frappe.local, "conf", frappe._dict(conf)):
			self.assertTrue(local_engine.runs_locally(MERCHANT))

	def test_the_service_version(self):
		with mock.patch.dict(frappe.conf, {"twint_soap_version": "10"}):
			self.assertEqual(local_engine.soap_version(), "10")
		conf = {key: value for key, value in frappe.conf.items() if key != "twint_soap_version"}
		with mock.patch.object(frappe.local, "conf", frappe._dict(conf)):
			self.assertEqual(local_engine.soap_version(), "8.7")


def _forget_certificate():
	frappe.db.delete(
		"__Auth",
		{"doctype": local_engine.DOCTYPE, "name": MERCHANT, "fieldname": local_engine.CERTIFICATE_FIELD},
	)


def _set_password(password):
	set_encrypted_password(local_engine.DOCTYPE, MERCHANT, password, "p12_password")


class TestTheCertificateOnTheInstance(FrappeTestCase):
	def tearDown(self):
		_forget_certificate()

	def test_kept_encrypted_and_read_back(self):
		raw = os.urandom(3398)
		self.assertTrue(local_engine.store_certificate(MERCHANT, raw))
		self.assertEqual(local_engine.stored_certificate(MERCHANT), raw)
		# __Auth has no `modified`: read it in SQL, get_value would order by it
		stored = frappe.db.sql(
			"select `password` from `__Auth` where doctype=%s and name=%s and fieldname=%s",
			(local_engine.DOCTYPE, MERCHANT, local_engine.CERTIFICATE_FIELD),
		)[0][0]
		self.assertNotIn(
			base64.b64encode(raw).decode()[:40], stored
		)  # encrypted, not the base64 of the file

	def test_a_too_large_certificate_also_forgets_the_older_copy(self):
		self.assertTrue(local_engine.store_certificate(MERCHANT, b"\x30" * 64))
		self.assertFalse(
			local_engine.store_certificate(MERCHANT, b"\x30" * (local_engine.MAX_LOCAL_CERTIFICATE + 1))
		)
		self.assertIsNone(
			local_engine.stored_certificate(MERCHANT), "an older copy would stand for the wrong certificate"
		)

	def test_too_large_is_not_kept(self):
		self.assertFalse(
			local_engine.store_certificate(MERCHANT, b"\x30" * (local_engine.MAX_LOCAL_CERTIFICATE + 1))
		)
		self.assertIsNone(local_engine.stored_certificate(MERCHANT))


class TestTheDriverOnTheEngine(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.folder = tempfile.mkdtemp(prefix="twint-local-engine-")
		cls.pki = make_pki(os.path.join(cls.folder, "pki"))
		cls.fake = FakeTwint(cls.pki).start()
		if not frappe.db.exists(local_engine.DOCTYPE, MERCHANT):
			frappe.get_doc(
				{
					"doctype": local_engine.DOCTYPE,
					"merchant_uuid": MERCHANT,
					"display_label": "Engine test merchant",
					"enabled": 1,
					"store_uuid": cases_module.STORE,
					"environment": "production",
					"p12_password": cls.pki.password,
				}
			).insert(ignore_permissions=True)

	@classmethod
	def tearDownClass(cls):
		cls.fake.stop()
		shutil.rmtree(cls.folder, ignore_errors=True)
		frappe.delete_doc(local_engine.DOCTYPE, MERCHANT, ignore_permissions=True, force=True)
		super().tearDownClass()

	def setUp(self):
		_forget_certificate()
		_set_password(self.pki.password)
		frappe.cache.delete_value(local_engine._refused_key(MERCHANT))
		engine_client.forget_enrollments()
		self.fake.reset()
		provider = mock.Mock(service_url="https://hub.example", default_merchant_uuid=MERCHANT)
		provider._auth_headers.return_value = {"Authorization": "token k:s"}
		self.driver = object.__new__(TwintPHPBridgeDriver)
		self.driver.provider = provider
		self.driver.settings_doc = None
		self.patches = [
			mock.patch.object(
				TwintPHPBridgeDriver, "_twint_provider", new=property(lambda _self: provider)
			),
			mock.patch.object(
				local_engine,
				"_client_options",
				return_value={"service_host": self.fake.host, "ca_file": self.pki.ca_pem},
			),
		]
		for patch in self.patches:
			patch.start()

	def tearDown(self):
		for patch in self.patches:
			patch.stop()
		_forget_certificate()
		_set_password(self.pki.password)

	def call(self, setting, hub=None):
		import requests

		with (
			mock.patch.dict(frappe.conf, {"twint_local_engine": setting}),
			mock.patch.object(
				requests, "post", side_effect=hub or AssertionError("the hub must not be called")
			) as post,
		):
			answer = self.driver._call_bridge("register_payment", MERCHANT, REGISTER)
		return answer, post

	def test_on_with_the_certificate_here_twint_is_reached_without_the_hub(self):
		local_engine.store_certificate(MERCHANT, self.pki.merchant_p12)
		answer, post = self.call(True)
		self.assertTrue(answer["success"], answer)
		self.assertEqual(answer["merchant_reference"], "PI-LOCAL-1")
		self.assertEqual(
			[request["operation"] for request in self.fake.requests], ["EnrollCashRegister", "StartOrder"]
		)
		post.assert_not_called()

	def test_off_goes_to_the_hub(self):
		local_engine.store_certificate(MERCHANT, self.pki.merchant_p12)
		answer, post = self.call(False, hub=[_hub_answer({"success": True, "order_id": "from-the-hub"})])
		self.assertEqual(answer["order_id"], "from-the-hub")
		self.assertTrue(post.call_args.args[0].endswith("neoffice_devops.api.twint.execute"))
		self.assertEqual(self.fake.requests, [])

	def test_a_merchant_not_listed_goes_to_the_hub(self):
		local_engine.store_certificate(MERCHANT, self.pki.merchant_p12)
		answer, _post = self.call(
			["another-merchant"], hub=[_hub_answer({"success": True, "order_id": "from-the-hub"})]
		)
		self.assertEqual(answer["order_id"], "from-the-hub")

	def test_the_certificate_is_fetched_once_from_the_hub_with_the_password(self):
		fetched = _hub_answer(
			{"success": True, "content_base64": base64.b64encode(self.pki.merchant_p12).decode()}
		)
		answer, post = self.call(True, hub=[fetched])
		self.assertTrue(answer["success"], answer)
		url = post.call_args.args[0]
		self.assertTrue(url.endswith(local_engine.FETCH_METHOD))
		self.assertEqual(post.call_args.kwargs["json"]["current_password"], self.pki.password)
		self.assertNotIn(self.pki.password, url)
		self.assertEqual(local_engine.stored_certificate(MERCHANT), self.pki.merchant_p12)
		answer, post = self.call(True)  # kept: no second fetch
		self.assertTrue(answer["success"], answer)

	def test_no_certificate_anywhere_goes_to_the_hub_as_before(self):
		refused = _hub_answer({"success": False, "error": "A certificate is already stored…"})
		bridged = _hub_answer({"success": True, "order_id": "from-the-hub"})
		answer, post = self.call(True, hub=[refused, bridged])
		self.assertEqual(answer["order_id"], "from-the-hub")
		self.assertEqual(
			[call.args[0].rsplit("/", 1)[-1] for call in post.call_args_list],
			[local_engine.FETCH_METHOD, "neoffice_devops.api.twint.execute"],
		)
		self.assertIsNone(local_engine.stored_certificate(MERCHANT))
		answer, post = self.call(True, hub=[_hub_answer({"success": True, "order_id": "again"})])
		self.assertEqual(answer["order_id"], "again")
		self.assertEqual(
			len(post.call_args_list), 1, "no second fetch for ten minutes: straight to the hub"
		)

	def test_a_copy_here_that_no_longer_opens_is_replaced_by_the_hubs(self):
		# Renewed by hand on the hub, or on another site of the same merchant: the copy kept here is stale.
		local_engine.store_certificate(MERCHANT, b"\x30\x03\x02\x01\x00")
		fetched = _hub_answer(
			{"success": True, "content_base64": base64.b64encode(self.pki.merchant_p12).decode()}
		)
		answer, post = self.call(True, hub=[fetched])
		self.assertTrue(answer["success"], answer)
		self.assertTrue(post.call_args.args[0].endswith(local_engine.FETCH_METHOD))
		self.assertEqual(local_engine.stored_certificate(MERCHANT), self.pki.merchant_p12)
		self.assertEqual(
			[request["operation"] for request in self.fake.requests], ["EnrollCashRegister", "StartOrder"]
		)

	def test_a_stale_copy_and_no_hub_copy_go_to_the_hubs_bridge(self):
		local_engine.store_certificate(MERCHANT, b"\x30\x03\x02\x01\x00")
		refused = _hub_answer(
			{"success": False, "error": "Send the password that opens the certificate…"}
		)
		bridged = _hub_answer({"success": True, "order_id": "from-the-hub"})
		answer, post = self.call(True, hub=[refused, bridged])
		self.assertEqual(answer["order_id"], "from-the-hub")
		self.assertEqual(self.fake.requests, [], "nothing reached TWINT from here")

	def test_a_hub_that_does_not_know_the_fetch_yet_goes_to_the_hub(self):
		missing = mock.Mock(ok=False, status_code=404)
		missing.json.return_value = {"exc_type": "DoesNotExistError"}
		bridged = _hub_answer({"success": True, "order_id": "from-the-hub"})
		answer, _post = self.call(True, hub=[missing, bridged])
		self.assertEqual(answer["order_id"], "from-the-hub")

	def test_a_wrong_password_asks_the_hub_then_its_bridge_says_no(self):
		# The copy here does not open: the hub's copy is asked for (it refuses the same wrong password), then the
		# hub's bridge answers as it always did. Nothing reached TWINT from here.
		local_engine.store_certificate(MERCHANT, self.pki.merchant_p12)
		_set_password("wrong")
		refused = _hub_answer(
			{"success": False, "error": "Send the password that opens the certificate…"}
		)
		bridged = _hub_answer({"success": False, "error": "Reading PKCS12 file failed"})
		answer, post = self.call(True, hub=[refused, bridged])
		self.assertEqual(answer["error"], "Reading PKCS12 file failed")
		self.assertEqual(
			[call.args[0].rsplit("/", 1)[-1] for call in post.call_args_list],
			[local_engine.FETCH_METHOD, "neoffice_devops.api.twint.execute"],
		)
		self.assertEqual(self.fake.requests, [])
		self.assertEqual(
			local_engine.stored_certificate(MERCHANT), self.pki.merchant_p12, "a refusal replaces nothing"
		)

	def test_a_refusal_of_twint_is_the_engines_answer_not_a_reason_to_go_to_the_hub(self):
		local_engine.store_certificate(MERCHANT, self.pki.merchant_p12)
		self.fake.force("StartOrder", _fault_answer())
		answer, post = self.call(True)
		self.assertEqual(answer["exception"], "Twint\\Sdk\\Exception\\ApiFailure")
		self.assertEqual(answer["error"], "Merchant is not valid")
		post.assert_not_called()
