#//// Neoffice — added file (no upstream equivalent). `Payment Channel` is level 2 of
#//// the ontology (ADR-004 §2): one record per way of consuming a PSP — `web`,
#//// `terminal`, `qr_bridge`, `tap_to_pay` — carrying that channel's capabilities as
#//// JSON. Upstream is a web-checkout hub only and has no channel concept at all,
#//// which is exactly what makes its one-settings-doctype-per-PSP pattern dead-end.
#//// Commits: e32ecf5 2026-05-13 "feat(payments): Phase 1 — unified payment driver layer (Provider × Channel × Driver)"
# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE

import json

import frappe
from frappe import _
from frappe.model.document import Document


class PaymentChannel(Document):
	"""A consumption channel of a payment provider (Web, POS Terminal, QR Bridge, …).

	System DocType with a small number of records (~5).
	"""

	def validate(self):
		self._validate_channel_code()
		self._validate_capabilities_json()

	def _validate_channel_code(self):
		code = (self.channel_code or "").strip()
		if not code:
			frappe.throw(_("Channel Code is required"))
		if not code.replace("_", "").isalnum() or not code.islower():
			frappe.throw(
				_("Channel Code must be lowercase alphanumeric with optional underscores (got: {0})").format(code)
			)
		self.channel_code = code

	def _validate_capabilities_json(self):
		if not self.capabilities_json:
			return
		try:
			value = json.loads(self.capabilities_json)
		except (ValueError, TypeError) as exc:
			frappe.throw(_("Capabilities JSON is invalid: {0}").format(str(exc)))
		if not isinstance(value, dict):
			frappe.throw(_("Capabilities JSON must be a JSON object (dict)"))

	def get_capabilities(self) -> dict:
		"""Return parsed capabilities dict."""
		if not self.capabilities_json:
			return {}
		try:
			return json.loads(self.capabilities_json)
		except (ValueError, TypeError):
			return {}
