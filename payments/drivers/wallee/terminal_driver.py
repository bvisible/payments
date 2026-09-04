#//// Neoffice — added file (no upstream equivalent). Wallee Terminal driver,
#//// server-driven through the Wallee Python SDK and mirroring the Stripe Terminal
#//// two-step flow; state changes arrive by webhook with a polling fallback.
#//// Upstream has neither Wallee nor any physical-terminal concept. Note the shape
#//// this imposes: Wallee exposes no idempotency-key header, so uniqueness rests on
#//// `merchant_reference` = the Frappe Payment Intent name, and the API layer must
#//// call create_intent at most once per intent.
#//// Commits: 49af519 2026-05-18 "feat(drivers): Wallee Terminal driver (server-driven, webhook + polling fallback)"
#////          99e929c 2026-05-19 "feat(payments): merge wallee_integration into payments — ADR-005"
# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""Wallee Terminal driver — server-driven via the Wallee Python SDK.

Two-step flow, mirroring Stripe Terminal:

1. ``create_intent`` builds a Wallee transaction (``auto_confirmation_enabled=False``
   so it stays PENDING for terminal processing). The Frappe Payment Intent name
   is stored in ``merchant_reference`` so the webhook can map back.
2. ``confirm_intent(reader_id=<wallee_terminal_id>)`` calls
   ``PaymentTerminalsService.post_payment_terminals_id_perform_transaction`` —
   Wallee returns 200 immediately; the actual card-tap happens on the device.
   State changes arrive by webhook (handled in :mod:`payments.api.webhook_wallee`).

Wallee Transaction states (subset we care about):

- ``CREATE``, ``PENDING``, ``CONFIRMED``, ``PROCESSING`` — still in flight (no FSM transition)
- ``AUTHORIZED``, ``COMPLETED``, ``FULFILL``                — ``succeeded``
- ``FAILED``, ``DECLINE``                                   — ``failed``
- ``VOIDED``                                                — ``canceled``

Refund webhooks (``listenerEntityTechnicalName=Refund``) carry their own state:

- ``SUCCESSFUL`` — ``refunded``
- ``FAILED``    — ``failed``

