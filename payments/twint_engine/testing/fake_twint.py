# //// Neoffice — added file (no upstream equivalent). A fake TWINT Merchant service for the TWINT engine's tests and
# //// for the bench that runs the engine and the official PHP SDK on the same answers (maintenance#1721). It serves
# //// the answers the SDK's own hermetic test lane serves (twint_sdk/<version>/stubs.json, recorded from TWINT's API
# //// and rendered the way WireMock renders them), plus the cases the SDK does not record (confirmation pending,
# //// SOAP faults, answers that are not XML), behind TLS that asks for the merchant's client certificate.
# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""A fake TWINT Merchant service on 127.0.0.1, for tests only."""

from __future__ import annotations

import datetime
import json
import os
import random
import re
import ssl
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from lxml import etree

HERE = os.path.dirname(__file__)
SDK_DIR = os.path.join(HERE, "twint_sdk")
VERSIONS = {
	"8_7": "v8_7",
	"10": "v10",
}  # the URL's suffix (TWINTMerchantServiceV8_7) → the fixtures' folder

SOAP_ENV = "http://schemas.xmlsoap.org/soap/envelope/"


def namespaces(version: str) -> dict:
	return {
		"header": f"http://service.twint.ch/header/types/v{version}",
		"merchant": f"http://service.twint.ch/merchant/types/v{version}",
		"base": f"http://service.twint.ch/base/types/v{version}",
		"fault": f"http://service.twint.ch/fault/types/v{version}",
	}


# ----------------------------------------------------------------------------------------------------------------------
# WireMock's response templates, the part of them the SDK's recorded answers use
# ----------------------------------------------------------------------------------------------------------------------

_ASSIGN = re.compile(r"\{\{#assign '([^']+)'\}\}(.*?)\{\{/assign\}\}", re.S)
_EXPRESSION = re.compile(r"\{\{(.*?)\}\}", re.S)
_STEP = re.compile(r"(?<=/)([A-Za-z_][\w.-]*)(?=/|\[|$)")


def _namespace_free(expression: str) -> str:
	"""WireMock's xPath helper ignores namespaces: `//A/B/text()[1]` names elements by their local name."""
	return _STEP.sub(lambda match: f"*[local-name()='{match.group(1)}']", expression)


class Renderer:
	"""Renders one answer of the SDK's fixtures for one request, with values drawn from a seeded generator."""

	def __init__(self, request_body: bytes, rng: random.Random, now: datetime.datetime):
		self.rng = rng
		self.now = now
		self.variables: dict[str, str] = {}
		try:
			self.document = etree.fromstring(
				request_body, parser=etree.XMLParser(resolve_entities=False, no_network=True)
			)
		except etree.XMLSyntaxError:
			self.document = None

	def render(self, template: str) -> str:
		def assign(match):
			self.variables[match.group(1)] = self.render(match.group(2)).strip()
			return ""

		template = _ASSIGN.sub(assign, template)
		return _EXPRESSION.sub(lambda match: self.expression(match.group(1).strip()), template)

	def expression(self, text: str) -> str:
		if text.startswith("randomValue"):
			return str(uuid.UUID(int=self.rng.getrandbits(128), version=4))
		if text.startswith("randomInt"):
			lower = re.search(r"lower=(-?\d+)", text)
			return str(self.rng.randint(int(lower.group(1)) if lower else -(2**31), 2**31 - 1))
		if text == "now":
			return self.now.strftime("%Y-%m-%dT%H:%M:%SZ")
		if text.startswith("xPath request.body"):
			path = re.search(r"'(.*)'", text).group(1)
			if self.document is None:
				return ""
			found = self.document.xpath(_namespace_free(path))
			return str(found[0]) if found else ""
		if text.startswith("defaultIfEmpty"):
			_, first, second = text.split()
			return self.variables.get(first) or self.variables.get(second, "")
		raise ValueError(f"template helper not supported by the fake server: {text}")


# ----------------------------------------------------------------------------------------------------------------------
# Answers the SDK does not record
# ----------------------------------------------------------------------------------------------------------------------


