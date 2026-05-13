# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""TWINT provider — credentials lookup + bridge health check.

Unlike Stripe (which lives in a centralized SaaS), TWINT requires a PHP SDK
running where the P12 certificate is stored. We host that SDK on
``neoservice.neoffice.me`` (see :mod:`neoffice_devops.api.twint`) and call it
over HTTP from every client instance.

The provider credentials therefore contain:

- ``service_url``: typically ``https://neoservice.neoffice.me`` (also resolvable
  from ``frappe.conf.twint_service_url`` for backward compat with EBICS pattern)
- ``api_key`` + ``api_secret``: Frappe Token for authenticating against the TWINT
  Service User on neoservice
- ``default_merchant_uuid``: optional, used when not provided per-call

Per-merchant TWINT credentials (Store UUID, P12 password) live on the
:class:`payments.payment_gateways.doctype.twint_settings.twint_settings.TwintSettings`
DocType, indexed by ``merchant_uuid``.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _

from payments.drivers.base import PaymentProviderBase


class TwintProvider(PaymentProviderBase):
	name = "twint"

	@classmethod
	def from_doc(cls, provider_doc) -> "TwintProvider":  # noqa: ANN001
		return cls(provider_doc)

	# ------------------------------------------------------------------------
	# Credentials
	# ------------------------------------------------------------------------

	def get_credentials(self) -> dict[str, Any]:
		creds = self.provider_doc.get_credentials()
		# Resolve service URL: provider credentials wins, else site_config.
		if not creds.get("service_url"):
			site_url = frappe.conf.get("twint_service_url")
			if site_url:
				creds["service_url"] = site_url
		if not creds.get("service_url"):
			raise frappe.ValidationError(
				_(
					"TWINT Payment Provider {0} is missing 'service_url' in credentials_json and "
					"no 'twint_service_url' is set in site_config.json"
				).format(self.provider_doc.name)
			)
		return creds

	@property
	def service_url(self) -> str:
		return self.get_credentials()["service_url"].rstrip("/")

	@property
	def api_key(self) -> str | None:
		creds = self.get_credentials()
		return creds.get("api_key") or frappe.conf.get("twint_api_key")

	@property
	def api_secret(self) -> str | None:
		creds = self.get_credentials()
		return creds.get("api_secret") or frappe.conf.get("twint_api_secret")

	@property
	def default_merchant_uuid(self) -> str | None:
		return self.get_credentials().get("default_merchant_uuid")

	# ------------------------------------------------------------------------
	# Health check
	# ------------------------------------------------------------------------

	def health_check(self) -> dict[str, Any]:
		"""Ping the TWINT bridge via its ``health_check`` command."""
		import requests

		try:
			resp = requests.post(
				f"{self.service_url}/api/method/neoffice_devops.api.twint.execute",
				headers=self._auth_headers(),
				json={
					"command": "health_check",
					"merchant_uuid": self.default_merchant_uuid or "none",
					"store_uuid": "",
					"environment": "production",
					"params": "{}",
				},
				timeout=10,
			)
			data = resp.json() if resp.headers.get("Content-Type", "").startswith("application/json") else {}
			return {
				"ok": resp.ok and data.get("message", {}).get("success", False),
				"http_status": resp.status_code,
				"detail": data,
			}
		except Exception as exc:  # noqa: BLE001
			return {"ok": False, "error": repr(exc)}

	def list_supported_channels(self) -> list[str]:
		return ["qr_bridge"]

	# ------------------------------------------------------------------------
	# Helpers
	# ------------------------------------------------------------------------

	def _auth_headers(self) -> dict[str, str]:
		key = self.api_key
		secret = self.api_secret
		if key and secret:
			return {"Authorization": f"token {key}:{secret}"}
		if key:
			return {"Authorization": f"token {key}"}
		return {}
