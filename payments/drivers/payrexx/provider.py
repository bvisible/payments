# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""Payrexx provider — credentials lookup + health check.

Payrexx is a Swiss PSP (Thun) that covers in one contract what we currently
address with two integrations: card-present at the terminal (ECR on NexGo), TWINT,
and web checkout. It is added as a **third provider alongside** Stripe Terminal
and the TWINT PHP bridge, not as a replacement — guigoz runs in production on
those and must not regress. The choice is per client, through
``Payment Provider`` + ``POS Payment Driver Mapping``.

Unlike the TWINT provider, nothing needs hosting: Payrexx is a SaaS reached over
HTTPS, and all HTTP work lives in the standalone ``payrexx`` library. This class
only resolves credentials and probes reachability.

One security note worth repeating wherever these credentials are handled:
**Payrexx API keys are not scoped.** An account can hold several and the back
office labels them by purpose (one may be marked *POS*), but verified against a
live account, a key labelled for a POS device reads the merchant API and the
account balance. A key deployed on a terminal is a full credential.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _

from payments.drivers.base import PaymentProviderBase
from payments.drivers.payrexx._common import build_client


class PayrexxProvider(PaymentProviderBase):
	name = "payrexx"

	@classmethod
	def from_doc(cls, provider_doc) -> PayrexxProvider:  # noqa: ANN001
		return cls(provider_doc)

	# ------------------------------------------------------------------------
	# Credentials
	# ------------------------------------------------------------------------

	def get_credentials(self) -> dict[str, Any]:
		"""Return the decrypted credentials, validating the two required keys."""
		creds = self.provider_doc.get_credentials() or {}
		missing = [key for key in ("instance", "api_secret") if not creds.get(key)]
		if missing:
			raise frappe.ValidationError(
				_("Payrexx Payment Provider {0} is missing {1} in credentials_json").format(
					self.provider_doc.name, ", ".join(missing)
				)
			)
		return creds

	@property
	def instance(self) -> str:
		"""Account name — the ``<instance>`` in ``https://<instance>.payrexx.com``."""
		return str(self.get_credentials()["instance"]).strip()

	@property
	def webhook_signing_key(self) -> str | None:
		"""Signing key for ``X-Webhook-Signature``, from *Webhooks* in the back office.

		Optional at the provider level so a web-only integration can start before
		webhooks are configured, but :mod:`payments.api.webhook_payrexx` refuses to
		process a delivery without it — an unverified webhook is
		attacker-controlled input and must never drive a payment state machine.
		"""
		value = self.get_credentials().get("webhook_signing_key")
		return str(value).strip() if value else None

	def build_client(self):  # noqa: ANN201 - payrexx.PayrexxClient, imported lazily
		"""Build a configured Payrexx API client."""
		return build_client(self.provider_doc)

	# ------------------------------------------------------------------------
	# Health
	# ------------------------------------------------------------------------

	def health_check(self) -> dict[str, Any]:
		"""Probe credentials and reachability. Never raises.

		Uses the payment-provider endpoint, which is read-only and cheap. Reports
		the payment methods that are actually **enabled** on the account, because a
		method can be supported by the PSP yet switched off — and offering a
		switched-off method makes the hosted page reject the shopper's choice.
		"""
		try:
			client = self.build_client()
		except Exception as exc:  # noqa: BLE001 - health checks report, never raise
			return {"ok": False, "provider": self.provider_doc.name, "error": str(exc)}

		with client:
			result = client.health_check()

		result["provider"] = self.provider_doc.name
		return result

	def list_supported_channels(self) -> list[str]:
		"""Channels this provider can serve.

		``payrexx_tap_to_pay`` is listed, with one caveat worth knowing: unlike the
		other two, its driver cannot **initiate** a payment. Tap to Pay is an Android
		app-to-app integration over Intents, so the phone starts it and the webhook
		records it; the driver exists to hold the intent, read the transaction back and
		refund it. See :mod:`payments.drivers.payrexx.tap_to_pay_driver`.
		"""
		return ["payrexx_web", "terminal", "payrexx_tap_to_pay"]