Idempotency keys: Wallee SDK does not expose an idempotency-key header like
Stripe does. For ``create_intent`` we instead rely on ``merchant_reference``
being unique per Frappe Payment Intent — if Wallee receives the same
merchant_reference twice the API does NOT dedupe, so callers must ensure
``create_intent`` is invoked at most once per intent_name (the API layer does).
"""

from __future__ import annotations

import hmac
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
from payments.drivers.wallee.provider import WalleeProvider


# Wallee Transaction state → target Payment Intent FSM status.
_TX_STATE_TO_STATUS: dict[str, str] = {
	"AUTHORIZED": "succeeded",
	"COMPLETED": "succeeded",
	"FULFILL": "succeeded",
	"FAILED": "failed",
	"DECLINE": "failed",
	"VOIDED": "canceled",
}

# Wallee Refund state → target FSM status (when the webhook is a Refund event).
_REFUND_STATE_TO_STATUS: dict[str, str] = {
	"SUCCESSFUL": "refunded",
	"FAILED": "failed",
}

# Transaction states that carry no FSM transition (still in flight).
_TX_PASSTHROUGH: set[str] = {"CREATE", "PENDING", "CONFIRMED", "PROCESSING"}


class WalleeTerminalChannel(PaymentChannelBase):
	code = "terminal"
	capabilities = {
		"supports_refund": True,
		"supports_partial_refund": True,
		"supports_tip": False,
		"async": True,
		"requires_device": True,
	}


class WalleeTerminalDriver(PaymentDriverBase):
	"""Server-driven Wallee Terminal driver."""

	code = "wallee.terminal"

	@classmethod
	def from_docs(cls, provider_doc, channel_doc, binding_doc):  # noqa: ANN001
		provider = WalleeProvider(provider_doc)
		channel = WalleeTerminalChannel()
		return cls(provider, channel, settings_doc=binding_doc)

	# ------------------------------------------------------------------------
	# Internals
	# ------------------------------------------------------------------------

	@property
	def _wallee_provider(self) -> WalleeProvider:
		assert isinstance(self.provider, WalleeProvider)
		return self.provider

	def _binding_config(self) -> dict[str, Any]:
		if self.settings_doc is None or not getattr(self.settings_doc, "config_json", None):
			return {}
		try:
			return json.loads(self.settings_doc.config_json)
		except (ValueError, TypeError):
			return {}

	def _services(self):  # noqa: ANN202 — Wallee SDK service instances
		"""Build (TransactionsService, PaymentTerminalsService, RefundsService).

		Lazy import keeps non-Wallee sites cheap to load.
		"""
		from wallee import TransactionsService
		from wallee.service.payment_terminals_service import PaymentTerminalsService
		from wallee.service.refunds_service import RefundsService

		config = self._wallee_provider.build_configuration()
		return (
			TransactionsService(config),
			PaymentTerminalsService(config),
			RefundsService(config),
		)

	# ------------------------------------------------------------------------
	# Driver contract
	# ------------------------------------------------------------------------

	def create_intent(self, request: IntentRequest) -> DriverResponse:
		"""Create a Wallee transaction in card-present mode.

		Does NOT push it to a terminal yet — that's :meth:`confirm_intent`'s job.
		Returns a ``requires_action`` response with the Wallee transaction id.
		"""
		from wallee import LineItemCreate, LineItemType, TransactionCreate

		# Wallee LineItem amount is in MAJOR units (decimal). Our `amount` is in
		# minor units (rappen). One single line item — the POS invoice itself.
		amount_major = round(request.amount / 100, 2)
		line_items = [
			LineItemCreate(
				name=(request.metadata or {}).get("description")
					or f"POS sale {request.intent_name}",
				unique_id=request.intent_name,
				quantity=1,
				amount_including_tax=amount_major,
				type=LineItemType.PRODUCT,
			)
		]

		# `merchant_reference` is how the webhook maps back to the Frappe Intent.
		# Wallee enforces max 100 chars; Payment Intent names are <30 chars so we're safe.
		merchant_reference = request.intent_name

		tx_create = TransactionCreate(
			line_items=line_items,
			currency=request.currency.upper(),
			# Stay PENDING — terminal processing will transition it.
			auto_confirmation_enabled=False,
			merchant_reference=merchant_reference,
		)

		try:
			tx_service, _terminals, _refunds = self._services()
			response = tx_service.post_payment_transactions(
				self._wallee_provider.space_id, tx_create
			)
		except Exception as exc:  # noqa: BLE001 — Wallee SDK raises generic Exception subclasses
			friendly_msg, error_code = _extract_wallee_error(exc)
			return DriverResponse(
				status="failed",
				error_code=error_code or "wallee_create_failed",
				error_message=friendly_msg,
				raw={"exception": repr(exc)},
			)

		state_value = _state_value(response.state)
		next_action_payload: dict[str, Any] = {
			"requires_attach_to_reader": True,
			"wallee_state": state_value,
			"hint": "call confirm_intent(reader_id=<wallee_terminal_id>) to push to the terminal",
		}
		return DriverResponse(
			status="requires_action",
			provider_intent_id=str(response.id),
			next_action_type="display_card_present_modal",
			next_action_payload=next_action_payload,
			raw=response.to_dict() if hasattr(response, "to_dict") else {},
		)

	def confirm_intent(self, provider_intent_id: str, **kwargs: Any) -> DriverResponse:
		"""Push the transaction to a Wallee terminal.

		Required kwarg: ``reader_id`` (Wallee Terminal ID, integer-as-str — taken
		from ``Payment Device.provider_device_id``). Optional alias ``terminal_id``
		is accepted for clarity at call sites that build their own kwargs.
		"""
		reader_id = kwargs.get("reader_id") or kwargs.get("terminal_id")
		if not reader_id:
			return DriverResponse(
				status="failed",
				provider_intent_id=provider_intent_id,
				error_code="missing_reader_id",
				error_message="confirm_intent requires reader_id kwarg",
			)
		try:
			terminal_id_int = int(reader_id)
			transaction_id_int = int(provider_intent_id)
		except (TypeError, ValueError):
			return DriverResponse(
				status="failed",
				provider_intent_id=provider_intent_id,
				error_code="invalid_id",
				error_message=f"reader_id ({reader_id!r}) and provider_intent_id ({provider_intent_id!r}) must be integer-castable",
			)

		try:
			_tx, terminals, _refunds = self._services()
			# Note Wallee SDK signature: (terminal_id, transaction_id, space_id)
			response = terminals.post_payment_terminals_id_perform_transaction(
				terminal_id_int,
				transaction_id_int,
				self._wallee_provider.space_id,
			)
		except Exception as exc:  # noqa: BLE001
			# Wallee ApiException carries the full HTTP response (headers + body)
			# in str(exc). The actionable bit is the body's "message=" — extract
			# it so the cashier sees something readable instead of an HTTP dump.
			# Also expose Wallee's error code (e.g. long_polling_timeout, ...).
			friendly_msg, error_code = _extract_wallee_error(exc)
			return DriverResponse(
				status="failed",
				provider_intent_id=provider_intent_id,
				error_code=error_code or "wallee_perform_failed",
				error_message=friendly_msg,
				raw={"exception": repr(exc)},
			)

		# Wallee returned 200 — the terminal is now displaying the prompt.
		return DriverResponse(
			status="processing",
			provider_intent_id=provider_intent_id,
			next_action_type="display_card_present_modal",
			next_action_payload={
				"terminal_id": terminal_id_int,
				"terminal_action_status": "in_progress",
			},
			raw=response.to_dict() if hasattr(response, "to_dict") else {},
		)

	def cancel_intent(self, provider_intent_id: str) -> DriverResponse:
		"""Void the Wallee transaction. Idempotent (already-voided is OK)."""
		try:
			tx_service, _terminals, _refunds = self._services()
			response = tx_service.post_payment_transactions_id_void_online(
				int(provider_intent_id), self._wallee_provider.space_id
			)
		except Exception as exc:  # noqa: BLE001
			# Wallee returns 4xx if the transaction is already in a terminal
			# state (COMPLETED/VOIDED/FAILED). Treat as canceled — the FSM
			# transition is no-op when already in the target state.
			msg = str(exc)
			if "already" in msg.lower() or "VOIDED" in msg or "FAILED" in msg:
				return DriverResponse(status="canceled", provider_intent_id=provider_intent_id)
			return DriverResponse(
				status="failed",
				provider_intent_id=provider_intent_id,
				error_code="wallee_void_failed",
				error_message=msg,
			)
		return DriverResponse(
			status="canceled",
			provider_intent_id=provider_intent_id,
			raw=response.to_dict() if hasattr(response, "to_dict") else {},
		)

	def refund(self, provider_intent_id: str, amount: int | None = None) -> DriverResponse:
		"""Refund the Wallee transaction. ``amount=None`` → full refund.

		Wallee ``RefundCreate.amount`` is in MAJOR units (decimal). If amount is
		omitted, we re-read the transaction's ``authorized_amount`` to know the
		full amount.
		"""
		from wallee.models import RefundCreate

		try:
			tx_service, _terminals, refunds = self._services()

			refund_amount: float
			if amount is None:
				tx = tx_service.get_payment_transactions_id(
					int(provider_intent_id), self._wallee_provider.space_id
				)
				# `authorized_amount` is a Decimal in major units already on Wallee.
				refund_amount = float(tx.authorized_amount or 0)
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
			return DriverResponse(
				status="failed",
				provider_intent_id=provider_intent_id,
				error_code="wallee_refund_failed",
				error_message=str(exc),
				raw={"exception": repr(exc)},
			)

		# Refund returns SUCCESSFUL/PENDING/FAILED. We optimistically mark refunded
		# if SUCCESSFUL; otherwise the FSM stays where it was and the operator
		# can inspect via Wallee dashboard.
		state_value = _state_value(response.state)
		if state_value == "SUCCESSFUL":
			return DriverResponse(
				status="refunded",
				provider_intent_id=provider_intent_id,
				raw=response.to_dict() if hasattr(response, "to_dict") else {},
			)
		# PENDING / MANUAL_CHECK / FAILED — let the webhook resolve.
		return DriverResponse(
			status="processing",
			provider_intent_id=provider_intent_id,
			error_code=None if state_value != "FAILED" else "refund_failed",
			error_message=f"Refund {response.id} state={state_value}",
			raw=response.to_dict() if hasattr(response, "to_dict") else {},
		)

	def handle_webhook(self, payload: bytes, headers: dict[str, str]) -> WebhookResult:
		"""Verify the Wallee HMAC signature and map the event to an FSM transition.

		Wallee webhook payload shape (JSON):

			{"entityId": 123, "listenerEntityTechnicalName": "Transaction",
			 "state": "COMPLETED", "spaceId": 4242, ...}

		The signature is HMAC-SHA256 of the raw body, hex-encoded, in the
		``X-Signature`` header. Secret comes from ``Wallee Settings.webhook_secret``.

		If the event is a Transaction state change, ``entityId`` IS our
		``provider_intent_id`` — so we look up the Payment Intent by that field
		(no need to re-read merchant_reference).
		"""
		signature = (
			headers.get("X-Signature")
			or headers.get("x-signature")
			or headers.get("HTTP_X_SIGNATURE")
		)
		webhook_secret = self._wallee_provider.webhook_secret
		# Mode test/debug: when no webhook_secret is configured AND the provider is
		# in test mode, we accept the webhook without signature verification. This
		# lets the operator iterate on the UI flow before configuring the secret
		# on the Wallee dashboard. In live mode the secret is mandatory.
		provider_mode = getattr(self.provider.provider_doc, "mode", "live")
		bypass_signature = (provider_mode == "test") and not webhook_secret

		if not bypass_signature:
			if not webhook_secret:
				return WebhookResult(
					event_id="unknown",
					event_type="unknown",
					signature_valid=False,
					error_code="no_webhook_secret",
					error_message="Wallee Settings.webhook_secret is not configured (required in live mode)",
				)
			if not signature:
				return WebhookResult(
					event_id="unknown",
					event_type="unknown",
					signature_valid=False,
					error_code="no_signature_header",
					error_message="X-Signature header missing",
				)
			expected = hmac.new(webhook_secret.encode(), payload, hashlib.sha256).hexdigest()
			if not hmac.compare_digest(expected, signature):
				return WebhookResult(
					event_id="unknown",
					event_type="unknown",
					signature_valid=False,
					error_code="invalid_signature",
					error_message="HMAC mismatch",
				)

		try:
			body = json.loads(payload.decode("utf-8") or "{}")
		except (ValueError, UnicodeDecodeError) as exc:
			return WebhookResult(
				event_id="unknown",
				event_type="unknown",
				signature_valid=True,
				error_code="invalid_payload",
				error_message=str(exc),
			)

		entity_id = body.get("entityId")
		entity_type = body.get("listenerEntityTechnicalName") or "Unknown"
		state_value = _state_value(body.get("state"))
		event_type = f"{entity_type.lower()}.{(state_value or 'unknown').lower()}"
		# Deterministic event_id for dedup (Wallee does not emit a global event id):
		event_id = f"wallee-{entity_type.lower()}-{entity_id}-{(state_value or 'na').lower()}"

		# Map entity + state → target FSM status + look up Frappe Intent.
		frappe_intent_name: str | None = None
		target_status: str | None = None
		error_code: str | None = None
		error_message: str | None = None

		if entity_type == "Transaction":
			target_status = _TX_STATE_TO_STATUS.get(state_value or "")
			if state_value in _TX_PASSTHROUGH:
				target_status = None  # informational only
			# Look up the intent by provider_intent_id == entity_id
			frappe_intent_name = frappe.db.get_value(
				"Payment Intent",
				{"provider_intent_id": str(entity_id), "channel": "terminal", "provider": ["like", "wallee%"]},
				"name",
			)
			# Fallback: try without channel filter (defensive)
			if not frappe_intent_name:
				frappe_intent_name = frappe.db.get_value(
					"Payment Intent",
					{"provider_intent_id": str(entity_id)},
					"name",
				)
			if target_status == "failed":
				# Wallee failure_reason is on the full transaction object — we
				# don't re-fetch here to keep webhook handling cheap; the worker
				# can enrich if needed.
				error_message = f"Wallee transaction {entity_id} ended in {state_value}"
				error_code = state_value.lower() if state_value else None

		elif entity_type == "Refund":
			target_status = _REFUND_STATE_TO_STATUS.get(state_value or "")
			# Refunds reference their parent transaction; the API layer can
			# correlate via the transaction id stored in the Payment Intent. For
			# the webhook receiver, just signal the refund event — looking up
			# the parent transaction would require an extra API call, so we
			# defer that to a subsequent enrichment step.
			frappe_intent_name = None

		excerpt = f"wallee {entity_type} id={entity_id} state={state_value}"

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


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------


_WALLEE_MESSAGE_RE = __import__("re").compile(r"message=['\"]([^'\"]+)['\"]")
_WALLEE_CODE_RE = __import__("re").compile(r"\bcode=['\"]?([\w_\-]+)['\"]?")


def _extract_wallee_error(exc: Exception) -> tuple[str, str | None]:
	"""Pull the user-facing ``message=...`` and ``code=...`` out of a Wallee
	SDK exception. Falls back to a truncated str(exc) when the format does not
	match.
	"""
	raw = str(exc)
	msg_match = _WALLEE_MESSAGE_RE.search(raw)
	code_match = _WALLEE_CODE_RE.search(raw)
	message = msg_match.group(1) if msg_match else raw[:200]
	code = code_match.group(1) if (code_match and code_match.group(1) not in ("None", "")) else None
	# Tag the HTTP status if present in the prefix "(NNN)\nReason:"
	import re as _re
	status_match = _re.match(r"\((\d{3})\)", raw)
	if status_match:
		message = f"[HTTP {status_match.group(1)}] {message}"
	return message, code


def _state_value(state: Any) -> str | None:
	"""Wallee SDK returns either an Enum (with `.value`) or a raw string.

	Tolerate both forms so unit tests + live calls converge on the same string.
	"""
	if state is None:
		return None
	if hasattr(state, "value"):
		return str(state.value)
	return str(state)


# ----------------------------------------------------------------------------
# Scheduler fallback (hooks.py registers this)
# ----------------------------------------------------------------------------


def _finalize_wallee_web_sales_order(intent_doc) -> None:  # noqa: ANN001
	"""Create the webshop Sales Order for a succeeded wallee_web Payment Intent.

	Mirror of the TWINT poller's finalize step. The /wallee/success www page
	is the primary path; this is the fallback for when the buyer closed that
	tab before the transaction settled. Idempotent — ``handle_payment_success``
	short-circuits if the Payment Request is already Paid.
	"""
	if intent_doc.reference_doctype != "Payment Request" or not intent_doc.reference_name:
		return
	try:
		from webshop.controllers.payment_handler import handle_payment_success

		result = handle_payment_success(payment_request_id=intent_doc.reference_name)
		extra = {}
		if result and result.get("status") == "success" and result.get("redirect_to"):
			extra["redirect_to"] = result["redirect_to"]
		# Push the new state so any open POS/checkout tab can react.
		frappe.publish_realtime(
			event=f"payment.intent.{intent_doc.name}.updated",
			message={"intent_name": intent_doc.name, "status": "succeeded", "channel": "wallee_web", **extra},
			after_commit=True,
		)
	except ImportError:
		pass  # webshop app not installed on this site
	except Exception as exc:  # noqa: BLE001
		frappe.log_error(
			"Wallee web poll finalize failed",
			f"intent={intent_doc.name} pr={intent_doc.reference_name}: {exc!r}",
		)


def poll_pending_transactions() -> dict[str, Any]:
	"""Scheduler fallback: refresh Wallee transactions still in flight.

	Runs every 5 minutes. For each Payment Intent in ``requires_action`` or
	``processing`` on the Wallee channel, re-fetches the Wallee transaction and
	applies any state change the webhook may have missed (LAN drop, firewall,
	delivery delay).

	Idempotent — ``transition_to`` is a no-op when the intent is already in the
	target state.
	"""
	from payments.drivers.registry import resolve_driver

	stats = {"checked": 0, "transitioned": 0, "errors": 0}
	# Find all wallee providers (typically `wallee_test` and `wallee_live`).
	wallee_providers = [
		r.name for r in frappe.get_all(
			"Payment Provider",
			filters={"driver_class": ["like", "payments.drivers.wallee.%"], "enabled": 1},
			fields=["name"],
		)
	]
	if not wallee_providers:
		return stats

	# Poll both wallee.terminal and wallee_web — both back the same Wallee
	# transaction state machine, the only difference is which dialog/redirect
	# spawned the intent.
	intents = frappe.get_all(
		"Payment Intent",
		filters={
			"status": ["in", ("requires_action", "processing")],
			"provider": ["in", wallee_providers],
			"channel": ["in", ("terminal", "wallee_web")],
		},
		fields=["name", "provider", "channel", "provider_intent_id"],
		limit=200,
	)
	for row in intents:
		stats["checked"] += 1
		if not row.provider_intent_id:
			continue
		try:
			driver = resolve_driver(row.provider, row.channel)
			# WalleeTerminalDriver._services() returns 3 (tx, terminals,
			# refunds); WalleeWebDriver._services() returns 2 (tx, refunds).
			# Take the first element either way — only tx_service is needed.
			tx_service = driver._services()[0]  # type: ignore[attr-defined]
			tx = tx_service.get_payment_transactions_id(
				int(row.provider_intent_id), driver._wallee_provider.space_id  # type: ignore[attr-defined]
			)
			state_value = _state_value(tx.state)
			target_status = _TX_STATE_TO_STATUS.get(state_value or "")
			if not target_status:
				continue
			intent_doc = frappe.get_doc("Payment Intent", row.name)
			if intent_doc.status == target_status:
				continue
			moved = intent_doc.transition_to(
				target_status,
				event_source="poll",
				payload_excerpt=f"poll: wallee tx {row.provider_intent_id} state={state_value}",
				ignore_invalid=True,
			)
			if moved:
				stats["transitioned"] += 1
			# For webshop (wallee_web) intents, the FSM transition alone does
			# not create the Sales Order — the /wallee/success page normally
			# does. When the buyer closed that tab, this scheduler is the only
			# path left, so finalize the order here too. Idempotent: a second
			# call when the SO already exists is a no-op.
			if moved and target_status == "succeeded" and row.channel == "wallee_web":
				_finalize_wallee_web_sales_order(intent_doc)
		except Exception as exc:  # noqa: BLE001 — keep the loop alive
			stats["errors"] += 1
			frappe.log_error(
				"Wallee poll_pending_transactions",
				f"intent={row.name} tx={row.provider_intent_id}: {exc!r}",
			)

	# Self-heal: a wallee_web intent can be `succeeded` (transitioned by the
	# /wallee/success page or a webhook) yet still have a Draft Payment
	# Request — finalize failed or the buyer's tab closed mid-finalize.
	# Recover those so the Sales Order always gets created.
	#
	# Bounded to the last 24 hours, like the Payrexx and TWINT equivalents. Without
	# an upper bound, a first run on a site with history settles old requests in
	# silence — TWINT did exactly that on osiris, marking two Payment Requests from
	# May as Paid. Past a day, an unfinalised payment is a human's call: it may have
	# been refunded, keyed in by hand, or its order cancelled.
	horizon = frappe.utils.add_to_date(frappe.utils.now_datetime(), hours=-24)
	orphans = frappe.get_all(
		"Payment Intent",
		filters={
			"status": "succeeded",
			"provider": ["in", wallee_providers],
			"channel": "wallee_web",
			"reference_doctype": "Payment Request",
			"modified": [">", horizon],
		},
		fields=["name", "reference_name"],
		limit=100,
	)
	for o in orphans:
		if not o.reference_name:
			continue
		pr_state = frappe.db.get_value(
			"Payment Request", o.reference_name, ["status", "docstatus"], as_dict=True
		)
		if pr_state and pr_state.docstatus == 0 and pr_state.status != "Paid":
			try:
				_finalize_wallee_web_sales_order(frappe.get_doc("Payment Intent", o.name))
				stats["transitioned"] += 1
			except Exception as exc:  # noqa: BLE001
				stats["errors"] += 1
				frappe.log_error("Wallee web orphan finalize", f"intent={o.name}: {exc!r}")

	return stats
