#//// Neoffice — added file (no upstream equivalent). Placeholder `stripe.web`
#//// driver: it exists so that a `Provider Channel Settings` binding created ahead
#//// of Phase 6 still resolves to an importable class instead of failing the
#//// registry lookup. Web checkout meanwhile keeps flowing through the legacy
#//// upstream `Stripe Settings` controller; every operation raises NotImplementedError.
#//// Commits: 0efe5ef 2026-05-13 "feat(payments): Phase 2 — Stripe Terminal server-driven driver"
# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""Stripe Web driver (placeholder for Phase 6).

Phase 6 will refactor ``payments/templates/pages/stripe_checkout.py`` to flow
through the unified intent layer. Until then, web checkout keeps using the
legacy ``Stripe Settings`` controller.

This file exists so the ``stripe.web`` driver code lookup
(``payments.drivers.stripe.web_driver.StripeWebDriver``) does not 404 if an
operator pre-creates the ``Provider Channel Settings`` binding.
"""

from __future__ import annotations

from typing import Any

from payments.drivers.base import (
	DriverResponse,
	IntentRequest,
	PaymentChannelBase,
	PaymentDriverBase,
	WebhookResult,
)
from payments.drivers.stripe.provider import StripeProvider


class StripeWebChannel(PaymentChannelBase):
	code = "web"
	capabilities = {"supports_refund": True, "supports_partial_refund": True, "async": False}


class StripeWebDriver(PaymentDriverBase):
	"""Stub Stripe Web driver — to be implemented in Phase 6.

	Currently raises ``NotImplementedError`` on every operation. Web checkout
	users continue to flow through the legacy ``Stripe Settings`` controller in
	``payments/payment_gateways/doctype/stripe_settings/``.
	"""

	code = "stripe.web"

	@classmethod
	def from_docs(cls, provider_doc, channel_doc, binding_doc):  # noqa: ANN001
		return cls(StripeProvider(provider_doc), StripeWebChannel(), settings_doc=binding_doc)

	def _raise(self) -> "DriverResponse":
		raise NotImplementedError(
			"StripeWebDriver is a Phase 6 placeholder. Use the legacy "
			"`payment_gateways/stripe_settings` controller until Phase 6 lands."
		)

	def create_intent(self, request: IntentRequest) -> DriverResponse:  # pragma: no cover
		return self._raise()

	def confirm_intent(self, provider_intent_id: str, **kwargs: Any) -> DriverResponse:  # pragma: no cover
		return self._raise()

	def cancel_intent(self, provider_intent_id: str) -> DriverResponse:  # pragma: no cover
		return self._raise()

	def refund(self, provider_intent_id: str, amount: int | None = None) -> DriverResponse:  # pragma: no cover
		return self._raise()

	def handle_webhook(self, payload: bytes, headers: dict[str, str]) -> WebhookResult:  # pragma: no cover
		return self._raise()
