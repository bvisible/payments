# //// Neoffice — added file (no upstream equivalent). Wallee hosted payment page
# //// (Redirect / Lightbox / iFrame), ported by the ADR-005 fusion of the retired
# //// `wallee_integration` app: it replaces that app's `create_webshop_payment` code
# //// path and its own `Wallee Transaction` DocType, so web checkout becomes just
# //// another Payment Intent on the unified FSM. Refund, cancel and webhook are
# //// shared with the terminal driver — one Wallee transactions API for both channels.
# //// Commits: 99e929c 2026-05-19 "feat(payments): merge wallee_integration into payments — ADR-005"
# ////          d30e585 2026-05-19 "feat(wallee-web): webshop endpoints for the 3 modes (Redirect/Lightbox/iFrame)"
# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""Wallee Web driver — hosted payment page (Redirect / Lightbox / iFrame).

Replaces the legacy ``wallee_integration.api.create_webshop_payment`` flow:
instead of a separate code path with its own ``Wallee Transaction`` DocType, web
checkout is now just another Payment Intent on the unified FSM. The driver:

1. ``create_intent`` builds a Wallee transaction with ``auto_confirmation_enabled=
   True`` and reads back the payment page URL via
   :meth:`get_payment_transactions_id_payment_page_url`. Returns a
   ``requires_action`` response with ``next_action_type="redirect_to_url"`` and
   the page URL in ``next_action_payload``.
2. The customer is redirected to Wallee. After paying they land on
   ``/wallee/success?payment_intent=PI-2026-XXXXX`` (built by
   :mod:`payments.www.wallee_success`), which syncs the Payment Intent status,
   finalises the Sales Order via ``webshop.controllers.payment_handler``, and
   redirects to ``/thank_you``.
3. State changes also arrive via Wallee webhook (same handler as
   :class:`WalleeTerminalDriver`) for non-redirect-based flows or as a fallback.

Refund, cancel and webhook are shared with the terminal driver — both channels
talk to the same Wallee transactions API.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _
from frappe.utils import get_url

from payments.drivers.base import (
	DriverResponse,
	IntentRequest,
	PaymentChannelBase,
	PaymentDriverBase,
	WebhookResult,
)
from payments.drivers.wallee.provider import WalleeProvider
from payments.drivers.wallee.terminal_driver import (
	_extract_wallee_error,
	_state_value,
	_TX_PASSTHROUGH,
	_TX_STATE_TO_STATUS,
	_REFUND_STATE_TO_STATUS,
)


class WalleeWebChannel(PaymentChannelBase):
	code = "wallee_web"
	capabilities = {
		"supports_refund": True,
		"supports_partial_refund": True,
		"supports_tip": False,
		"async": True,
		"requires_device": False,
		"requires_redirect": True,
	}


