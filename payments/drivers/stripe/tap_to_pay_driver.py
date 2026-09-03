# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""Stripe Tap to Pay driver — the phone is the reader, the server keeps the books.

Tap to Pay on iPhone and on Android is built into the Stripe Terminal SDK: the
merchant's own app collects the card and confirms the PaymentIntent, so there is no
device to address from the server and no reader to push anything to. The division of
labour is therefore:

- :meth:`StripeTapToPayDriver.create_intent` creates the PaymentIntent (card-present,
  **automatic** capture) and returns its ``client_secret`` — that is what the phone
  hands to ``retrievePaymentIntent`` / ``collectPaymentMethod`` / ``confirmPaymentIntent``;
- the phone confirms; Stripe captures; the ``payment_intent.succeeded`` webhook drives
  the Payment Intent FSM through :mod:`payments.api.webhook_stripe`, exactly as for the
  physical readers — the event carries ``metadata.frappe_intent_name``;
- :meth:`get_status` reads the PaymentIntent back for the poll that backs the webhook.

Automatic capture, unlike the physical-terminal driver's manual capture: there is no
cashier screen to adjust a tip on after authorisation, and a captured payment is what
the technician standing at the customer's door needs to see. Manual capture would leave
the intent in ``requires_capture`` until someone captured it, which nothing here would.

.. note::
   The device requirements are Stripe's, enforced by the SDK on the phone: not rooted,
   locked bootloader, Android 13+ / iOS 16.4+, developer options off. Nothing server-side
   can relax them, and nothing here needs to know about them.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from payments.drivers.base import DriverResponse, IntentRequest, PaymentChannelBase
from payments.drivers.stripe.provider import StripeProvider
from payments.drivers.stripe.terminal_driver import StripeTerminalDriver

CHANNEL = "stripe_tap_to_pay"

# Stripe PaymentIntent statuses → Payment Intent FSM statuses.
_PI_STATUS_TO_FSM: dict[str, str] = {
	"succeeded": "succeeded",
	"canceled": "canceled",
	"processing": "processing",
	# Waiting on the phone: the card has not been presented, or not confirmed.
	"requires_payment_method": "requires_action",
	"requires_confirmation": "requires_action",
	"requires_action": "requires_action",
	# Only reachable with manual capture; kept so an odd intent is not mislabelled.
	"requires_capture": "processing",
}


class StripeTapToPayChannel(PaymentChannelBase):
	code = CHANNEL
	capabilities = {
		"supports_refund": True,
		"supports_partial_refund": True,
		# Tip on Stripe Terminal is a US-only receipt-screen feature.
		"supports_tip": False,
		"async": True,
		# The reader is the operator's phone, attached to a Stripe Location at
		# connect time by the SDK. It is not a registered Payment Device.
		"requires_device": False,
		"requires_redirect": False,
	}


class StripeTapToPayDriver(StripeTerminalDriver):
	"""Card-present PaymentIntents confirmed on the operator's phone."""

	code = "stripe.tap_to_pay"

	@classmethod
	def from_docs(cls, provider_doc, channel_doc, binding_doc):  # noqa: ANN001
		return cls(StripeProvider(provider_doc), StripeTapToPayChannel(), settings_doc=binding_doc)

	# ------------------------------------------------------------------------
	# Internals
	# ------------------------------------------------------------------------

	def location_id(self) -> str | None:
		"""The Stripe Terminal Location the phone attaches to.

		Read from the binding's ``config_json`` (``{"location_id": "tml_…"}``), which
		the Mobile Payment Settings keep in step with what the merchant chose.
		"""
		return (self._binding_config().get("location_id") or "").strip() or None

	# ------------------------------------------------------------------------
	# Driver contract
	# ------------------------------------------------------------------------

	def create_intent(self, request: IntentRequest) -> DriverResponse:
		"""Create the PaymentIntent and hand its ``client_secret`` to the phone.

		No reader is involved server-side; the ``native_app_handoff`` action tells
		the app it now owns the next step.
		"""
		metadata = {
			"frappe_intent_name": request.intent_name,
			"channel": "tap_to_pay",
		}
		if request.reference_doctype and request.reference_name:
			metadata["reference_doctype"] = request.reference_doctype
			metadata["reference_name"] = request.reference_name
		metadata.update({k: str(v) for k, v in (request.metadata or {}).items()})

		# Same idempotency strategy as the terminal driver: keyed on the intent and
		# on a hash of the body, so a network retry hits Stripe's cache while a
		# deliberate re-run with different content gets a fresh key.
		body_seed = json.dumps(
			{
				"a": request.amount,
				"c": request.currency.lower(),
				"m": {k: str(v) for k, v in metadata.items()},
			},
			sort_keys=True,
		)
		body_hash = hashlib.sha256(body_seed.encode("utf-8")).hexdigest()[:12]
		idempotency_key = f"pi_ttp_{request.intent_name}_{body_hash}"

		try:
			pi = self._stripe.PaymentIntent.create(
				api_key=self._api_key,
				amount=request.amount,
				currency=request.currency.lower(),
				payment_method_types=["card_present"],
				capture_method="automatic",
				metadata=metadata,
				idempotency_key=idempotency_key,
			)
		except self._stripe.error.StripeError as exc:
			return DriverResponse(
				status="failed",
				error_code=getattr(exc, "code", "stripe_error") or "stripe_error",
				error_message=str(exc.user_message or exc) if hasattr(exc, "user_message") else str(exc),
				raw={"exception": repr(exc)},
			)

		return DriverResponse(
			status="requires_action",
			provider_intent_id=pi.id,
			client_secret=pi.client_secret,
			next_action_type="native_app_handoff",
			next_action_payload={
				"handoff": "stripe_terminal",
				"location_id": self.location_id(),
				"payment_intent_id": pi.id,
			},
			raw=pi.to_dict() if hasattr(pi, "to_dict") else dict(pi),
		)

	def confirm_intent(self, provider_intent_id: str, **kwargs: Any) -> DriverResponse:
		"""Not applicable — the phone confirms. Answered as a status read."""
		return self.get_status(provider_intent_id)

	def get_status(self, provider_intent_id: str) -> DriverResponse:
		"""Read the PaymentIntent back — the poll under the webhook."""
		try:
			pi = self._stripe.PaymentIntent.retrieve(provider_intent_id, api_key=self._api_key)
		except self._stripe.error.StripeError as exc:
			return DriverResponse(
				status="processing",
				provider_intent_id=provider_intent_id,
				error_code=getattr(exc, "code", "stripe_error") or "stripe_error",
				error_message=str(exc),
			)
		raw_status = str(pi.status or "")
		target = _PI_STATUS_TO_FSM.get(raw_status, "processing")
		error = pi.get("last_payment_error") if hasattr(pi, "get") else None
		return DriverResponse(
			status=target,
			provider_intent_id=pi.id,
			client_secret=pi.client_secret,
			error_code=(error or {}).get("code") if error else None,
			error_message=(error or {}).get("message") if error else None,
			raw=pi.to_dict() if hasattr(pi, "to_dict") else dict(pi),
		)
