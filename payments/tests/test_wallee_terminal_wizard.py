# //// Neoffice — added file (no upstream equivalent). maintenance#1730: the Wallee steps of the terminal wizard
# //// imported SDK services that never existed and sent a terminal request Wallee does not read; these tests hold
# //// them to the installed SDK, with its real models and its real signed request.
"""The Wallee steps of the terminal wizard against the wallee SDK actually installed."""

import ast
import base64
import importlib
import inspect
import json
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from wallee.models import (
	PaymentTerminal,
	PaymentTerminalAddress,
	PaymentTerminalConfiguration,
	PaymentTerminalConfigurationVersion,
	PaymentTerminalConfigurationVersionState,
	PaymentTerminalLocation,
	PaymentTerminalLocationVersion,
	PaymentTerminalLocationVersionState,
	TerminalListResponse,
)

from payments.integrations.wallee import api as wallee_api

PREFIX = "_T1730"
LOCATION_ID = 917300001
CONFIGURATION_ID = 917300002


class _Provider:
	space_id = 1
	provider_doc = frappe._dict(mode="test")

	def build_configuration(self):
		from wallee.configuration import Configuration

		return Configuration(
			user_id=1,
			authentication_key=base64.b64encode(b"k" * 32).decode(),
			host="https://wallee.invalid/api/v2.0",
		)


class _Refusal:
	"""What Wallee answers when it refuses a terminal (status 422, its sentence in `message`)."""

	status = 422
	reason = "Unprocessable Entity"
	data = json.dumps(
		{"code": "parameter_invalid", "message": "Please get in contact with us."}
	).encode()

	def read(self):
		return self.data

	def getheader(self, name, default=None):
		return "application/json" if name.lower() == "content-type" else default

	def getheaders(self):
		return {"content-type": "application/json"}


def _terminal(terminal_id, location_version, configuration_version, location_state="ACTIVE"):
	return PaymentTerminal(
		id=terminal_id,
		location_version=PaymentTerminalLocationVersion(
			id=location_version,
			state=PaymentTerminalLocationVersionState(location_state),
			location=PaymentTerminalLocation(id=LOCATION_ID, name=f"{PREFIX} Shop"),
			address=PaymentTerminalAddress(
				street="Rue du Test 1", postcode="1950", city="Sion", country="CH"
			),
		),
		configuration_version=PaymentTerminalConfigurationVersion(
			id=configuration_version,
			state=PaymentTerminalConfigurationVersionState.ACTIVE,
			configuration=PaymentTerminalConfiguration(id=CONFIGURATION_ID, name=f"{PREFIX} Config"),
		),
	)


def _service_with(terminals):
	class _Service:
		def __init__(self, configuration):
			pass

		def get_payment_terminals(self, space, after=None, limit=None, expand=None, **kwargs):
			_Service.expand = expand
			return TerminalListResponse(data=list(terminals), has_more=False, limit=limit)

	return _Service


class _Stop(Exception):
	pass


