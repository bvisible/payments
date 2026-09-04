#//// Neoffice — added file (no upstream equivalent). `Payment Provider` is level 1
#//// of the Provider × Channel × Driver ontology (ADR-004 §1): one record per PSP,
#//// holding the credentials and the default driver class shared by every channel.
#//// Upstream fuses credentials, channel config and business logic into a single
#//// `<psp>_settings` DocType per PSP, so each new channel would mean copying the
#//// same API keys into yet another settings doctype.
#//// Commits: e32ecf5 2026-05-13 "feat(payments): Phase 1 — unified payment driver layer (Provider × Channel × Driver)"
# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE

import json

import frappe
from frappe import _
from frappe.model.document import Document


class PaymentProvider(Document):
	"""A Payment Service Provider (PSP) or external bridge.

	A Payment Provider holds the credentials and high-level configuration shared
	across all Channels (Web checkout, POS Terminal, Server-side bridge, etc.).
	Channel-specific configuration lives in `Provider Channel Settings`.
	"""

	def validate(self):
		self._validate_provider_name()
		self._validate_credentials_json()
		self._validate_driver_class()

	def _validate_provider_name(self):
		# Provider name must be a lowercase, alphanumeric (+ underscore) identifier.
		name = (self.provider_name or "").strip()
		if not name:
			frappe.throw(_("Provider Name is required"))
		if not name.replace("_", "").isalnum() or not name.islower():
			frappe.throw(
				_("Provider Name must be lowercase alphanumeric with optional underscores (got: {0})").format(name)
			)
		self.provider_name = name

	def _validate_credentials_json(self):
		# Credentials, if provided, must be a valid JSON object.
		if not self.credentials_json:
			return
		try:
			value = json.loads(self.credentials_json)
		except (ValueError, TypeError) as exc:
			frappe.throw(_("Credentials JSON is invalid: {0}").format(str(exc)))
		if not isinstance(value, dict):
			frappe.throw(_("Credentials JSON must be a JSON object (dict)"))

	def _validate_driver_class(self):
		# Driver class must be a dotted python path (no leading/trailing dots).
		if not self.driver_class:
			return
		path = self.driver_class.strip()
		if not path or path.startswith(".") or path.endswith("."):
			frappe.throw(_("Driver Class path is invalid: {0}").format(path))
		self.driver_class = path

	def get_credentials(self) -> dict:
		"""Return the parsed credentials dict. Empty dict if none configured."""
		if not self.credentials_json:
			return {}
		try:
			return json.loads(self.credentials_json)
		except (ValueError, TypeError):
			return {}
