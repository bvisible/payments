# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""TWINT driver — calls the PHP bridge hosted on ``neoservice.neoffice.me``.

Flow (compass §2 + ADR-002):

	Frappe (any client) → requests.post(neoservice/twint.execute) → subprocess php
	twint_bridge.php → twint-ag/sdk → TWINT Merchant API

Driver mapping:

- ``create_intent`` calls ``register_payment`` on the bridge and stores the
  returned ``order_id`` as the provider intent id. The Payment Intent stays in
  ``requires_action`` until the customer scans the QR.
- ``confirm_intent`` is a no-op for TWINT (the customer drives confirmation via
  the TWINT app). The scheduler ``poll_pending_twint_transactions`` watches the
  bridge for status updates and drives the FSM.
- ``cancel_intent`` calls ``cancel_payment``.
- ``refund`` calls ``refund_payment``.
- ``handle_webhook`` is unused (TWINT does not expose Stripe-style webhooks).

The driver does NOT block: ``create_intent`` returns synchronously with the
``next_action_payload`` containing the QR (raw token; the frontend renders it).
"""

from __future__ import annotations

import json
import re
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
from payments.drivers.twint.provider import TwintProvider

# Mapping TWINT bridge transaction_status → unified FSM target.
# The TWINT SDK exposes a state machine that broadly groups into these buckets.
# Values are lower-cased and compared loosely (case-insensitive substring match).
_STATUS_BUCKETS: list[tuple[str, str]] = [
	# (substring to match (lowercased), target unified status). First match wins.
	# --- final success ---
	("order_ok", "succeeded"),  # POS: captured & settled (transaction_status after confirm)
	("success", "succeeded"),
	("merchant_completed", "succeeded"),
	# --- terminal failures (no money captured) ---
	("merchant_abort", "canceled"),  # MERCHANT_ABORT / MERCHANT_ABORTED
	("client_abort", "canceled"),  # CLIENT_ABORT / CLIENT_ABORTED
	("timeout", "canceled"),  # CLIENT_TIMEOUT
	("client_failed", "failed"),
	("failure", "failed"),
	# --- in flight (client not done yet, or awaiting merchant capture) ---
	("confirmation_pending", "processing"),  # ORDER_CONFIRMATION_PENDING → poll captures
	("order_received", "processing"),
	("ordering", "processing"),
	("in_progress", "processing"),
	("pending", "processing"),
	("paired", "processing"),
]


class TwintQRBridgeChannel(PaymentChannelBase):
	code = "qr_bridge"
	capabilities = {
		"supports_refund": True,
		"supports_partial_refund": True,
		"supports_tip": False,
		"async": True,
		"requires_qr_scan": True,
	}


class TwintPHPBridgeDriver(PaymentDriverBase):
	"""TWINT driver going through the centralized PHP bridge on neoservice."""

	code = "twint.qr_bridge"

	@classmethod
	def from_docs(cls, provider_doc, channel_doc, binding_doc):  # noqa: ANN001
		provider = TwintProvider(provider_doc)
		channel = TwintQRBridgeChannel()
		return cls(provider, channel, settings_doc=binding_doc)

	@property
	def _twint_provider(self) -> TwintProvider:
		assert isinstance(self.provider, TwintProvider)
		return self.provider

	def _binding_config(self) -> dict[str, Any]:
		if self.settings_doc is None or not getattr(self.settings_doc, "config_json", None):
			return {}
		try:
			return json.loads(self.settings_doc.config_json)
		except (ValueError, TypeError):
			return {}

	def _resolve_merchant_uuid(self, request_metadata: dict[str, Any] | None) -> str:
		"""Per-call lookup order: metadata → binding config → provider default."""
		md = request_metadata or {}
		return (
			(md.get("twint_merchant_uuid") or "").strip()
			or (self._binding_config().get("default_merchant_uuid") or "").strip()
			or (self._twint_provider.default_merchant_uuid or "").strip()
		)

	def _resolve_merchant_context(self, merchant_uuid: str) -> dict[str, Any]:
		"""Read store_uuid + cash_register_id + environment + P12 password from Twint Bridge Settings.

		The P12 password is stored encrypted on THIS (client) instance and is
		forwarded to neoservice in the request body — neoservice never persists
		it. The P12 file itself lives only on neoservice.
		"""
		if not frappe.db.exists("Twint Bridge Settings", merchant_uuid):
			frappe.throw(_("Twint Bridge Settings record not found for merchant_uuid {0}").format(merchant_uuid))
		doc = frappe.get_doc("Twint Bridge Settings", merchant_uuid)
		return {
			"store_uuid": doc.store_uuid,
			"cash_register_id": doc.cash_register_id or None,
			"environment": doc.environment or "production",
			"certificate_password": doc.get_password("p12_password", raise_exception=False) or "",
		}

	# ------------------------------------------------------------------------
	# HTTP plumbing
	# ------------------------------------------------------------------------

	def _call_bridge(self, command: str, merchant_uuid: str, params: dict[str, Any]) -> dict[str, Any]:
		"""POST to neoservice's neoffice_devops.api.twint.execute and parse the result."""
		import requests

		ctx = self._resolve_merchant_context(merchant_uuid)
		body = {
			"command": command,
			"merchant_uuid": merchant_uuid,
			"certificate_password": ctx["certificate_password"],
			"store_uuid": ctx["store_uuid"],
			"cash_register_id": ctx["cash_register_id"],
			"environment": ctx["environment"],
			"params": json.dumps(params),
		}
		url = f"{self._twint_provider.service_url}/api/method/neoffice_devops.api.twint.execute"
		try:
			resp = requests.post(
				url,
				headers=self._twint_provider._auth_headers(),  # noqa: SLF001 — provider helper
				json=body,
				timeout=70,  # bridge has 60s PHP timeout itself
			)
		except requests.exceptions.RequestException as exc:
			return {"success": False, "error": f"HTTP error: {exc!r}"}

		# Frappe whitelisted endpoints wrap the return in {"message": ...}.
		try:
			data = resp.json()
		except ValueError:
			return {"success": False, "error": f"non-JSON response (status={resp.status_code})"}
		return data.get("message") if isinstance(data, dict) else data

	# ------------------------------------------------------------------------
	# Driver contract
	# ------------------------------------------------------------------------

	def create_intent(self, request: IntentRequest) -> DriverResponse:
		merchant_uuid = self._resolve_merchant_uuid(request.metadata)
		if not merchant_uuid:
			return DriverResponse(
				status="failed",
				error_code="no_merchant_uuid",
				error_message="No TWINT merchant_uuid resolved (metadata, binding, or provider default missing)",
			)
		result = self._call_bridge(
			"register_payment",
			merchant_uuid,
			{
				"amount": int(request.amount),
				"currency": request.currency.upper(),
				"merchant_reference": request.intent_name,
			},
		)
		if not (result or {}).get("success"):
			return DriverResponse(
				status="failed",
				error_code=(result or {}).get("exception") or "twint_register_failed",
				error_message=(result or {}).get("error") or "TWINT register_payment failed",
				raw=result or {},
			)
		order_id = result.get("order_id")
		# TWINT pairing token is the data that the frontend renders as QR code.
		pairing_token = result.get("pairing_token")
		next_action_payload: dict[str, Any] = {
			"twint_order_id": order_id,
			"merchant_reference": request.intent_name,
			"pairing_token": pairing_token,
		}
		return DriverResponse(
			status="requires_action",
			provider_intent_id=order_id,
			next_action_type="display_qr_payload",
			next_action_payload=next_action_payload,
			raw=result,
		)

	def confirm_intent(self, provider_intent_id: str, **kwargs: Any) -> DriverResponse:
		# TWINT confirmation comes from the customer's app, not from our side.
		# We expose the bridge confirm_payment call as a passthrough in case the
		# integration needs to force a confirm step server-side (rare).
		merchant_uuid = kwargs.get("merchant_uuid")
		amount = kwargs.get("amount")
		if not merchant_uuid or not amount:
			return DriverResponse(
				status="processing",
				provider_intent_id=provider_intent_id,
				next_action_type="display_qr_payload",
				next_action_payload={"hint": "confirmation is driven by the TWINT app"},
			)
		result = self._call_bridge(
			"confirm_payment",
			merchant_uuid,
			{"order_id": provider_intent_id, "amount": int(amount)},
		)
		status = self._map_status(result.get("transaction_status")) or "processing"
		return DriverResponse(
			status=status,
			provider_intent_id=provider_intent_id,
			raw=result,
		)

	def cancel_intent(self, provider_intent_id: str) -> DriverResponse:
		# We cannot derive merchant_uuid from provider_intent_id alone; the API
		# layer is responsible for re-routing this call with the right context.
		# For convenience, accept merchant_uuid as a positional hint via the
		# caller, defaulting to a metadata lookup that may fail.
		merchant_uuid = self._guess_merchant_uuid_from_intent(provider_intent_id)
		if not merchant_uuid:
			return DriverResponse(
				status="failed",
				provider_intent_id=provider_intent_id,
				error_code="no_merchant_uuid",
				error_message="cancel_intent requires merchant_uuid context",
			)
		result = self._call_bridge(
			"cancel_payment", merchant_uuid, {"order_id": provider_intent_id}
		)
		if not (result or {}).get("success"):
			return DriverResponse(
				status="failed",
				provider_intent_id=provider_intent_id,
				error_code=(result or {}).get("exception") or "twint_cancel_failed",
				error_message=(result or {}).get("error"),
				raw=result or {},
			)
		return DriverResponse(status="canceled", provider_intent_id=provider_intent_id, raw=result)

	def refund(self, provider_intent_id: str, amount: int | None = None) -> DriverResponse:
		if amount is None:
			return DriverResponse(
				status="failed",
				provider_intent_id=provider_intent_id,
				error_code="amount_required",
				error_message="TWINT reverseOrder requires an explicit amount (partial or full)",
			)
		merchant_uuid = self._guess_merchant_uuid_from_intent(provider_intent_id)
		if not merchant_uuid:
			return DriverResponse(
				status="failed",
				provider_intent_id=provider_intent_id,
				error_code="no_merchant_uuid",
				error_message="refund requires merchant_uuid context",
			)
		result = self._call_bridge(
			"refund_payment",
			merchant_uuid,
			{"order_id": provider_intent_id, "amount": int(amount)},
		)
		if not (result or {}).get("success"):
			return DriverResponse(
				status="failed",
				provider_intent_id=provider_intent_id,
				error_code=(result or {}).get("exception") or "twint_refund_failed",
				error_message=(result or {}).get("error"),
				raw=result or {},
			)
		return DriverResponse(status="refunded", provider_intent_id=provider_intent_id, raw=result)

	def handle_webhook(self, payload: bytes, headers: dict[str, str]) -> WebhookResult:
		# TWINT has no webhooks of this kind. We expose this method so the base
		# class is satisfied; in practice scheduler polling drives the FSM.
		return WebhookResult(
			event_id="not_supported",
			event_type="not_supported",
			signature_valid=False,
			error_code="twint_no_webhooks",
			error_message="TWINT bridge does not push webhooks — use poll_pending_twint_transactions",
		)

	# ------------------------------------------------------------------------
	# Helpers / status mapping
	# ------------------------------------------------------------------------

	def _map_status(self, raw_status: str | None) -> str | None:
		if not raw_status:
			return None
		needle = raw_status.lower()
		for sub, target in _STATUS_BUCKETS:
			if sub in needle:
				return target
		return None

	def _guess_merchant_uuid_from_intent(self, provider_intent_id: str) -> str | None:
		"""Find the Payment Intent record by ``provider_intent_id`` and read merchant_uuid from metadata."""
		# Look up via unique index.
		intent_name = frappe.db.get_value(
			"Payment Intent", {"provider_intent_id": provider_intent_id}, "name"
		)
		if not intent_name:
			return None
		md_json = frappe.db.get_value("Payment Intent", intent_name, "metadata_json") or "{}"
		try:
			md = json.loads(md_json)
		except (ValueError, TypeError):
			md = {}
		return (md.get("twint_merchant_uuid") or "").strip() or None