class TestWalleeTerminalWizard(FrappeTestCase):
	def setUp(self):
		self.commit = patch.object(frappe.db, "commit")
		self.commit.start()
		self.provider = patch.object(wallee_api, "_provider", return_value=_Provider())
		self.provider.start()

	def tearDown(self):
		self.provider.stop()
		self.commit.stop()
		for doctype, field in (
			("Wallee Location", "location_name"),
			("Wallee Terminal Configuration", "configuration_name"),
		):
			for name in frappe.get_all(doctype, filters={field: ["like", f"{PREFIX}%"]}, pluck="name"):
				frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)
		frappe.db.rollback()

	def _sync(self, terminals):
		service = _service_with(terminals)
		with patch("wallee.service.payment_terminals_service.PaymentTerminalsService", service):
			locations = wallee_api.sync_locations_from_wallee("wallee_test")
			configurations = wallee_api.sync_configurations_from_wallee("wallee_test")
		return locations, configurations, service

	def test_every_wallee_import_of_the_wizard_resolves(self):
		tree = ast.parse(inspect.getsource(wallee_api))
		imports = [
			node
			for node in ast.walk(tree)
			if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("wallee")
		]
		self.assertTrue(imports)
		for node in imports:
			module = importlib.import_module(node.module)
			for alias in node.names:
				with self.subTest(name=f"{node.module}.{alias.name}"):
					self.assertTrue(hasattr(module, alias.name))

	def test_the_sync_reads_locations_and_configurations_from_the_terminals(self):
		# Two terminals on the same location: a newer version still pending, and the active one.
		terminals = [_terminal(1, 44, 51, location_state="PENDING"), _terminal(2, 42, 52)]
		locations, configurations, service = self._sync(terminals)

		self.assertEqual((locations["created"], locations["total"]), (1, 1))
		self.assertEqual((configurations["created"], configurations["total"]), (1, 1))
		self.assertNotIn("message", locations)
		self.assertIn("locationVersion.location", service.expand)

		location = frappe.get_doc(
			"Wallee Location",
			frappe.db.get_value("Wallee Location", {"wallee_location_id": str(LOCATION_ID)}),
		)
		self.assertEqual(location.location_name, f"{PREFIX} Shop")
		self.assertEqual(location.wallee_location_version_id, "42")
		self.assertEqual(
			(location.city, location.postal_code, location.street), ("Sion", "1950", "Rue du Test 1")
		)
		self.assertEqual(location.country, frappe.db.get_value("Country", {"code": "ch"}, "name"))
		configuration = frappe.get_doc(
			"Wallee Terminal Configuration",
			frappe.db.get_value(
				"Wallee Terminal Configuration", {"wallee_configuration_id": str(CONFIGURATION_ID)}
			),
		)
		self.assertEqual(configuration.configuration_name, f"{PREFIX} Config")
		self.assertEqual(configuration.wallee_configuration_version_id, "52")

		# A second sync updates, it does not duplicate.
		locations, configurations, _service = self._sync([_terminal(2, 43, 53)])
		self.assertEqual((locations["created"], locations["updated"]), (0, 1))
		self.assertEqual((configurations["created"], configurations["updated"]), (0, 1))
		location.reload()
		self.assertEqual(location.wallee_location_version_id, "43")

	def test_a_space_without_terminals_says_where_to_start(self):
		locations, configurations, _service = self._sync([])
		self.assertEqual((locations["total"], configurations["total"]), (0, 0))
		self.assertIn("message", locations)
		self.assertIn("message", configurations)
		self.assertFalse(frappe.db.exists("Wallee Location", {"wallee_location_id": str(LOCATION_ID)}))

	def _create(self):
		self._sync([_terminal(2, 42, 52)])
		location = frappe.db.get_value("Wallee Location", {"wallee_location_id": str(LOCATION_ID)})
		configuration = frappe.db.get_value(
			"Wallee Terminal Configuration", {"wallee_configuration_id": str(CONFIGURATION_ID)}
		)
		with patch.object(wallee_api, "_terminal_binding", return_value="_T1730 binding"):
			return wallee_api.create_terminal("wallee_test", f"{PREFIX} Till", configuration, location)

	def test_create_terminal_sends_what_wallee_reads(self):
		sent = []

		def _post(prov, body):
			sent.append(body)
			raise _Stop  # nothing past the request: no Payment Device

		with patch.object(wallee_api, "_post_terminal", _post):
			for _attempt in range(2):
				with self.assertRaises(_Stop):
					self._create()

		# The object ids, not their versions, and the environment the SDK model cannot carry.
		body = sent[0]
		self.assertEqual(
			{key: body[key] for key in ("name", "configuration", "location", "environment")},
			{
				"name": f"{PREFIX} Till",
				"configuration": CONFIGURATION_ID,
				"location": LOCATION_ID,
				"environment": "TEST",
			},
		)
		self.assertTrue(body["externalId"])
		# Retried after a failed link, the wizard asks Wallee for the same terminal, not a second one.
		self.assertEqual(sent[1]["externalId"], body["externalId"])

	def test_the_request_goes_out_signed_and_a_refusal_reaches_the_operator(self):
		calls = []

		def _call_api(
			api_client, method, url, header_params=None, body=None, post_params=None, **kwargs
		):
			calls.append((method, url, header_params or {}, body))
			return _Refusal()

		with patch("wallee.api_client.ApiClient.call_api", _call_api):
			with self.assertRaises(frappe.ValidationError) as refused:
				self._create()

		method, url, headers, body = calls[0]
		self.assertEqual(method, "POST")
		self.assertIn("/payment/terminals", url)
		self.assertTrue(headers.get("Authorization"))
		self.assertEqual(body["configuration"], CONFIGURATION_ID)
		self.assertIn("Please get in contact with us.", str(refused.exception))
