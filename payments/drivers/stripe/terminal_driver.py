# //// Neoffice — added file (no upstream equivalent). Stripe Terminal driver in
# //// **server-driven** mode (ADR-003): the server calls api.stripe.com, which
# //// drives the reader over Stripe's own cloud channel, so the cashier's browser
# //// never speaks to the hardware — no mDNS/LAN discovery, which is what broke on
# //// guest WiFi and segmented VLANs with the Wallee JS mode. Upstream `payments`
# //// is a web-checkout hub and has no notion of a physical card terminal.
# //// Commits: 0efe5ef 2026-05-13 "feat(payments): Phase 2 — Stripe Terminal server-driven driver"
# ////          0530096 2026-05-13 "fix(stripe-terminal): body-aware idempotency key (intent_name + sha256[body][:12])"
# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""Stripe Terminal driver — **server-driven** mode.

This driver implements the contract of :class:`PaymentDriverBase` for the
Stripe Terminal product, using only HTTP calls to ``api.stripe.com``. The reader
hardware (BBPOS WisePOS E, Stripe Reader S700, S710) is piloted via Stripe's
cloud channel — the cashier's browser never speaks to the reader directly.

Key behavioural notes (see ADR-003 + compass_artifact §1):

- ``create_intent`` builds a ``PaymentIntent`` with ``payment_method_types=
  ["card_present"]``, ``capture_method="manual"`` so we get auth/capture
  separation for POS use cases. The intent is **not** attached to a reader at
  this point; the API layer will call :meth:`confirm_intent` once the cashier
  picks one.
- ``confirm_intent`` is the actual *attach to reader* call
  (``Reader.process_payment_intent``). Stripe returns 200 immediately and pushes
  the work onto the reader via its cloud channel. The rest comes by webhook.
- ``terminal.reader.action_succeeded`` is **not** the source of truth — it only
  signals that auth succeeded reader-side. We capture, then wait for
  ``payment_intent.succeeded`` to actually mark the Frappe Payment Intent paid.
- ``cancel_intent`` cancels the in-flight reader action and the PaymentIntent;
  ``refund`` issues an online refund (Suisse: Visa/MC/Amex → no card-present
  refund needed, online works fine).

Idempotency keys are scoped per-Frappe-Intent-name so that retrying the same
logical action against Stripe is safe (24h cache window).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import frappe

from payments.drivers.base import (
	DriverResponse,
	IntentRequest,
	PaymentChannelBase,
	PaymentDriverBase,
	WebhookResult,
)
from payments.drivers.stripe.provider import StripeProvider

# Mapping Stripe event type → target FSM status for the Payment Intent.
# Events not in this dict are stored in Webhook Event Log but produce no FSM
# transition.
_EVENT_TO_STATUS: dict[str, str] = {
	"payment_intent.succeeded": "succeeded",
	"payment_intent.payment_failed": "failed",
	"payment_intent.canceled": "canceled",
	"terminal.reader.action_failed": "failed",
	"charge.refunded": "refunded",
}

# Events worth recording but that don't drive the FSM by themselves.
_EVENT_PASSTHROUGH: set[str] = {
	"terminal.reader.action_succeeded",  # auth ok reader-side, capture step will follow
	"terminal.reader.action_updated",
	"payment_intent.created",
	"payment_intent.requires_action",
	"charge.succeeded",
}


class StripeTerminalChannel(PaymentChannelBase):
	code = "terminal"
	capabilities = {
		"supports_refund": True,
		"supports_partial_refund": True,
		"supports_tip": False,  # Tip on receipt screen is US-only on Stripe Terminal.
		"async": True,
		"requires_device": True,
	}


