# //// Neoffice — added file (no upstream equivalent). The three ABCs of the
# //// Provider × Channel × Driver ontology: PaymentProviderBase (credentials,
# //// health check, supported channels), PaymentChannelBase (declarative
# //// capabilities) and PaymentDriverBase (create / confirm / cancel / refund /
# //// handle_webhook). Upstream has no driver layer at all — it fuses credentials,
# //// channel config and business logic into one `<psp>_settings` DocType per PSP,
# //// which cannot grow a second channel without copying the credentials (ADR-004).
# //// Every concrete driver under payments/drivers/<provider>/ implements these.
# //// Commits: e32ecf5 2026-05-13 "feat(payments): Phase 1 — unified payment driver
# //// layer (Provider × Channel × Driver)"; a3a4a4a 2026-08-24 "docs(drivers): le
# //// contrat oubliait une action que le code produit" (its marker was in French and
# //// sat inside a docstring; rewritten in English beside the field, 2026-09-04).
# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""Abstract base classes for the Provider × Channel × Driver hierarchy.

These ABCs define the minimal contract that every concrete payment driver must
fulfill. They are intentionally narrow:

- :class:`PaymentProviderBase` exposes provider-level concerns: credentials,
  health checks, list of supported channels.
- :class:`PaymentChannelBase` describes a *channel* (web checkout, POS terminal,
  QR bridge, …) with declarative capabilities.
- :class:`PaymentDriverBase` is the operational interface used by the rest of
  the codebase to create intents, confirm them, cancel, refund, and handle
  webhooks.

Concrete subclasses live under ``payments.drivers.<provider>.*``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class IntentRequest:
	"""Input to :meth:`PaymentDriverBase.create_intent`.

	Decouples the driver from the Frappe Document so drivers can be unit-tested
	without a database. The API layer builds an ``IntentRequest`` from the
	persisted ``Payment Intent`` doc and passes it down.
	"""

	intent_name: str
	"""Frappe Payment Intent record name (e.g. ``PI-2026-00000001``)."""

	amount: int
	"""Amount in the smallest currency unit (rappen, cents, …)."""

	currency: str
	"""ISO 4217 uppercase code, e.g. ``CHF``."""

	reference_doctype: str | None = None
	reference_name: str | None = None
	device_id: str | None = None
	"""Provider-side device id (e.g. ``tmr_xxx`` for Stripe Terminal)."""

	metadata: dict[str, Any] = field(default_factory=dict)
	"""Free-form metadata propagated to the provider when supported."""


@dataclass
class DriverResponse:
	"""Normalized response returned by every driver method.

	Drivers translate provider-specific responses into this shape. The API layer
	uses ``status`` and ``next_action`` to update the Payment Intent.
	"""

	status: str
	"""One of: ``requires_action``, ``processing``, ``succeeded``, ``failed``,
	``canceled``, ``refunded``."""

	provider_intent_id: str | None = None
	"""External transaction id (e.g. ``pi_xxx``)."""

	client_secret: str | None = None
	"""Optional secret used by SDK-based confirmation (Stripe Web)."""

	# //// Neoffice — `native_app_handoff` was missing from this list even though the
	# //// Payrexx tap-to-pay driver has returned it since the day it was written. A
	# //// consumer that trusts the docstring to enumerate the cases therefore misses
	# //// one and treats it as silence — here that would have read as "nothing to
	# //// collect" on a payment that was very real. Any action added to the code must
	# //// be added to this sentence too: it is the contract the screens read.
	next_action_type: str = "none"
	"""One of: ``none``, ``display_card_present_modal``, ``display_qr_payload``,
	``native_app_handoff``, ``redirect_to_url``, ``requires_confirmation``.
	"""

	next_action_payload: dict[str, Any] = field(default_factory=dict)

	error_code: str | None = None
	error_message: str | None = None

	raw: dict[str, Any] = field(default_factory=dict)
	"""Untransformed provider response, for audit/debug."""


@dataclass
class WebhookResult:
	"""Result returned by :meth:`PaymentDriverBase.handle_webhook`.

	``intent_name`` is set when the event maps to a known Payment Intent. The API
	layer uses ``target_status`` to drive the FSM via
	:meth:`PaymentIntent.transition_to`.
	"""

	event_id: str
	event_type: str
	signature_valid: bool
	intent_name: str | None = None
	target_status: str | None = None
	error_code: str | None = None
	error_message: str | None = None
	payload_excerpt: str = ""


class PaymentProviderBase(ABC):
	"""Provider-level interface.

	One concrete subclass per PSP (Stripe, Twint, Wallee, …). Built once per
	process and held by drivers as a reference for credentials lookup.
	"""

	#: Stable provider identifier (``stripe``, ``twint``, …). Must match the
	#: ``provider_name`` field of the corresponding ``Payment Provider`` record.
	name: str = ""

	def __init__(self, provider_doc) -> None:  # noqa: ANN001 (Document is dynamic)
		self.provider_doc = provider_doc

	@abstractmethod
	def get_credentials(self) -> dict[str, Any]:
		"""Return the decrypted credentials dict for this provider."""

	@abstractmethod
	def health_check(self) -> dict[str, Any]:
		"""Probe the provider's reachability. Returns ``{"ok": bool, ...}``."""

	def list_supported_channels(self) -> list[str]:
		"""Return the list of Channel codes this provider supports.

		Default: empty. Override in concrete providers.
		"""
		return []


class PaymentChannelBase(ABC):
	"""Channel-level interface.

	Describes a way of consuming a provider. Mostly declarative — its capabilities
	tell the frontend what UI affordances to show (tip support, refund support, …).
	"""

	#: Channel code (``web``, ``terminal``, ``qr_bridge``, …).
	code: str = ""

	#: Capability flags. Override in concrete channels.
	capabilities: dict[str, Any] = {}

	def supports_currency(self, ccy: str) -> bool:
		"""Default: accept any currency. Override for channels with restrictions."""
		return bool(ccy and len(ccy) == 3)


class PaymentDriverBase(ABC):
	"""Operational driver for one ``(Provider, Channel)`` pair.

	All methods receive *high-level* dataclasses and return :class:`DriverResponse`,
	keeping Frappe ORM out of the driver. The API layer is responsible for
	persisting the resulting state into the Payment Intent record.
	"""

	#: Globally unique driver code, e.g. ``stripe.terminal``.
	code: str = ""

	def __init__(
		self,
		provider: PaymentProviderBase,
		channel: PaymentChannelBase,
		settings_doc=None,  # noqa: ANN001
	) -> None:
		self.provider = provider
		self.channel = channel
		self.settings_doc = settings_doc

	@abstractmethod
	def create_intent(self, request: IntentRequest) -> DriverResponse:
		"""Create the underlying provider intent (or reservation) from a request."""

	@abstractmethod
	def confirm_intent(self, provider_intent_id: str, **kwargs: Any) -> DriverResponse:
		"""Confirm an intent that requires explicit confirmation (capture, attach to device)."""

	@abstractmethod
	def cancel_intent(self, provider_intent_id: str) -> DriverResponse:
		"""Cancel an in-flight intent."""

	@abstractmethod
	def refund(self, provider_intent_id: str, amount: int | None = None) -> DriverResponse:
		"""Refund a settled intent. ``amount=None`` means full refund."""

	@abstractmethod
	def handle_webhook(self, payload: bytes, headers: dict[str, str]) -> WebhookResult:
		"""Verify and parse a webhook event, returning what the API layer must do."""
