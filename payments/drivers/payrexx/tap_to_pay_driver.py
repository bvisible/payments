# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""Payrexx Tap to Pay driver — the phone initiates, the server records.

Unlike every other driver here, this one **cannot start a payment**. Tap to Pay is
an Android app-to-app integration: our mobile app hands off to the Payrexx Tap to Pay
app over an Intent, that app owns the NFC exchange, and the result comes back to the
phone. There is no REST call that makes a terminal ask for a card.

So the division of labour is:

- ``create_intent`` records the intent and returns what the phone must hand over —
  ``next_action_type="native_app_handoff"``. No provider call, and
  **``provider_intent_id`` stays empty**: the Payrexx transaction does not exist yet.
- the phone calls the SDK with ``order_reference`` set to the intent name;
- the **webhook** (``transaction.type == "Tap to Pay"``) is what drives the FSM and
  fills in the transaction id. It is the only signal that always arrives — the mobile
  process can be killed mid-payment and the payment still completes.
- ``get_status`` and ``refund`` read the transaction back over REST, because a Tap to
  Pay transaction is an ordinary Payrexx transaction once it exists.

.. warning::
   **The link between the two worlds is ``order_reference``.** The SDK carries it into
   the payment and the webhook is expected to return it as ``referenceId``. That
   chaining is verified in the SDK's source on both sides but **has never been
   observed end to end on a real device** — see
   ``payments.tests.payrexx_taptopay_probe`` and
   Neoffice/Payments/Payrexx/03-Tap-To-Pay-Mobile §9bis. If it turns out Payrexx does
   not propagate it, the phone has to report the transaction id back to the server
   instead, and :meth:`PayrexxTapToPayDriver.get_status` is where that would land.

.. note::
   Amounts here stay in **minor units** (rappen), as everywhere in this app. The SDK
   takes floats, and that conversion belongs in the native module — in one place,
   with an explicit rounding. Doing it server-side too would give two places to get
   it wrong.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _

from payments.drivers.base import (
	DriverResponse,
	IntentRequest,
	PaymentChannelBase,
	PaymentDriverBase,
	WebhookResult,
)
from payments.drivers.payrexx._common import build_client, error_response, map_status
from payments.drivers.payrexx.provider import PayrexxProvider

CHANNEL = "payrexx_tap_to_pay"


class PayrexxTapToPayChannel(PaymentChannelBase):
	code = CHANNEL
	capabilities = {
		"supports_refund": True,
		"supports_partial_refund": True,
		# The SDK's Sale takes a `tip` alongside the amount, so the channel really
		# does support tipping — unlike payrexx_web.
		"supports_tip": True,
		"async": True,
		# The device is the operator's own phone, not a registered Payment Device:
		# pairing happens inside the Payrexx app, on Payrexx's side. Requiring a
		# Payment Device would mean maintaining a record we cannot verify.
		"requires_device": False,
		"requires_redirect": False,
	}


