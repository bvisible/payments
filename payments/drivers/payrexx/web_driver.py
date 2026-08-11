# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""Payrexx Web driver — hosted payment page (``POST /Gateway/``).

Structurally identical to :mod:`payments.drivers.wallee.web_driver`: create a
gateway, redirect the shopper, land on a return page that finalises the Sales
Order, with the webhook as the authoritative source and a scheduler poll as the
safety net.

1. ``create_intent`` posts a gateway with ``referenceId`` set to the Payment
   Intent name and the return URLs pointing at ``/payrexx/success``. Returns
   ``requires_action`` with ``next_action_type="redirect_to_url"`` and the hosted
   page URL — which Payrexx builds on the instance subdomain
   (``https://<instance>.payrexx.com/?payment=<hash>``), not on ``api.payrexx.com``.
2. The shopper pays and returns to ``/payrexx/success?payment_intent=PI-…``
   (:mod:`payments.www.payrexx.success`), which syncs the status, finalises the
   order and redirects to ``/thank_you``.
3. The transaction webhook drives the same FSM, and covers the case where the
   shopper closes the tab before returning.

.. warning::
   **There is no idempotency.** Two identical ``POST /Gateway/`` calls with the
   same ``referenceId`` create two independent gateways — verified against a live
   account. ``create_intent`` must therefore be called once per Payment Intent;
   the provider gateway id is persisted as ``provider_intent_id`` immediately so a
   retry reads the existing gateway instead of minting a second one.
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
from payments.drivers.payrexx._common import build_client, error_response, map_status
from payments.drivers.payrexx.provider import PayrexxProvider


class PayrexxWebChannel(PaymentChannelBase):
	code = "payrexx_web"
	capabilities = {
		"supports_refund": True,
		"supports_partial_refund": True,
		"supports_tip": False,
		"async": True,
		"requires_device": False,
		"requires_redirect": True,
	}


class PayrexxWebDriver(PaymentDriverBase):
	"""Payrexx hosted payment page driver."""

	code = "payrexx_web"

	@classmethod
	def from_docs(cls, provider_doc, channel_doc, binding_doc):  # noqa: ANN001
		return cls(PayrexxProvider(provider_doc), PayrexxWebChannel(), settings_doc=binding_doc)

	# ------------------------------------------------------------------------
	# Internals
	# ------------------------------------------------------------------------

	@property
	def _payrexx_provider(self) -> PayrexxProvider:
		assert isinstance(self.provider, PayrexxProvider)
		return self.provider

	def _client(self):  # noqa: ANN202
		return build_client(self._payrexx_provider.provider_doc)

	def _config(self) -> dict[str, Any]:
		"""Per-channel knobs from ``Provider Channel Settings.config_json``."""
		if not self.settings_doc:
			return {}
		try:
			return frappe.parse_json(self.settings_doc.get("config_json") or "{}") or {}
		except (ValueError, TypeError):
			return {}

	def _return_urls(self, intent_name: str) -> dict[str, str]:
		base = get_url()
		success = f"{base}/payrexx/success?payment_intent={intent_name}"
		return {
			"success_redirect_url": success,
			# Failure and cancellation land on the same page: it reads the real
			# status off the Payment Intent, so it can explain what happened
			# instead of guessing from which URL the shopper arrived on.
			"failed_redirect_url": success,
			"cancel_redirect_url": success,
		}

	# ------------------------------------------------------------------------
	# Driver contract
	# ------------------------------------------------------------------------

	def create_intent(self, request: IntentRequest) -> DriverResponse:
		"""Create a hosted payment page for this Payment Intent."""
		config = self._config()
		methods = request.metadata.get("payment_methods") or config.get("payment_methods")

		try:
			with self._client() as client:
				gateway = client.gateway.create(
					amount=request.amount,
					currency=request.currency,
					reference_id=request.intent_name,
					purpose=request.metadata.get("purpose")
					or _("Payment {0}").format(request.intent_name),
					payment_methods=list(methods) if methods else None,
					psp=config.get("psp") or None,
					language=request.metadata.get("language") or frappe.local.lang or None,
					look_and_feel_profile=config.get("look_and_feel_profile") or None,
					skip_result_page=config.get("skip_result_page"),
					validity=config.get("validity"),
					**self._return_urls(request.intent_name),
				)
		except Exception as exc:  # noqa: BLE001 - contract: never raise upward
			return error_response(exc)

		# A silently-dropped payment-method filter would let the shopper pay by a
		# method we never recorded, breaking reconciliation. The library encodes it
		# correctly, so this only fires if Payrexx changes behaviour — worth knowing
		# about, not worth failing the payment over.
		if methods and not gateway.filter_was_applied:
			frappe.log_error(
				"Payrexx dropped the payment method filter",
				f"intent={request.intent_name} requested={list(methods)} "
				f"returned={list(gateway.payment_methods)}",
			)

		return DriverResponse(
			status="requires_action",
			provider_intent_id=str(gateway.id) if gateway.id else None,
			next_action_type="redirect_to_url",
			next_action_payload={
				"url": gateway.link,
				# Payrexx also returns an app deep link, which a mobile caller can
				# prefer over the browser URL.
				"app_link": gateway.app_link,
				"hash": gateway.hash,
			},
			raw=gateway.raw,
		)

	def confirm_intent(self, provider_intent_id: str, **kwargs: Any) -> DriverResponse:
		"""Not applicable — the shopper confirms on the hosted page.

		Implemented as a status read so callers that confirm generically get a
		useful answer rather than an error.
		"""
		return self.get_status(provider_intent_id)

	def get_status(self, provider_intent_id: str) -> DriverResponse:
		"""Read the gateway back and map its status onto the FSM."""
		try:
			with self._client() as client:
				gateway = client.gateway.retrieve(provider_intent_id)
		except Exception as exc:  # noqa: BLE001
			return error_response(exc, provider_intent_id=provider_intent_id)

		target = map_status(str(gateway.status) if gateway.status else None)
		return DriverResponse(
			status=target or "processing",
			provider_intent_id=provider_intent_id,
			next_action_type="none" if target else "redirect_to_url",
			next_action_payload={} if target else {"url": gateway.link},
			raw=gateway.raw,
		)

	def cancel_intent(self, provider_intent_id: str) -> DriverResponse:
		"""Delete the gateway so the link stops accepting payments.

		Only meaningful while unpaid — it does not reverse a payment. A paid
		gateway must be refunded instead.
		"""
		try:
			with self._client() as client:
				client.gateway.delete(provider_intent_id)
		except Exception as exc:  # noqa: BLE001
			return error_response(exc, provider_intent_id=provider_intent_id)

		return DriverResponse(status="canceled", provider_intent_id=provider_intent_id)

	def refund(self, provider_intent_id: str, amount: int | None = None) -> DriverResponse:
		"""Refund the transaction behind this gateway.

		The gateway id is not the transaction id: a refund targets the transaction
		Payrexx created when the shopper paid. It is looked up by ``referenceId``,
		which carries our Payment Intent name.
		"""
		try:
			with self._client() as client:
				reference = frappe.db.get_value(
					"Payment Intent", {"provider_intent_id": provider_intent_id}, "name"
				)
				transactions = (
					client.transaction.find_by_reference(reference) if reference else []
				)
				if not transactions:
					return DriverResponse(
						status="failed",
						provider_intent_id=provider_intent_id,
						error_code="transaction_not_found",
						error_message=_(
							"No Payrexx transaction found for reference {0}"
						).format(reference or provider_intent_id),
					)

				# Newest first: a reference can legitimately carry several
				# transactions, since Payrexx does not enforce uniqueness on it.
				target = max(transactions, key=lambda t: t.id or 0)
				refunded = client.transaction.refund(target.id, amount=amount)
		except Exception as exc:  # noqa: BLE001
			return error_response(exc, provider_intent_id=provider_intent_id)

		status = map_status(str(refunded.status) if refunded.status else None)
		return DriverResponse(
			status=status or "processing",
			provider_intent_id=provider_intent_id,
			raw=refunded.raw,
		)

	def handle_webhook(self, payload: bytes, headers: dict[str, str]) -> WebhookResult:
		"""Verify and parse a transaction webhook.

		Shared with the terminal driver — Payrexx sends one webhook stream for all
		channels, distinguished by ``transaction.type``. See
		:mod:`payments.api.webhook_payrexx` for the dispatch.
		"""
		from payments.api.webhook_payrexx import parse_delivery

		return parse_delivery(
			payload, headers, signing_key=self._payrexx_provider.webhook_signing_key
		)
