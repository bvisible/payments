# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE

import json

import frappe
from frappe import _
from frappe.model.document import Document


class ProviderChannelSettings(Document):
	"""Bridges a Payment Provider × Payment Channel with channel-specific config.

	One record per (Provider, Channel) pair. Holds the driver_class override
	and a JSON config blob that differs per channel (e.g. terminal_location_id
	for Stripe×Terminal, merchant_uuid for Twint×QR Bridge).
	"""

	def validate(self):
		self._validate_uniqueness()
		self._validate_config_json()
		self._compute_webhook_endpoint()

	def _validate_uniqueness(self):
		# DB-level unique not enforced via JSON autoname format; check programmatically.
		existing = frappe.db.exists(
			"Provider Channel Settings",
			{"provider": self.provider, "channel": self.channel, "name": ("!=", self.name)},
		)
		if existing:
			frappe.throw(
				_("A Provider Channel Settings already exists for {0} × {1}: {2}").format(
					self.provider, self.channel, existing
				)
			)

	def _validate_config_json(self):
		if not self.config_json:
			return
		try:
			value = json.loads(self.config_json)
		except (ValueError, TypeError) as exc:
			frappe.throw(_("Config JSON is invalid: {0}").format(str(exc)))
		if not isinstance(value, dict):
			frappe.throw(_("Config JSON must be a JSON object (dict)"))

	def _compute_webhook_endpoint(self):
		# Webhook endpoint is conventional: /api/method/payments.api.webhook_<provider>.handle
		if self.provider:
			self.webhook_endpoint = f"/api/method/payments.api.webhook_{self.provider}.handle"

	def get_config(self) -> dict:
		"""Return parsed config dict."""
		if not self.config_json:
			return {}
		try:
			return json.loads(self.config_json)
		except (ValueError, TypeError):
			return {}

	def get_effective_driver_class(self) -> str | None:
		"""Driver class for this binding: override → provider default."""
		if self.driver_class:
			return self.driver_class
		if self.provider:
			provider_driver = frappe.db.get_value("Payment Provider", self.provider, "driver_class")
			return provider_driver or None
		return None