class PayrexxTapToPayDriver(PaymentDriverBase):
	"""Records Tap to Pay payments that the phone initiates."""

	code = CHANNEL

	@classmethod
	def from_docs(cls, provider_doc, channel_doc, binding_doc):  # noqa: ANN001
		return cls(PayrexxProvider(provider_doc), PayrexxTapToPayChannel(), settings_doc=binding_doc)

	# ------------------------------------------------------------------------
	# Internals
	# ------------------------------------------------------------------------

	@property
	def _payrexx_provider(self) -> PayrexxProvider:
		assert isinstance(self.provider, PayrexxProvider)
		return self.provider

	def _client(self):  # noqa: ANN202
		return build_client(self._payrexx_provider.provider_doc)

	def _find_transaction(self, handle: str):  # noqa: ANN202
		"""Resolve a Payrexx transaction from either an id or an intent name.

		Both forms reach this driver. Before the webhook lands, the only handle we
		have is the Payment Intent name (``PI-2026-…``), because the transaction does
		not exist yet; afterwards it is the numeric Payrexx id. Telling them apart on
		``isdigit`` is enough — Payrexx ids are numeric and our names never are.

		Returns the newest match, or ``None``. Newest because ``referenceId`` is not
		unique on Payrexx's side: a cashier who retries after a decline leaves two
		transactions on one reference, and the last one is the one that counts.
		"""
		with self._client() as client:
			if handle.isdigit():
				return client.transaction.retrieve(handle)
			matches = client.transaction.find_by_reference(handle)
			if not matches:
				return None
			return max(matches, key=lambda t: t.id or 0)

	# ------------------------------------------------------------------------
	# Driver contract
	# ------------------------------------------------------------------------

	def create_intent(self, request: IntentRequest) -> DriverResponse:
		"""Record the intent and return what the phone hands to the Payrexx app.

		Deliberately makes **no provider call**. There is nothing to create: the
		payment starts when the operator taps in the Payrexx app. Returning
		``requires_action`` with the handoff payload is the honest description of the
		state — we are waiting on something outside our control.
		"""
		method = request.metadata.get("payment_method")
		if method and str(method).upper() not in ("CARD", "TWINT"):
			# Fail loudly rather than silently dropping the filter: the SDK only knows
			# these two, and a payment taken by an unexpected method breaks
			# reconciliation. Same reasoning as the pm filter on payrexx_web.
			return DriverResponse(
				status="failed",
				error_code="unsupported_payment_method",
				error_message=_("Payrexx Tap to Pay accepts CARD or TWINT, got {0}").format(method),
			)

		return DriverResponse(
			status="requires_action",
			# Empty on purpose — the Payrexx transaction does not exist yet. The
			# webhook fills it in, and the scheduler tolerates the gap.
			provider_intent_id=None,
			next_action_type="native_app_handoff",
			next_action_payload={
				"handoff": "payrexx_taptopay",
				# The Android intent the mobile app hands off to, so a client does not
				# have to hard-code it.
				"android_intent": "com.payrexx.taptopay.SOFTPOS",
				# What the SDK's Sale needs. Amounts stay in minor units; the native
				# module converts to the float the SDK wants.
				"order_reference": request.intent_name,
				"amount": request.amount,
				"tip_amount": request.metadata.get("tip_amount") or 0,
				"currency": request.currency,
				"payment_method": str(method).upper() if method else None,
				# False keeps the Payrexx result screen out of the way so our own
				# receipt is the only one the customer sees. See decision 3 in
				# 03-Tap-To-Pay-Mobile.
				"show_result": bool(request.metadata.get("show_result") or False),
			},
		)

	def confirm_intent(self, provider_intent_id: str, **kwargs: Any) -> DriverResponse:
		"""Not applicable — the Payrexx app confirms, on the phone.

		Answered as a status read so a caller that confirms generically gets a useful
		answer instead of an error.
		"""
		return self.get_status(provider_intent_id)

	def get_status(self, provider_intent_id: str) -> DriverResponse:
		"""Read the transaction back, by id or by intent name.

		``processing`` is returned when nothing is found yet — that is the accurate
		reading, not a failure: the operator may simply not have tapped a card. A
		payment that never happens is closed out by ``cancel_intent`` or by the
		intent's own timeout, never by guessing here.
		"""
		try:
			transaction = self._find_transaction(provider_intent_id)
		except Exception as exc:  # noqa: BLE001 - contract: never raise upward
			return error_response(exc, provider_intent_id=provider_intent_id)

		if transaction is None:
			return DriverResponse(
				status="processing",
				provider_intent_id=None,
				next_action_type="native_app_handoff",
				next_action_payload={"handoff": "payrexx_taptopay", "awaiting_tap": True},
			)

		target = map_status(str(transaction.status) if transaction.status else None)
		return DriverResponse(
			status=target or "processing",
			provider_intent_id=str(transaction.id) if transaction.id else None,
			raw=transaction.raw,
		)

	def cancel_intent(self, provider_intent_id: str) -> DriverResponse:
		"""Cancel only while nothing has been charged.

		Two very different situations, and conflating them would be the dangerous
		part:

		- **no transaction yet** — the operator abandoned before presenting a card.
		  Nothing was charged, so the intent is legitimately ``canceled``.
		- **a transaction exists** — money has moved. There is no server-side way to
		  reverse a Tap to Pay payment mid-flight; a void or refund has to happen, and
		  the void is only available inside the Payrexx app. Refusing with that said
		  plainly beats reporting a cancellation that did not occur.
		"""
		try:
			transaction = self._find_transaction(provider_intent_id)
		except Exception as exc:  # noqa: BLE001
			return error_response(exc, provider_intent_id=provider_intent_id)

		if transaction is None:
			return DriverResponse(status="canceled", provider_intent_id=None)

		return DriverResponse(
			status=map_status(str(transaction.status) if transaction.status else None) or "processing",
			provider_intent_id=str(transaction.id) if transaction.id else None,
			error_code="cancel_not_possible",
			error_message=_(
				"This Tap to Pay payment already exists at Payrexx (transaction {0}). "
				"Refund it, or void it from the Payrexx Tap to Pay app — a void cannot "
				"be done from the server."
			).format(transaction.id),
			raw=transaction.raw,
		)

	def refund(self, provider_intent_id: str, amount: int | None = None) -> DriverResponse:
		"""Refund over REST — a Tap to Pay transaction is an ordinary transaction.

		Note the asymmetry with the SDK: the app offers both ``Void`` and ``Refund``,
		but a void is all-or-nothing, card-only, and only reachable from the app. From
		here, only a refund is possible — which is also the one that works on TWINT.
		"""
		try:
			transaction = self._find_transaction(provider_intent_id)
			if transaction is None:
				return DriverResponse(
					status="failed",
					provider_intent_id=provider_intent_id,
					error_code="transaction_not_found",
					error_message=_("No Payrexx transaction found for {0}").format(provider_intent_id),
				)
			with self._client() as client:
				refunded = client.transaction.refund(transaction.id, amount=amount)
		except Exception as exc:  # noqa: BLE001
			return error_response(exc, provider_intent_id=provider_intent_id)

		return DriverResponse(
			status=map_status(str(refunded.status) if refunded.status else None) or "processing",
			provider_intent_id=str(transaction.id) if transaction.id else None,
			raw=refunded.raw,
		)

	def handle_webhook(self, payload: bytes, headers: dict[str, str]) -> WebhookResult:
		"""Verify and parse a transaction webhook.

		Shared with the other two Payrexx drivers — one webhook stream serves every
		channel, distinguished by ``transaction.type``. See
		:mod:`payments.api.webhook_payrexx` for the dispatch.
		"""
		from payments.api.webhook_payrexx import parse_delivery

		return parse_delivery(
			payload, headers, signing_key=self._payrexx_provider.webhook_signing_key
		)


def resolve_intent_by_reference(order_reference: str) -> str | None:
	"""Find the Payment Intent a Tap to Pay payment belongs to.

	Kept here rather than inline in the webhook because it is the one place that
	encodes our assumption about the link. If the ``order_reference`` chaining turns
	out not to hold, this is the function that changes, and its callers do not.
	"""
	if not order_reference:
		return None
	if frappe.db.exists("Payment Intent", order_reference):
		return order_reference
	# The phone may have sent something else as the reference (a POS Invoice name,
	# for instance). Fall back to a lookup on the intent's own reference fields.
	return frappe.db.get_value(
		"Payment Intent",
		{"channel": CHANNEL, "reference_name": order_reference},
		"name",
	)
