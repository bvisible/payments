# //// Neoffice — added file (no upstream equivalent). TWINT credentials lookup +
# //// health check. TWINT's official SDK is PHP and needed the merchant P12 certificate
# //// on disk, so it was hosted on neoservice and every instance called it over HTTP
# //// (ADR-002, the EBICS pattern). Since maintenance#1721 each instance speaks TWINT
# //// with its own Python engine and the hub's PHP bridge is retired (10.11.2026); the
# //// service_url + Frappe token remain for the hub's certificate store.
# //// Commits: 258f8cf 2026-05-13 "feat(payments): Phase 4 — TWINT PHP bridge driver + scheduler poll"
# ////          ec69d96 2026-05-19 "feat(twint): Phase 11 fusion twint_integration → payments (webshop QR consumer + dialog)"
# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""TWINT provider — credentials lookup + health check.

Every instance speaks TWINT with its own engine (:mod:`payments.twint_engine`), with
the merchant's P12 certificate kept encrypted on the instance. The hub
(``neoservice.neoffice.me``, :mod:`neoffice_devops.api.twint`) keeps a copy of each
certificate, which an instance uploads or takes once; its PHP bridge is retired.

The provider credentials contain:

- ``service_url``: typically ``https://neoservice.neoffice.me`` (also resolvable
  from ``frappe.conf.twint_service_url`` for backward compat with EBICS pattern)
- ``api_key`` + ``api_secret``: Frappe Token for authenticating against the TWINT
  Service User on neoservice (the certificate store)
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
	def from_doc(cls, provider_doc) -> TwintProvider:  # noqa: ANN001
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
		"""Whether this instance can speak TWINT for its default merchant: its own engine and the merchant's .p12.

		The hub's PHP bridge this used to ping is retired (10.11.2026): every call runs on the instance's engine
		(payments.twint_engine), with the certificate kept here. Nothing is sent to TWINT or to the hub.
		"""
		from payments.drivers.twint import local_engine

		merchant = self.default_merchant_uuid
		if not merchant:
			return {"ok": False, "error": "No default TWINT merchant on this provider"}
		certificate = local_engine.stored_certificate(merchant) is not None
		return {
			"ok": certificate,
			"engine": "python",
			"certificate_here": certificate,
			**(
				{} if certificate else {"error": "No TWINT certificate for this merchant on this instance yet"}
			),
		}

	def list_supported_channels(self) -> list[str]:
		# qr_bridge: merchant-initiated (POS terminal)
		# twint_web: consumer-initiated (webshop QR scanned by buyer)
		return ["qr_bridge", "twint_web"]

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
