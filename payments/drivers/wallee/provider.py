# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""Wallee provider — credentials lookup + health check.

Wallee credentials live on the **single DocType ``Wallee Settings``**, owned by
the legacy ``wallee_integration`` app (which stays installed for this purpose).
We deliberately do NOT mirror them into ``Payment Provider.credentials_json``:
operators already manage them via the Wallee Settings desk page, and the
existing wallee_integration webshop/guest flow reads from the same source.

The ``Payment Provider`` record for Wallee therefore carries minimal data:
``provider_name=wallee_test``, ``mode``, ``driver_class``, and an empty
``credentials_json=\"{}\"``.

If the operator wants per-instance overrides (test vs live keys on the same
site), they create two ``Payment Provider`` records with distinct ``mode`` and
two ``Wallee Settings``-equivalent DocTypes — out of scope for v1.
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
		return frappe.get_single("Wallee Settings")

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
