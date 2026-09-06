# //// Neoffice — added file (no upstream equivalent). `Provider Channel Settings` is
# //// the junction of the ontology (ADR-004 §3): one record per (Provider, Channel)
# //// pair, holding the driver-class override and the config that differs per channel
# //// (`terminal_location_id` for Stripe × Terminal, `merchant_uuid` for TWINT × QR
# //// bridge) plus the conventional webhook endpoint. It is what keeps credentials in
# //// ONE place while the N×M combinations stay configurable — the thing upstream's
# //// per-PSP settings pattern cannot do without duplicating the keys.
# //// Commits: e32ecf5 2026-05-13 "feat(payments): Phase 1 — unified payment driver layer (Provider × Channel × Driver)"
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

	# //// Neoffice — the endpoint is derived from the provider FAMILY, not from the
	# //// record name, and is only written when the receiver actually ships (#219).
	# //// `self.provider` is a Payment Provider record name, and several providers of
	# //// one family live side by side on a site: `wallee_test` and `wallee_live`.
	# //// The app ships ONE receiver per family (webhook_stripe, webhook_wallee,
	# //// webhook_payrexx), so the old `webhook_{self.provider}` produced
	# //// `/api/method/payments.api.webhook_wallee_test.handle` — a 404 — and rewrote
	# //// it on EVERY save, so a path corrected by hand was silently destroyed.
	WEBHOOK_PREFIX = "/api/method/payments.api.webhook_"
	WEBHOOK_SUFFIX = ".handle"

	def _provider_family(self) -> str:
		"""The family the shipped receiver is named after: `wallee_live` -> `wallee`.

		Uses the provider's own `mode` when it has one, so the suffix is not guessed;
		falls back to the two conventional suffixes for records that predate it.
		"""
		name = (self.provider or "").strip()
		if not name:
			return ""
		mode = frappe.db.get_value("Payment Provider", name, "mode") or ""
		for suffix in (f"_{mode}" if mode else "", "_test", "_live"):
			if suffix and name.endswith(suffix) and len(name) > len(suffix):
				return name[: -len(suffix)]
		return name

	# //// Neoffice — see the block marker above: webhook endpoint fix (#219)
	def _compute_webhook_endpoint(self):
		"""Fill in the conventional endpoint — never overwrite a human's, never lie.

		Only an empty field or a value we computed ourselves (recognisable by the
		convention) is touched. A family with no receiver leaves the field EMPTY:
		an empty endpoint is honest, a path that 404s is not.
		"""
		import importlib.util

		current = (self.webhook_endpoint or "").strip()
		ours = current.startswith(self.WEBHOOK_PREFIX) and current.endswith(self.WEBHOOK_SUFFIX)
		if current and not ours:
			return  # set by hand: leave it alone

		family = self._provider_family()
		module = f"payments.api.webhook_{family}" if family else ""
		try:
			ships = bool(module) and importlib.util.find_spec(module) is not None
		except (ImportError, ValueError):
			ships = False

		if ships:
			self.webhook_endpoint = f"{self.WEBHOOK_PREFIX}{family}{self.WEBHOOK_SUFFIX}"
		elif ours:
			self.webhook_endpoint = ""

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
