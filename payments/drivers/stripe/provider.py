# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""Stripe provider — credentials + lifecycle.

The provider holds the bare-minimum Stripe configuration shared by all
Stripe channels (Web, Terminal, future Billing). Channel-specific knobs
(``terminal_default_location_id``, ``webhook_secret_override``, …) live in the
matching ``Provider Channel Settings.config_json``.

Credentials are stored in ``Payment Provider.credentials_json`` as a JSON
object with at least:

	{
		"secret_key":     "sk_test_xxx",
		"publishable_key": "pk_test_xxx",
		"webhook_secret":  "whsec_xxx"
	}

A ``mode`` toggle (``test`` / ``live``) lives on the Payment Provider record
itself; we don't store two sets of keys per record. Two environments = two
records (e.g. ``stripe_test`` / ``stripe_live``).
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _

from payments.drivers.base import PaymentProviderBase


class StripeProvider(PaymentProviderBase):
	"""Stripe provider wrapper.

	Holds a memoized reference to the ``stripe`` SDK module configured with the
	credentials of the Payment Provider record. We DO NOT mutate the module-level
	``stripe.api_key`` because Frappe is multi-tenant: another site in the same
	process may use a different Stripe account. Instead, the SDK is used via
	per-call ``api_key`` kwargs.
	"""

	name = "stripe"

	@classmethod
	def from_doc(cls, provider_doc) -> "StripeProvider":  # noqa: ANN001
		return cls(provider_doc)

	def get_credentials(self) -> dict[str, Any]:
		creds = self.provider_doc.get_credentials()
		# Validate the required keys are present without raising on get to keep the
		# error message friendly.
		missing = [k for k in ("secret_key",) if not creds.get(k)]
		if missing:
			raise frappe.ValidationError(
				_("Stripe Payment Provider {0} is missing credentials: {1}").format(
					self.provider_doc.name, ", ".join(missing)
				)
			)
		return creds

	@property
	def secret_key(self) -> str:
		return self.get_credentials()["secret_key"]

	@property
	def publishable_key(self) -> str | None:
		return self.get_credentials().get("publishable_key")

	@property
	def webhook_secret(self) -> str | None:
		return self.get_credentials().get("webhook_secret")

	def health_check(self) -> dict[str, Any]:
		"""Probe Stripe reachability via a cheap API call.

		Uses ``stripe.Balance.retrieve()`` because it is permission-light and
		account-wide. Returns ``{ok, account_id?, livemode?, error?}``.
		"""
		try:
			import stripe

			balance = stripe.Balance.retrieve(api_key=self.secret_key)
			# ``balance.livemode`` is true if the secret key is a live key.
			return {
				"ok": True,
				"livemode": bool(getattr(balance, "livemode", False)),
				"provider": self.provider_doc.name,
				"mode": self.provider_doc.mode,
			}
		except Exception as exc:  # noqa: BLE001 — driver-agnostic catch
			return {"ok": False, "error": repr(exc), "provider": self.provider_doc.name}

	def list_supported_channels(self) -> list[str]:
		return ["web", "terminal", "qr_bridge"]  # qr_bridge ≈ Stripe-hosted TWINT QR (Phase 5+)
