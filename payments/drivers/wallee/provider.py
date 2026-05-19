# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""Wallee provider — credentials lookup + health check.

After ADR-005 (fusion of ``wallee_integration`` into ``payments``), credentials
live in a regular (non-single) ``Wallee Settings`` DocType — one record per
``Payment Provider``. The ``Payment Provider.driver_class`` points to the
Wallee driver class, and ``Wallee Settings.provider`` links back to the
provider record.

This means two Wallee accounts can cohabit on the same site (e.g.
``wallee_test`` and ``wallee_live``) — they each have their own
``Wallee Settings`` row with distinct API credentials, webhook secret,
locations, etc.

The ``Payment Provider.credentials_json`` is left empty for Wallee (``{}``):
all Wallee-specific config lives on the linked ``Wallee Settings`` row, which
exposes a richer schema (POS toggles, terminal defaults, webshop options).
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _

from payments.drivers.base import PaymentProviderBase


class WalleeProvider(PaymentProviderBase):
	"""Wallee provider wrapper.

	Holds a memoized reference to the singleton ``Wallee Settings`` row and
	exposes its fields as Python properties. The Wallee SDK ``Configuration``
	object is built lazily by the driver when an API call is actually made.
	"""

	name = "wallee"

	@classmethod
	def from_doc(cls, provider_doc) -> "WalleeProvider":  # noqa: ANN001
		return cls(provider_doc)

	def _settings(self):  # noqa: ANN202
		# Re-fetch each time so credential rotations propagate without a restart.
		# Cost is one row read — negligible compared with the network call that
		# follows.
		#
		# Wallee Settings is keyed by ``provider`` (Link → Payment Provider). One
		# record per Wallee account. autoname=field:provider so the document name
		# equals the provider name.
		name = frappe.db.get_value("Wallee Settings", {"provider": self.provider_doc.name})
		if not name:
			raise frappe.ValidationError(
				_("No Wallee Settings row found for Payment Provider {0}. "
				  "Create one at /app/wallee-settings/new?provider={0}.").format(
					self.provider_doc.name
				)
			)
		return frappe.get_doc("Wallee Settings", name)

	def get_credentials(self) -> dict[str, Any]:
		"""Return the decrypted credentials dict.

		Raises if Wallee Settings is missing or incomplete.
		"""
		settings = self._settings()
		if not settings.enabled:
			raise frappe.ValidationError(
				_("Wallee Settings is not enabled — Wallee provider {0} cannot operate").format(
					self.provider_doc.name
				)
			)
		missing = [
			k for k in ("user_id", "authentication_key", "space_id")
			if not settings.get(k)
		]
		if missing:
			raise frappe.ValidationError(
				_("Wallee Settings is missing credentials: {0}").format(", ".join(missing))
			)
		return {
			"user_id": settings.user_id,
			"authentication_key": settings.get_password("authentication_key"),
			"space_id": settings.space_id,
			"api_host": settings.api_host or "https://app-wallee.com/api/v2.0",
			"webhook_secret": (
				settings.get_password("webhook_secret") if settings.webhook_secret else None
			),
		}

	@property
	def space_id(self) -> int:
		return int(self.get_credentials()["space_id"])

	@property
	def webhook_secret(self) -> str | None:
		return self.get_credentials().get("webhook_secret")

	def build_configuration(self):  # noqa: ANN201 — wallee.Configuration is a runtime class
		"""Instantiate a ``wallee.Configuration`` from the current credentials."""
		from wallee.configuration import Configuration

		creds = self.get_credentials()
		return Configuration(
			user_id=creds["user_id"],
			authentication_key=creds["authentication_key"],
			host=creds["api_host"],
		)

	def health_check(self) -> dict[str, Any]:
		"""Probe Wallee reachability via a cheap API call.

		Lists transactions in the configured space — light call that fails fast on
		bad credentials or networking issues. Returns ``{ok, space_id?, error?}``.
		"""
		try:
			from wallee import TransactionsService

			config = self.build_configuration()
			TransactionsService(config).get_payment_transactions(self.space_id)
			return {
				"ok": True,
				"space_id": self.space_id,
				"provider": self.provider_doc.name,
				"mode": self.provider_doc.mode,
			}
		except Exception as exc:  # noqa: BLE001 — driver-agnostic catch
			return {"ok": False, "error": repr(exc), "provider": self.provider_doc.name}

	def list_supported_channels(self) -> list[str]:
		# Only `terminal` for now. Wallee also supports hosted web checkout
		# (`web` channel) via the same SDK — wired in a later phase.
		return ["terminal"]
