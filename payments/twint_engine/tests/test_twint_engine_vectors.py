# //// Neoffice — added file (no upstream equivalent). The TWINT engine held to the hub's PHP bridge in CI
# //// (maintenance#1721): every case of testing/cases.py runs on the fake TWINT (the SDK's recorded answers), and
# //// the engine must answer what the bridge answered and send what it sent, byte for byte but the message id
# //// (vectors-<version>.json, recorded from the real bridge by neoffice-devops
# //// scripts/twint_engine_bench/make_vectors.py). Plus what the bridge cannot show: the schemas, the deliberate
# //// difference, the enrolment kept for the process.
# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE

import json
import os
import re
import shutil
import tempfile
import unittest

import payments.twint_engine as engine
from payments.twint_engine import client as engine_client
from payments.twint_engine.testing import cases as cases_module
from payments.twint_engine.testing import schemas
from payments.twint_engine.testing.fake_twint import FakeTwint, body_of
from payments.twint_engine.testing.pki import make_pki

HERE = os.path.dirname(os.path.dirname(__file__))
MESSAGE_ID = re.compile(rb"<MessageId>[^<]*</MessageId>")


class TwintEngineAgainstTheBridge(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.folder = tempfile.mkdtemp(prefix="twint-engine-test-")
		cls.pki = make_pki(os.path.join(cls.folder, "pki"))
		cls.fake = FakeTwint(cls.pki).start()

	@classmethod
	def tearDownClass(cls):
		cls.fake.stop()
		shutil.rmtree(cls.folder, ignore_errors=True)

	def config(self, case, version):
		config = {
			"certificate_password": self.pki.password,
			"store_uuid": cases_module.STORE,
			"cash_register_id": None,
			"environment": "production",
			"soap_version": version,
		}
		config.update(case.config)
		config["certificate"] = {
			"merchant": self.pki.merchant_p12,
			"legacy": self.pki.merchant_p12_legacy,
			"stranger": self.pki.stranger_p12,
			"none": b"",
		}[case.certificate]
		return config

	def run_case(self, case, version):
		suffix = version.replace(".", "_")
		cases_module.apply(case, self.fake, suffix)
		engine_client.forget_enrollments()
		answer = engine.execute(
			case.command,
			self.config(case, version),
			case.params,
			service_host=self.fake.host,
			ca_file=self.pki.ca_pem,
		)
		requests = [
			{
				"operation": request["operation"],
				"path": request["path"],
				"soap_action": request["soap_action"],
				"content_type": request["content_type"],
				"user_agent": request["user_agent"],
				"header_names": request["header_names"],
				"body": MESSAGE_ID.sub(b"<MessageId>*</MessageId>", request["body"]).decode(),
			}
			for request in self.fake.requests
		]
		return answer, requests

	def replay(self, version):
		suffix = version.replace(".", "_")
		with open(os.path.join(HERE, "testing", f"vectors-v{suffix}.json"), encoding="utf-8") as handle:
			vectors = {vector["label"]: vector for vector in json.load(handle)["vectors"]}
		cases = cases_module.cases(suffix)
		self.assertEqual(
			sorted(vectors), sorted(case.label for case in cases), "vectors and cases differ: re-record"
		)
		for case in cases:
			vector = vectors[case.label]
			with self.subTest(case=case.label):
				answer, requests = self.run_case(case, version)
				for request in self.fake.requests:
					self.assertEqual(schemas.problems(request["body"], suffix), [], request["operation"])
				if case.compare == cases_module.DIFFERENCE:
					continue
				self.assertEqual(requests, vector["php_requests"])
				if case.compare == cases_module.FAILURE:
					self.assertIs(answer.get("success"), False)
					self.assertIs(vector["php_answer"].get("success"), False)
				else:
					self.assertEqual(answer, vector["php_answer"])

	def test_soap_8_7_like_the_hubs_bridge(self):
		self.replay("8.7")

	def test_soap_10_like_the_sdk_2(self):
		self.replay("10")

	def test_a_named_cash_register_is_sent_as_the_sdk_means_it(self):
		case = next(
			case for case in cases_module.cases() if case.label == "register: cash register named"
		)
		answer, _requests = self.run_case(case, "8.7")
		self.assertTrue(answer["success"])
		for request in self.fake.requests:
			register = body_of(request).xpath("//*[local-name()='CashRegisterId']/text()")
			self.assertEqual(register, [f"till-1-{cases_module.STORE}"])

	def test_the_cash_register_is_enrolled_once_per_process(self):
		case = cases_module.cases()[0]
		cases_module.apply(case, self.fake, "8_7")
		engine_client.forget_enrollments()
		for _ in range(3):
			answer = engine.execute(
				"register_payment",
				self.config(case, "8.7"),
				{"amount": 100, "merchant_reference": "X"},
				service_host=self.fake.host,
				ca_file=self.pki.ca_pem,
			)
			self.assertTrue(answer["success"])
		self.assertEqual(
			[request["operation"] for request in self.fake.requests],
			["EnrollCashRegister", "StartOrder", "StartOrder", "StartOrder"],
		)

	def test_a_server_whose_certificate_the_system_does_not_trust_is_refused(self):
		case = cases_module.cases()[0]
		cases_module.apply(case, self.fake, "8_7")
		engine_client.forget_enrollments()
		answer = engine.execute(
			"register_payment",
			self.config(case, "8.7"),
			{"amount": 100, "merchant_reference": "X"},
			service_host=self.fake.host,  # no ca_file: the system's authorities only
		)
		self.assertIs(answer["success"], False)
		self.assertIn("TLS", answer["error"])
		self.assertEqual(self.fake.requests, [])

	def test_an_unknown_service_version_is_refused_before_anything_is_sent(self):
		case = cases_module.cases()[0]
		cases_module.apply(case, self.fake, "8_7")
		config = self.config(case, "8.7")
		config["soap_version"] = "9.9"
		answer = engine.execute(
			"register_payment",
			config,
			{"amount": 100, "merchant_reference": "X"},
			service_host=self.fake.host,
		)
		self.assertIs(answer["success"], False)
		self.assertEqual(self.fake.requests, [])
