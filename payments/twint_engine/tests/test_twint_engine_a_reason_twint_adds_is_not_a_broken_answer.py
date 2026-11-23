# //// Neoffice — added file (no upstream equivalent). The engine reads a reason TWINT adds (ISSUER_ABORT, the customer's bank refused) instead of
# //// refusing the answer, which left an order that had already failed open for ever (maintenance#3259).
# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""The engine against a fake TWINT that answers a monitor with a reason the SDK did not know.

The parity suite (test_twint_engine_vectors) holds the engine to what the old PHP bridge answered on the SDK's own scenarios. The bridge refused an unknown reason
too, and TWINT's contract (`Reason` is a free token with a code) never said it should: a customer's bank that refuses is a failed order, and the engine must say so.
"""

import os
import shutil
import tempfile
import unittest

import payments.twint_engine as engine
from payments.twint_engine import client as engine_client
from payments.twint_engine.testing import cases as cases_module
from payments.twint_engine.testing.fake_twint import FakeTwint
from payments.twint_engine.testing.pki import make_pki

TIMEOUT_STEP = "MonitorOrder_FailureScenario (Client Timeout Step)"
PAID_STEP = "MonitorOrder_SuccessScenario (OK Step)"


class TestAReasonTwintAddsIsNotABrokenAnswer(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.folder = tempfile.mkdtemp(prefix="twint-engine-reason-")
		cls.pki = make_pki(os.path.join(cls.folder, "pki"))
		cls.fake = FakeTwint(cls.pki).start()

	@classmethod
	def tearDownClass(cls):
		cls.fake.stop()
		shutil.rmtree(cls.folder, ignore_errors=True)

	def monitor(self, version: str, stub: str, old: str, new: str) -> dict:
		suffix = version.replace(".", "_")
		self.fake.reset()
		self.fake.force(
			"MonitorOrder", cases_module.answer_for(self.fake, suffix, "edit", (stub, [(old, new)]))
		)
		engine_client.forget_enrollments()
		config = {
			"certificate_password": self.pki.password,
			"store_uuid": cases_module.STORE,
			"cash_register_id": None,
			"environment": "production",
			"soap_version": version,
			"certificate": self.pki.merchant_p12,
		}
		return engine.execute(
			"monitor_status",
			config,
			{"order_id": cases_module.ORDER},
			service_host=self.fake.host,
			ca_file=self.pki.ca_pem,
		)

	def test_a_refusal_of_the_customers_bank_is_a_failed_order_not_a_broken_answer(self):
		for version in ("8.7", "10"):
			with self.subTest(version=version):
				answer = self.monitor(version, TIMEOUT_STEP, "CLIENT_TIMEOUT", "ISSUER_ABORT")

				self.assertIs(answer.get("success"), True, answer)
				self.assertEqual(answer["order_status"], "FAILURE")
				self.assertEqual(answer["transaction_status"], "ISSUER_ABORT")

	def test_any_other_reason_twint_adds_is_passed_on_as_it_is(self):
		for version in ("8.7", "10"):
			with self.subTest(version=version):
				answer = self.monitor(version, TIMEOUT_STEP, "CLIENT_TIMEOUT", "A_REASON_OF_NEXT_YEAR")

				self.assertIs(answer.get("success"), True, answer)
				self.assertEqual(answer["order_status"], "FAILURE")
				self.assertEqual(answer["transaction_status"], "A_REASON_OF_NEXT_YEAR")

	def test_a_paid_order_still_reads_as_paid(self):
		for version in ("8.7", "10"):
			with self.subTest(version=version):
				answer = self.monitor(version, PAID_STEP, "ORDER_OK", "ORDER_OK")

				self.assertIs(answer.get("success"), True, answer)
				self.assertEqual(answer["order_status"], "SUCCESS")
				self.assertEqual(answer["transaction_status"], "ORDER_OK")

	def test_the_reason_helper_refuses_only_an_answer_with_no_reason_at_all(self):
		from payments.twint_engine import values

		self.assertEqual(values.reason("ISSUER_ABORT"), "ISSUER_ABORT")
		self.assertEqual(values.reason(" SOMETHING_NEW "), "SOMETHING_NEW")
		self.assertEqual(values.reason(""), "")
		with self.assertRaises(values.SdkError):
			values.reason(None)

	def test_issuer_abort_is_a_reason_the_engine_knows(self):
		from payments.twint_engine import values

		self.assertIn("ISSUER_ABORT", values.TRANSACTION_STATUSES)