def fault(version: str, element: str, code: str, status: str, faultstring: str) -> str:
	"""A SOAP 1.1 fault the way the WSDL declares them: one element of the fault types, holding its ErrorCode."""
	ns = namespaces(version)["fault"]
	return (
		f'<soapenv:Envelope xmlns:soapenv="{SOAP_ENV}"><soapenv:Body><soapenv:Fault>'
		f"<faultcode>soapenv:Server</faultcode><faultstring>{faultstring}</faultstring>"
		f'<detail><ns2:{element} xmlns:ns2="{ns}"><ns2:ErrorCode><ns2:Code>{code}</ns2:Code>'
		f"<ns2:Status>{status}</ns2:Status></ns2:ErrorCode></ns2:{element}></detail>"
		"</soapenv:Fault></soapenv:Body></soapenv:Envelope>"
	)


def system_status(version: str, status: str = "OK") -> str:
	ns = namespaces(version)
	return (
		f'<soapenv:Envelope xmlns:soapenv="{SOAP_ENV}" xmlns:h="{ns["header"]}" xmlns:m="{ns["merchant"]}">'
		f"<soapenv:Header><h:ResponseHeaderElement><h:MessageId>{{{{randomValue type='UUID'}}}}</h:MessageId>"
		f"</h:ResponseHeaderElement></soapenv:Header><soapenv:Body><m:CheckSystemStatusResponseElement>"
		f"<m:Status>{status}</m:Status></m:CheckSystemStatusResponseElement></soapenv:Body></soapenv:Envelope>"
	)


class Answer:
	"""One answer forced on the next call of an operation: a status, a body (a template), a content type."""

	def __init__(
		self,
		body: str,
		status: int = 200,
		content_type: str = "text/xml;charset=UTF-8",
		close: bool = False,
	):
		self.body = body
		self.status = status
		self.content_type = content_type
		self.close = close  # drop the connection without answering


# ----------------------------------------------------------------------------------------------------------------------
# The server
# ----------------------------------------------------------------------------------------------------------------------