class StripeTerminalDriver(PaymentDriverBase):
	"""Server-driven Stripe Terminal driver."""

	code = "stripe.terminal"

	@classmethod
	def from_docs(cls, provider_doc, channel_doc, binding_doc):  # noqa: ANN001
		provider = StripeProvider(provider_doc)
		channel = StripeTerminalChannel()
		return cls(provider, channel, settings_doc=binding_doc)

	# ------------------------------------------------------------------------
	# Internals
	# ------------------------------------------------------------------------

	@property
	def _stripe(self):
		# Imported lazily so module load stays cheap for sites that don't use Stripe.
		import stripe

		return stripe

	@property
	def _api_key(self) -> str:
		# Type narrowing — provider is always a StripeProvider for this driver.
		assert isinstance(self.provider, StripeProvider)
		return self.provider.secret_key

	def _binding_config(self) -> dict[str, Any]:
		if self.settings_doc is None or not getattr(self.settings_doc, "config_json", None):
			return {}
		try:
			return json.loads(self.settings_doc.config_json)
		except (ValueError, TypeError):
			return {}

	# ------------------------------------------------------------------------
	# Driver contract
	# ------------------------------------------------------------------------

	def create_intent(self, request: IntentRequest) -> DriverResponse:
		"""Create a Stripe PaymentIntent in card-present mode.

		Does NOT push it to a reader yet — that's :meth:`confirm_intent`'s job.
		Returns a ``requires_action`` response with the Stripe intent id and
		client_secret.
		"""
		metadata = {
			"frappe_intent_name": request.intent_name,
			"channel": "pos_terminal",
		}
		if request.reference_doctype and request.reference_name:
			metadata["reference_doctype"] = request.reference_doctype
			metadata["reference_name"] = request.reference_name
		# Free-form caller metadata overrides ours only on conflicting keys with
		# the same name, by design — we trust the caller.
		metadata.update({k: str(v) for k, v in (request.metadata or {}).items()})

		# Idempotency key strategy: scope by intent_name AND a deterministic hash
		# of the body. Stripe rejects 'same key + different body' with HTTP 400;
		# hashing the body ensures network retries (same body) hit the 24h cache
		# whereas an intentional re-run with different metadata gets a fresh key.
		import hashlib

		body_seed = json.dumps(
			{
				"a": request.amount,
				"c": request.currency.lower(),
				"m": {k: str(v) for k, v in metadata.items()},
			},
			sort_keys=True,
		)
		body_hash = hashlib.sha256(body_seed.encode("utf-8")).hexdigest()[:12]
		idempotency_key = f"pi_create_{request.intent_name}_{body_hash}"
		try:
			pi = self._stripe.PaymentIntent.create(
				api_key=self._api_key,
				amount=request.amount,
				currency=request.currency.lower(),
				payment_method_types=["card_present"],
				capture_method="manual",
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

		# Always returned in requires_action. The reader push happens in confirm_intent.
		next_action_payload: dict[str, Any] = {
			"requires_attach_to_reader": True,
			"hint": "call confirm_intent(reader_id=...) to push to the reader",
		}
		return DriverResponse(
			status="requires_action",
			provider_intent_id=pi.id,
			client_secret=pi.client_secret,
			next_action_type="display_card_present_modal",
			next_action_payload=next_action_payload,
			raw=pi.to_dict() if hasattr(pi, "to_dict") else dict(pi),
		)

	def confirm_intent(self, provider_intent_id: str, **kwargs: Any) -> DriverResponse:
		"""Push the PaymentIntent to a reader (server-driven *attach* step).

		Required kwarg: ``reader_id`` (Stripe Terminal Reader id, ``tmr_xxx``).
		Optional kwarg: ``process_config`` (passed through to Stripe verbatim).
		"""
		reader_id = kwargs.get("reader_id")
		if not reader_id:
			return DriverResponse(
				status="failed",
				provider_intent_id=provider_intent_id,
				error_code="missing_reader_id",
				error_message="confirm_intent requires reader_id kwarg",
			)
		process_config = kwargs.get("process_config") or {"enable_customer_cancellation": True}

		try:
			reader = self._stripe.terminal.Reader.process_payment_intent(
				reader_id,
				api_key=self._api_key,
				payment_intent=provider_intent_id,
				process_config=process_config,
				idempotency_key=f"proc_{provider_intent_id}",
			)
		except self._stripe.error.StripeError as exc:
			# `terminal_reader_timeout` is often a false negative (cf. compass §1).
			# We surface as `processing` rather than `failed` so the caller does NOT
			# create a new intent. The webhook loop will eventually resolve it.
			if getattr(exc, "code", None) == "terminal_reader_timeout":
				return DriverResponse(
					status="processing",
					provider_intent_id=provider_intent_id,
					error_code="terminal_reader_timeout",
					error_message="Reader did not ACK in time; reconcile via webhook before retrying",
					raw={"exception": repr(exc)},
				)
			return DriverResponse(
				status="failed",
				provider_intent_id=provider_intent_id,
				error_code=getattr(exc, "code", "stripe_error") or "stripe_error",
				error_message=str(exc),
				raw={"exception": repr(exc)},
			)

		# Stripe returned 200; the reader is now working on it.
		return DriverResponse(
			status="processing",
			provider_intent_id=provider_intent_id,
			next_action_type="display_card_present_modal",
			next_action_payload={"reader_id": reader_id, "reader_action_status": "in_progress"},
			raw=reader.to_dict() if hasattr(reader, "to_dict") else dict(reader),
		)

	def cancel_intent(self, provider_intent_id: str) -> DriverResponse:
		"""Cancel both the reader action (if any) and the PaymentIntent."""
		# Try to find the reader currently attached to this intent; if unknown,
		# just cancel the PaymentIntent.
		reader_id: str | None = None
		try:
			pi = self._stripe.PaymentIntent.retrieve(provider_intent_id, api_key=self._api_key)
			# `latest_charge` won't exist yet at this point; we don't strictly need
			# a reader id for the cancel — Stripe accepts cancel_action with the
			# reader id if known, or PaymentIntent.cancel directly.
			# The Frappe Payment Intent stores the reader, callers can pass it via
			# the API layer; for the driver we play conservative.
		except self._stripe.error.StripeError as exc:
			# If PaymentIntent retrieve fails, we can't safely proceed.
			return DriverResponse(
				status="failed",
				provider_intent_id=provider_intent_id,
				error_code=getattr(exc, "code", "stripe_error") or "stripe_error",
				error_message=str(exc),
			)

		# Cancel the PaymentIntent (this is the safe, idempotent path).
		try:
			self._stripe.PaymentIntent.cancel(provider_intent_id, api_key=self._api_key)
		except self._stripe.error.StripeError as exc:
			# Already canceled is fine.
			if getattr(exc, "code", None) == "payment_intent_unexpected_state":
				return DriverResponse(status="canceled", provider_intent_id=provider_intent_id)
			return DriverResponse(
				status="failed",
				provider_intent_id=provider_intent_id,
				error_code=getattr(exc, "code", "stripe_error") or "stripe_error",
				error_message=str(exc),
			)
		return DriverResponse(status="canceled", provider_intent_id=provider_intent_id)

	def refund(self, provider_intent_id: str, amount: int | None = None) -> DriverResponse:
		"""Refund the PaymentIntent (online — Suisse Visa/MC/Amex, no card-present)."""
		kwargs: dict[str, Any] = {"payment_intent": provider_intent_id}
		if amount is not None:
			kwargs["amount"] = int(amount)
		try:
			refund = self._stripe.Refund.create(
				api_key=self._api_key,
				idempotency_key=f"rf_{provider_intent_id}_{amount or 'full'}",
				**kwargs,
			)
		except self._stripe.error.StripeError as exc:
			return DriverResponse(
				status="failed",
				provider_intent_id=provider_intent_id,
				error_code=getattr(exc, "code", "stripe_error") or "stripe_error",
				error_message=str(exc),
				raw={"exception": repr(exc)},
			)
		return DriverResponse(
			status="refunded",
			provider_intent_id=provider_intent_id,
			raw=refund.to_dict() if hasattr(refund, "to_dict") else dict(refund),
		)

	def handle_webhook(self, payload: bytes, headers: dict[str, str]) -> WebhookResult:
		"""Verify the Stripe signature and map the event to an FSM transition.

		Two invocation modes:
		- **Live webhook** (called from ``handle()`` with real HTTP headers):
		  verify the Stripe-Signature header against the webhook_secret.
		- **Trusted replay** (called from ``process_event()`` worker with
		  empty headers): the signature was already validated at insertion
		  time, so we skip the check and just JSON-parse the payload.

		The signature_valid flag in the returned WebhookResult is True in both
		modes — the worker can't distinguish a re-parse from a fresh call, and
		shouldn't have to.
		"""
		# Stripe-Signature header. We accept any case for the header dict (Werkzeug
		# vs lower-case sources).
		signature = (
			headers.get("Stripe-Signature")
			or headers.get("stripe-signature")
			or headers.get("STRIPE_SIGNATURE")
		)

		if signature:
			# Live mode — verify signature against webhook_secret.
			webhook_secret = self._resolve_webhook_secret()
			if not webhook_secret:
				return WebhookResult(
					event_id="unknown",
					event_type="unknown",
					signature_valid=False,
					error_code="no_webhook_secret",
					error_message="Stripe webhook_secret not configured on this Provider",
				)
			try:
				event = self._stripe.Webhook.construct_event(payload, signature, webhook_secret)
			except self._stripe.error.SignatureVerificationError as exc:
				return WebhookResult(
					event_id="unknown",
					event_type="unknown",
					signature_valid=False,
					error_code="invalid_signature",
					error_message=str(exc),
				)
			except (ValueError, TypeError) as exc:
				return WebhookResult(
					event_id="unknown",
					event_type="unknown",
					signature_valid=False,
					error_code="invalid_payload",
					error_message=str(exc),
				)
		else:
			# Trusted replay mode (worker re-processes a verified row).
			try:
				event = json.loads(payload.decode("utf-8"))
			except (ValueError, UnicodeDecodeError) as exc:
				return WebhookResult(
					event_id="unknown",
					event_type="unknown",
					signature_valid=False,
					error_code="invalid_payload",
					error_message=str(exc),
				)

		event_id = event["id"]
		event_type = event["type"]
		target_status = _EVENT_TO_STATUS.get(event_type)

		# Identify the Frappe Payment Intent. Lookup strategy (in order):
		# 1. obj.metadata.frappe_intent_name (the canonical path for PaymentIntent
		#    events).
		# 2. For terminal.reader.* events, the metadata can sometimes be on
		#    obj.action.payment_intent.metadata (when Stripe expands the PI).
		# 3. For terminal.reader.action_* events, Stripe omits the PI id entirely
		#    from the payload — we fall back to looking up by the reader id
		#    (obj.id) and picking the most recent Payment Intent attached to
		#    that device, still in requires_action/processing. This is the only
		#    way to map a terminal.reader.action_failed event back to its PI.
		obj = event.get("data", {}).get("object", {}) or {}
		frappe_intent_name = (obj.get("metadata") or {}).get("frappe_intent_name")

		if not frappe_intent_name and event_type.startswith("terminal.reader."):
			action = obj.get("action") or {}
			pi_obj = action.get("payment_intent")
			# pi_obj might be a dict (expanded), a string (just the id), or None.
			if isinstance(pi_obj, dict):
				frappe_intent_name = (pi_obj.get("metadata") or {}).get("frappe_intent_name")
			elif isinstance(pi_obj, str) and pi_obj:
				# Direct lookup by Stripe PI id.
				frappe_intent_name = frappe.db.get_value(
					"Payment Intent", {"provider_intent_id": pi_obj}, "name"
				)

			# Final fallback: lookup by reader id (obj.id) — the most recent
			# Payment Intent still attached to that physical device. Bounded by
			# device + non-terminal status to avoid mismatching old intents.
			if not frappe_intent_name and obj.get("id"):
				device_name = frappe.db.get_value(
					"Payment Device", {"provider_device_id": obj["id"]}, "name"
				)
				if device_name:
					rows = frappe.get_all(
						"Payment Intent",
						filters={
							"device": device_name,
							"status": ["in", ("requires_action", "processing")],
						},
						order_by="creation desc",
						limit=1,
						pluck="name",
					)
					if rows:
						frappe_intent_name = rows[0]

		# Build a short excerpt for the Payment Event log.
		excerpt = f"{event_type} pi={obj.get('id') or '<no-id>'} status={obj.get('status') or '?'}"

		# Extract failure detail when available.
		error_code: str | None = None
		error_message: str | None = None
		if target_status == "failed":
			# For payment_intent.* events, the last_payment_error is on the PI.
			last_err = obj.get("last_payment_error") or {}
			error_code = last_err.get("code")
			error_message = last_err.get("message")
			# For terminal.reader.action_failed events, the failure is on
			# obj.action — propagate it for the cashier UI.
			if not error_code and event_type == "terminal.reader.action_failed":
				action = obj.get("action") or {}
				error_code = action.get("failure_code")
				error_message = action.get("failure_message")

		# If event is informational only (passthrough), record it but don't drive FSM.
		if target_status is None and event_type in _EVENT_PASSTHROUGH:
			return WebhookResult(
				event_id=event_id,
				event_type=event_type,
				signature_valid=True,
				intent_name=frappe_intent_name,
				target_status=None,
				payload_excerpt=excerpt[:140],
			)

		return WebhookResult(
			event_id=event_id,
			event_type=event_type,
			signature_valid=True,
			intent_name=frappe_intent_name,
			target_status=target_status,
			error_code=error_code,
			error_message=error_message,
			payload_excerpt=excerpt[:140],
		)

	# ------------------------------------------------------------------------
	# Helpers
	# ------------------------------------------------------------------------

	def _resolve_webhook_secret(self) -> str | None:
		"""Secret lookup order: binding config_json override → provider credentials."""
		override = self._binding_config().get("webhook_secret_override")
		if override:
			return override
		assert isinstance(self.provider, StripeProvider)
		return self.provider.webhook_secret

	# ------------------------------------------------------------------------
	# Post-auth capture helper (called by webhook worker on action_succeeded)
	# ------------------------------------------------------------------------

	def capture_payment(self, provider_intent_id: str) -> DriverResponse:
		"""Capture a previously-authorized PaymentIntent.

		Called by the webhook worker when ``terminal.reader.action_succeeded`` is
		received: the card has authorized, we now capture to actually charge.
		"""
		try:
			pi = self._stripe.PaymentIntent.capture(
				provider_intent_id,
				api_key=self._api_key,
				idempotency_key=f"cap_{provider_intent_id}",
			)
		except self._stripe.error.StripeError as exc:
			# Already captured is fine — return processing and let payment_intent.succeeded close it.
			code = getattr(exc, "code", None)
			if code in {"payment_intent_unexpected_state", "intent_already_captured"}:
				return DriverResponse(status="processing", provider_intent_id=provider_intent_id)
			return DriverResponse(
				status="failed",
				provider_intent_id=provider_intent_id,
				error_code=code or "stripe_error",
				error_message=str(exc),
			)
		return DriverResponse(
			status="processing",  # waits for payment_intent.succeeded webhook for terminal state
			provider_intent_id=provider_intent_id,
			raw=pi.to_dict() if hasattr(pi, "to_dict") else dict(pi),
		)