class WalleeWebDriver(PaymentDriverBase):
	"""Wallee hosted payment page driver (redirect/iframe/lightbox)."""

	code = "wallee_web"

	@classmethod
	def from_docs(cls, provider_doc, channel_doc, binding_doc):  # noqa: ANN001
		provider = WalleeProvider(provider_doc)
		channel = WalleeWebChannel()
		return cls(provider, channel, settings_doc=binding_doc)

	# ------------------------------------------------------------------------
	# Internals (shared shape with terminal driver)
	# ------------------------------------------------------------------------

	@property
	def _wallee_provider(self) -> WalleeProvider:
		assert isinstance(self.provider, WalleeProvider)
		return self.provider

	def _services(self):  # noqa: ANN202
		from wallee import TransactionsService
		from wallee.service.refunds_service import RefundsService

		config = self._wallee_provider.build_configuration()
		return (
			TransactionsService(config),
			RefundsService(config),
		)

	def _build_line_items(self, request: IntentRequest):
		"""Build Wallee LineItemCreate list.

		Callers can pass a structured ``metadata["line_items"]`` (list of dicts
		with ``name``, ``quantity``, ``amount_including_tax``, ``unique_id``,
		``type``, ``sku``, ``taxes``) for granular line items. If absent, we
		emit a single PRODUCT line item with the full amount — sufficient for
		simple checkouts and matches the legacy
		``wallee_integration.api.transaction.create_transaction`` default.
		"""
		from wallee import LineItemCreate, LineItemType, TaxCreate

		metadata = request.metadata or {}
		line_items_input = metadata.get("line_items") if isinstance(metadata, dict) else None

		if line_items_input:
			out = []
			for item in line_items_input:
				type_str = (item.get("type") or "PRODUCT").upper()
				item_type = {
					"PRODUCT": LineItemType.PRODUCT,
					"SHIPPING": LineItemType.SHIPPING,
					"DISCOUNT": LineItemType.DISCOUNT,
					"FEE": LineItemType.FEE,
				}.get(type_str, LineItemType.PRODUCT)

				wallee_taxes = None
				if item.get("taxes"):
					wallee_taxes = [
						TaxCreate(rate=float(t["rate"]), title=t["title"])
						for t in item["taxes"]
					]

				amount = item.get("amount_including_tax") or item.get("amount") or 0
				out.append(
					LineItemCreate(
						name=item.get("name") or "Item",
						quantity=float(item.get("quantity") or 1),
						amount_including_tax=float(amount),
						unique_id=item.get("unique_id") or item.get("sku") or request.intent_name,
						type=item_type,
						sku=item.get("sku"),
						taxes=wallee_taxes,
					)
				)
			return out

		# Fallback: single line item with the full amount (in major units).
		amount_major = round(request.amount / 100, 2)
		label = metadata.get("description") or f"Web sale {request.intent_name}" if isinstance(metadata, dict) else f"Web sale {request.intent_name}"
		return [
			LineItemCreate(
				name=label,
				quantity=1,
				amount_including_tax=amount_major,
				unique_id=request.intent_name,
				type=LineItemType.PRODUCT,
			)
		]

	# ------------------------------------------------------------------------
	# Driver contract
	# ------------------------------------------------------------------------

	def _build_billing_address(self, request: IntentRequest):
		"""Build Wallee AddressCreate from a metadata.billing_address dict.

		Caller passes a dict with keys: email_address, given_name, family_name,
		street, city, postcode, country (ISO 3166-1 alpha-2). Missing keys are
		omitted (Wallee accepts partial addresses for hosted checkout).
		"""
		metadata = request.metadata or {}
		addr_input = metadata.get("billing_address") if isinstance(metadata, dict) else None
		if not addr_input or not isinstance(addr_input, dict):
			return None

		from wallee.models import AddressCreate

		country = (addr_input.get("country") or "").upper() or None
		return AddressCreate(
			email_address=addr_input.get("email_address") or None,
			given_name=addr_input.get("given_name") or None,
			family_name=addr_input.get("family_name") or None,
			street=addr_input.get("street") or None,
			city=addr_input.get("city") or None,
			postcode=addr_input.get("postcode") or None,
			country=country,
		)

	def create_intent(self, request: IntentRequest) -> DriverResponse:
		"""Create a Wallee web transaction and return its payment page URL.

		The customer must be redirected to ``next_action_payload["url"]``. After
		paying they return to ``/wallee/success?payment_intent=<intent_name>``
		(or ``/wallee/failed?...``), see :mod:`payments.www.wallee_success`.

		Metadata keys honoured by the driver:
		- ``line_items`` — see :meth:`_build_line_items`
		- ``billing_address`` — see :meth:`_build_billing_address`
		- ``success_url`` / ``failed_url`` — override the default redirect
		  targets (webshop passes its own with ``?payment_request=<PR>`` so the
		  www page can finalise the Sales Order).
		"""
		from wallee import TransactionCreate

		metadata = request.metadata if isinstance(request.metadata, dict) else {}
		base_url = get_url()
		success_url = metadata.get("success_url") or (
			f"{base_url}/wallee/success?payment_intent={request.intent_name}"
		)
		failed_url = metadata.get("failed_url") or (
			f"{base_url}/wallee/failed?payment_intent={request.intent_name}"
		)

		tx_kwargs: dict[str, Any] = dict(
			line_items=self._build_line_items(request),
			currency=request.currency.upper(),
			# Web checkout: auto-confirm so we can pay right away after the
			# customer enters their card on Wallee's hosted page.
			auto_confirmation_enabled=True,
			merchant_reference=request.intent_name,
			success_url=success_url,
			failed_url=failed_url,
		)
		billing_address = self._build_billing_address(request)
		if billing_address is not None:
			tx_kwargs["billing_address"] = billing_address

		tx_create = TransactionCreate(**tx_kwargs)

		try:
			tx_service, _refunds = self._services()
			response = tx_service.post_payment_transactions(
				self._wallee_provider.space_id, tx_create
			)
		except Exception as exc:  # noqa: BLE001
			friendly_msg, error_code = _extract_wallee_error(exc)
			return DriverResponse(
				status="failed",
				error_code=error_code or "wallee_create_failed",
				error_message=friendly_msg,
				raw={"exception": repr(exc)},
			)

		# Read the hosted payment page URL for the customer redirect.
		try:
			payment_url = tx_service.get_payment_transactions_id_payment_page_url(
				response.id, self._wallee_provider.space_id
			)
		except Exception as exc:  # noqa: BLE001
			friendly_msg, error_code = _extract_wallee_error(exc)
			return DriverResponse(
				status="failed",
				provider_intent_id=str(response.id),
				error_code=error_code or "wallee_page_url_failed",
				error_message=friendly_msg,
				raw={"exception": repr(exc)},
			)

		state_value = _state_value(response.state)
		return DriverResponse(
			status="requires_action",
			provider_intent_id=str(response.id),
			next_action_type="redirect_to_url",
			next_action_payload={
				"url": str(payment_url),
				"wallee_state": state_value,
			},
			raw=response.to_dict() if hasattr(response, "to_dict") else {},
		)

	def confirm_intent(self, provider_intent_id: str, **kwargs: Any) -> DriverResponse:
		"""Web channel has no separate confirm step — the redirect IS the confirm.

		The Sales Order finalisation happens in ``www/wallee_success.py`` once
		Wallee redirects the customer back. Returning ``processing`` here lets
		the caller chain confirm_intent uniformly without special-casing the
		channel.
		"""
		return DriverResponse(status="processing", provider_intent_id=provider_intent_id)

	def cancel_intent(self, provider_intent_id: str) -> DriverResponse:
		"""Void a pending Wallee transaction. Idempotent (already-voided is OK)."""
		try:
			tx_service, _refunds = self._services()
			response = tx_service.post_payment_transactions_id_void_online(
				int(provider_intent_id), self._wallee_provider.space_id
			)
		except Exception as exc:  # noqa: BLE001
			msg = str(exc)
			if "already" in msg.lower() or "VOIDED" in msg or "FAILED" in msg:
				return DriverResponse(status="canceled", provider_intent_id=provider_intent_id)
			friendly_msg, error_code = _extract_wallee_error(exc)
			return DriverResponse(
				status="failed",
				provider_intent_id=provider_intent_id,
				error_code=error_code or "wallee_void_failed",
				error_message=friendly_msg,
			)
		return DriverResponse(
			status="canceled",
			provider_intent_id=provider_intent_id,
			raw=response.to_dict() if hasattr(response, "to_dict") else {},
		)

	def refund(self, provider_intent_id: str, amount: int | None = None) -> DriverResponse:
		"""Refund (full or partial) a settled Wallee transaction.

		Identical semantics to :meth:`WalleeTerminalDriver.refund` — both
		channels back the same Wallee transaction id.
		"""
		from wallee.models import RefundCreate

		try:
			tx_service, refunds = self._services()

			refund_amount: float
			if amount is None:
				tx = tx_service.get_payment_transactions_id(
					int(provider_intent_id), self._wallee_provider.space_id
				)
				refund_amount = float(getattr(tx, "authorized_amount", 0) or 0)
			else:
				refund_amount = round(int(amount) / 100, 2)

			if refund_amount <= 0:
				return DriverResponse(
					status="failed",
					provider_intent_id=provider_intent_id,
					error_code="invalid_amount",
					error_message=f"refund amount must be > 0 (got {refund_amount})",
				)

			refund_create = RefundCreate(
				transaction=int(provider_intent_id),
				amount=refund_amount,
				external_id=f"pi_{provider_intent_id}_rf_{int(refund_amount * 100)}",
				type="MERCHANT_INITIATED_ONLINE",
			)
			response = refunds.refund(self._wallee_provider.space_id, refund_create)
		except Exception as exc:  # noqa: BLE001
			friendly_msg, error_code = _extract_wallee_error(exc)
			return DriverResponse(
				status="failed",
				provider_intent_id=provider_intent_id,
				error_code=error_code or "wallee_refund_failed",
				error_message=friendly_msg,
				raw={"exception": repr(exc)},
			)

		state_value = _state_value(response.state)
		if state_value == "SUCCESSFUL":
			return DriverResponse(
				status="refunded",
				provider_intent_id=provider_intent_id,
				raw=response.to_dict() if hasattr(response, "to_dict") else {},
			)
		return DriverResponse(
			status="processing",
			provider_intent_id=provider_intent_id,
			error_code=None if state_value != "FAILED" else "refund_failed",
			error_message=f"Refund {response.id} state={state_value}",
			raw=response.to_dict() if hasattr(response, "to_dict") else {},
		)

	def handle_webhook(self, payload: bytes, headers: dict[str, str]) -> WebhookResult:
		"""Webhook handling is identical to the terminal driver.

		Wallee sends the same event shape regardless of how the transaction was
		created (web checkout vs terminal). We delegate to the terminal driver's
		implementation rather than duplicating the parsing + signature logic.
		"""
		# Reuse the terminal driver's implementation by instantiating it
		# transiently with the same provider/channel/binding. Cheap (no SDK
		# call, no DB hit beyond what we'd do anyway).
		from payments.drivers.wallee.terminal_driver import WalleeTerminalDriver

		terminal_view = WalleeTerminalDriver(self.provider, self.channel, self.settings_doc)
		return terminal_view.handle_webhook(payload, headers)