class FakeTwint:
	"""TWINT's Merchant service on 127.0.0.1: the SDK's recorded answers, its scenarios, and forced answers.

	`requests` keeps every call (path, SOAPAction, content type, user agent, body, client certificate subject).
	`state[scenario]` is WireMock's scenario state ("Started" until a test sets it). `force(operation, answer)` makes
	the next call of an operation get that answer instead. Values drawn by the templates come from a generator
	seeded by (seed, operation, how many times that operation was called), so two engines that make the same calls
	get the same answers whatever else they call.
	"""

	def __init__(self, pki, seed: int = 1721, now: datetime.datetime | None = None):
		self.pki = pki
		self.seed = seed
		self.now = now or datetime.datetime(2026, 10, 19, 12, 0, 0, tzinfo=datetime.timezone.utc)
		self.stubs = {}
		for suffix, folder in VERSIONS.items():
			with open(os.path.join(SDK_DIR, folder, "stubs.json"), encoding="utf-8") as handle:
				mappings = json.load(handle)["mappings"]
			by_name = {}
			for (
				mapping
			) in (
				mappings
			):  # the fixtures carry two copies of most answers: the later one wins, as in WireMock
				by_name[(mapping["operation"], mapping.get("name"))] = mapping
			self.stubs[suffix] = list(by_name.values())
		self.state: dict[str, str] = {}
		self.forced: dict[str, list[Answer]] = {}
		self.requests: list[dict] = []
		self.counters: dict[str, int] = {}
		self.lock = threading.Lock()
		self.server = None
		self.thread = None

	# -- control ---------------------------------------------------------------------------------------------------

	def reset(self):
		with self.lock:
			self.state.clear()
			self.forced.clear()
			self.requests.clear()
			self.counters.clear()

	def set_state(self, scenario: str, state: str):
		self.state[scenario] = state

	def force(self, operation: str, answer: Answer):
		self.forced.setdefault(operation, []).append(answer)

	def url(self, version: str = "8_7") -> str:
		return f"https://127.0.0.1:{self.port}/merchant/service/TWINTMerchantServiceV{version}"

	@property
	def host(self) -> str:
		return f"127.0.0.1:{self.port}"

	def start(self) -> FakeTwint:
		fake = self

		class Handler(BaseHTTPRequestHandler):
			protocol_version = "HTTP/1.1"

			def log_message(self, *args):  # quiet
				pass

			def do_POST(self):
				length = int(self.headers.get("Content-Length") or 0)
				body = self.rfile.read(length)
				peer = self.connection.getpeercert() or {}
				subject = dict(item[0] for item in peer.get("subject", ()))
				answer = fake.answer(
					self.path,
					(self.headers.get("SOAPAction") or "").strip().strip('"'),
					body,
					{
						"content_type": self.headers.get("Content-Type"),
						"user_agent": self.headers.get("User-Agent"),
						"soap_action": self.headers.get("SOAPAction"),
						"header_names": sorted(name.lower() for name in self.headers.keys()),
						"client": subject.get("commonName"),
					},
				)
				if answer.close:
					self.close_connection = True
					self.connection.shutdown(2)
					return
				payload = answer.body.encode()
				self.send_response(answer.status)
				self.send_header("Content-Type", answer.content_type)
				self.send_header("Content-Length", str(len(payload)))
				self.end_headers()
				self.wfile.write(payload)

		context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
		context.load_cert_chain(self.pki.server_cert, self.pki.server_key)
		context.load_verify_locations(self.pki.ca_pem)
		context.verify_mode = (
			ssl.CERT_REQUIRED
		)  # TWINT authenticates the merchant by its certificate, nothing else

		class Server(ThreadingHTTPServer):
			daemon_threads = True

			def handle_error(
				self, request, client_address
			):  # a refused handshake is expected in some tests
				pass

		self.server = Server(("127.0.0.1", 0), Handler)
		self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
		self.port = self.server.server_address[1]
		self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
		self.thread.start()
		return self

	def stop(self):
		if self.server:
			self.server.shutdown()
			self.server.server_close()
			self.server = None

	# -- answering -------------------------------------------------------------------------------------------------

	def answer(self, path: str, operation: str, body: bytes, meta: dict) -> Answer:
		version = path.rsplit("TWINTMerchantServiceV", 1)[-1]
		with self.lock:
			record = {"path": path, "operation": operation, "body": body, **meta}
			self.requests.append(record)
			count = self.counters.get(operation, 0)
			self.counters[operation] = count + 1
			rng = random.Random(f"{self.seed}:{operation}:{count}")
			forced = self.forced.get(operation)
			if forced:
				chosen = forced.pop(0)
				answer = Answer(
					Renderer(body, rng, self.now).render(chosen.body),
					chosen.status,
					chosen.content_type,
					chosen.close,
				)
			else:
				stub = self.match(version, operation, body)
				if stub is None:
					answer = Answer(
						"No response could be served as there are no stub mappings", 404, "text/plain"
					)
				else:
					if stub.get("newScenarioState"):
						self.state[stub["scenarioName"]] = stub["newScenarioState"]
					answer = Answer(Renderer(body, rng, self.now).render(stub["body"]), stub.get("status", 200))
			record["answer_status"] = answer.status
			record["answer"] = answer.body
			return answer

	def match(self, version: str, operation: str, body: bytes):
		text = body.decode(errors="replace")
		best = None
		for stub in self.stubs.get(version, []):
			if stub["operation"] != operation or not self._body_matches(
				stub.get("bodyPatterns") or [], text
			):
				continue
			scenario = stub.get("scenarioName")
			if (
				scenario
				and stub.get("requiredScenarioState")
				and self.state.get(scenario, "Started") != stub["requiredScenarioState"]
			):
				continue
			# WireMock: the lowest priority number wins, then the stub added last.
			if best is None or stub.get("priority", 5) <= best.get("priority", 5):
				best = stub
		return best

	@staticmethod
	def _body_matches(patterns: list, text: str) -> bool:
		for pattern in patterns:
			if "contains" in pattern and pattern["contains"] not in text:
				return False
			if "doesNotContain" in pattern and pattern["doesNotContain"] in text:
				return False
			if "not" in pattern and "contains" in pattern["not"] and pattern["not"]["contains"] in text:
				return False
		return True


def body_of(request: dict) -> etree._Element:
	return etree.fromstring(
		request["body"], parser=etree.XMLParser(resolve_entities=False, no_network=True)
	)
