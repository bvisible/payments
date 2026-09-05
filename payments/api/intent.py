# //// Neoffice — added file (no upstream equivalent). The Payment Intent lifecycle API —
# //// create / status / cancel / refund — uniform across PSPs: it resolves a driver from
# //// the (provider, channel) binding of ADR-001/004 and drives the single fact table.
# //// Upstream fuses PSP credentials, channel and business logic into one settings
# //// doctype per PSP, so it has no Payment Intent, no driver registry, and no
# //// cross-PSP entry point to expose.
# //// Commits: e32ecf5 2026-05-13 "feat(payments): Phase 1 — unified payment driver layer (Provider × Channel × Driver)"
# ////          78d9240 2026-08-21 "fix(payments): actually release a frozen card reader instead of just reporting it"
# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""Public API for Payment Intent lifecycle.

All endpoints are whitelisted and authenticated. They sit on top of the driver
registry to provide a uniform interface across PSPs.

Endpoints:
- :func:`create_intent` — build a new Payment Intent + call the driver.
- :func:`get_intent_status` — read current FSM state.
- :func:`cancel_intent` — cancel an in-flight intent.
- :func:`refund_intent` — refund a settled intent.
"""

from __future__ import annotations

import json
import time
from typing import Any

import frappe
from frappe import _

from payments.drivers.base import IntentRequest
from payments.drivers.registry import resolve_driver


def _serialize_intent_for_client(intent_doc) -> dict[str, Any]:  # noqa: ANN001
	"""Return a JSON-safe dict suitable for sending to the POS / web frontend."""
	next_action_payload = {}
	if intent_doc.next_action_payload:
		try:
			next_action_payload = json.loads(intent_doc.next_action_payload)
		except (ValueError, TypeError):
			next_action_payload = {}
	return {
		"intent_name": intent_doc.name,
		"status": intent_doc.status,
		"amount": intent_doc.amount,
		"currency": intent_doc.currency,
		"provider": intent_doc.provider,
		"channel": intent_doc.channel,
		"device": intent_doc.device,
		"provider_intent_id": intent_doc.provider_intent_id,
		"client_secret": intent_doc.client_secret,
		"next_action_type": intent_doc.next_action_type,
		"next_action_payload": next_action_payload,
		"error_code": intent_doc.error_code,
		"error_message": intent_doc.error_message,
	}


@frappe.whitelist()
def create_intent(
	provider: str,
	channel: str,
	amount: int,
	currency: str,
	reference_doctype: str | None = None,
	reference_name: str | None = None,
	device: str | None = None,
	metadata: dict[str, Any] | str | None = None,
) -> dict[str, Any]:
	"""Create a Payment Intent and request its creation at the provider.

	Returns a dict ready to be consumed by the frontend (intent_name + next_action).
	Raises ``frappe.ValidationError`` or ``DriverResolutionError`` on misconfiguration.
	"""
	amount = int(amount)
	currency = (currency or "").strip().upper()
	if amount <= 0:
		frappe.throw(_("amount must be > 0"))

	# Normalize metadata: accept dict or JSON string from HTTP.
	if isinstance(metadata, str):
		try:
			metadata_dict = json.loads(metadata) if metadata else {}
		except (ValueError, TypeError) as exc:
			frappe.throw(_("metadata is not valid JSON: {0}").format(exc))
	else:
		metadata_dict = metadata or {}
	if not isinstance(metadata_dict, dict):
		frappe.throw(_("metadata must be a JSON object"))

	# Find the binding so we can stamp it on the Intent for traceability.
	binding_name = frappe.db.get_value(
		"Provider Channel Settings", {"provider": provider, "channel": channel}, "name"
	)

	# 1. Create the Payment Intent in `requires_action` state.
	intent_doc = frappe.get_doc(
		{
			"doctype": "Payment Intent",
			"status": "requires_action",
			"provider": provider,
			"channel": channel,
			"provider_channel_settings": binding_name,
			"device": device,
			"amount": amount,
			"currency": currency,
			"reference_doctype": reference_doctype,
			"reference_name": reference_name,
			"metadata_json": json.dumps(metadata_dict) if metadata_dict else None,
		}
	).insert(ignore_permissions=True)

	# 2. Resolve the driver and build an IntentRequest.
	driver = resolve_driver(provider, channel)
	device_id = None
	if device:
		device_id = frappe.db.get_value("Payment Device", device, "provider_device_id")
	request = IntentRequest(
		intent_name=intent_doc.name,
		amount=amount,
		currency=currency,
		reference_doctype=reference_doctype,
		reference_name=reference_name,
		device_id=device_id,
		metadata=metadata_dict,
	)

	# 3. Ask the driver to create the provider-side intent.
	# Errors are logged and surfaced; the Frappe Intent stays in requires_action so
	# the caller can retry without producing a duplicate. The provider-side ID is
	# stamped only on success.
	try:
		response = driver.create_intent(request)
	except Exception as exc:  # noqa: BLE001 — driver-agnostic catch
		frappe.log_error("create_intent driver error", f"Intent {intent_doc.name}: {exc!r}")
		intent_doc.transition_to(
			"failed",
			event_source="api",
			error_code="driver_exception",
			error_message=str(exc)[:140],
			ignore_invalid=True,
		)
		raise

	# 4. Persist driver response back to the Payment Intent.
	updates: dict[str, Any] = {
		"provider_intent_id": response.provider_intent_id,
		"client_secret": response.client_secret,
		"next_action_type": response.next_action_type,
		"next_action_payload": (
			json.dumps(response.next_action_payload) if response.next_action_payload else None
		),
		"error_code": response.error_code,
		"error_message": response.error_message,
	}
	for k, v in updates.items():
		setattr(intent_doc, k, v)
	intent_doc.save(ignore_permissions=True)

	# 5. Drive the FSM based on the driver's reported status.
	intent_doc.transition_to(
		response.status,
		event_source="api",
		error_code=response.error_code,
		error_message=response.error_message,
		ignore_invalid=True,
	)

	# Reload to get the final state (transition_to() saves).
	intent_doc.reload()
	return _serialize_intent_for_client(intent_doc)


@frappe.whitelist()
def get_intent_status(intent_name: str) -> dict[str, Any]:
	"""Return the current state of a Payment Intent."""
	intent_doc = frappe.get_doc("Payment Intent", intent_name)
	return _serialize_intent_for_client(intent_doc)


@frappe.whitelist()
def cancel_intent(intent_name: str, reason: str | None = None) -> dict[str, Any]:
	"""Cancel a Payment Intent. No-op if already in a terminal state.

	``reason`` records WHY it was cancelled. Without it, a cancellation the
	cashier deliberately triggered and one that merely ran out of time are
	indistinguishable afterwards — and they call for opposite follow-ups.
	"""
	intent_doc = frappe.get_doc("Payment Intent", intent_name)
	if intent_doc.status in {"succeeded", "failed", "canceled", "refunded"}:
		return _serialize_intent_for_client(intent_doc)

	driver = resolve_driver(intent_doc.provider, intent_doc.channel)
	if intent_doc.provider_intent_id:
		try:
			driver.cancel_intent(intent_doc.provider_intent_id)
		except Exception as exc:  # noqa: BLE001
			# The PSP refused to cancel. The commonest reason is the one that
			# costs money: the intent settled a moment ago and the webhook has
			# not landed yet — a PSP will not cancel a paid transaction.
			#
			# We used to mark it `canceled` anyway "to avoid orphans". That is
			# backwards: `canceled` is TERMINAL, so it buries a real payment as
			# cancelled — money taken, sale never recorded, and the till tells
			# the cashier to start over, charging the customer twice. An orphan
			# is merely untidy and gets flagged within the hour by
			# `pos_next.tasks.detect_uncollected_payments`; a wrongly-cancelled
			# payment is invisible.
			#
			# So: leave the status alone and let the webhook/poll settle it.
			# The caller must check the returned status rather than assume the
			# cancellation happened.
			frappe.log_error(
				"cancel_intent refused by PSP — status left untouched",
				f"Intent {intent_name} (status={intent_doc.status}, "
				f"provider_intent_id={intent_doc.provider_intent_id}): {exc!r}\n\n"
				"NOT marked canceled on purpose: the intent may have just been paid.",
			)
			# Leaving the status alone protects the books, but on a card terminal it
			# leaves the *hardware* frozen on "present your card" — the reader is not
			# released until someone cancels it, and the cashier has already pressed
			# the only button there is. So keep insisting in the background instead of
			# waiting up to five minutes for the scheduler.
			if intent_doc.channel == "terminal":
				frappe.enqueue(
					"payments.api.intent.release_stuck_terminal",
					queue="short",
					intent_name=intent_name,
					enqueue_after_commit=True,
				)
			intent_doc.reload()
			return _serialize_intent_for_client(intent_doc)

	intent_doc.transition_to(
		"canceled",
		event_source="api",
		error_code=reason or None,
		ignore_invalid=True,
	)
	intent_doc.reload()
	return _serialize_intent_for_client(intent_doc)


#: How hard to insist on releasing a frozen reader, and how long between tries.
#:
#: A NexGo N86 on 4G took 8 s to ~40 s to acknowledge a cancellation (measured
#: 2026-08-21), and sometimes ignored the first one outright. Six rounds of 10 s
#: covers that with room to spare while staying far below any job timeout.
_RELEASE_ROUNDS = 6
_RELEASE_INTERVAL_SECONDS = 10.0


def release_stuck_terminal(intent_name: str) -> None:
	"""Keep cancelling until the reader is actually free.

	A card terminal that was asked for a payment stays frozen on "present your
	card" until something cancels it. Cancelling is a *request*: the reply carries
	the payment's current state rather than the outcome, an early one can be
	dropped, and confirmation is slow. One attempt is therefore not a cancellation
	— it is the start of one.

	Left alone, the till shows a finished sale while the reader in front of the
	customer still takes cards. That is the whole reason this exists.

	Runs in the background, checks before acting (a payment that settled in the
	meantime must never be cancelled), and gives up quietly — the five-minute
	sweep in :func:`payments.api.webhook_payrexx.poll_pending_payrexx_transactions`
	is the backstop.
	"""
	final = {"succeeded", "failed", "canceled", "refunded"}
	for _round in range(_RELEASE_ROUNDS):
		intent_doc = frappe.get_doc("Payment Intent", intent_name)
		if intent_doc.status in final or not intent_doc.provider_intent_id:
			return

		driver = resolve_driver(intent_doc.provider, intent_doc.channel)
		reader_status = None
		try:
			reading = driver.get_status(
				intent_doc.provider_intent_id, device_id=intent_doc.device
			)
			status = reading.status
			reader_status = str(
				(reading.next_action_payload or {}).get("terminal_status") or ""
			).strip().upper()
		except Exception:  # noqa: BLE001 - a failed read is not a reason to stop
			status = None

		if reader_status == "TERMINATED":
			# The reader is already free, so there is nothing to release — and this is
			# the one state that cannot be read as an outcome: a paid card and a
			# cancelled request both end in TERMINATED, indistinguishably (see
			# NEEDS_HUMAN in payments/drivers/payrexx/_common.py). Cancelling now would
			# stamp `canceled` on what may well be a sale the customer paid for.
			frappe.log_error(
				"Terminal payment ended ambiguously — left for reconciliation",
				f"Intent {intent_name}: the reader reports TERMINATED, which covers "
				"both a completed payment and a cancelled one. Status left untouched; "
				"the webhook or the merchant transaction decides.",
			)
			return

		if status in final:
			# It resolved on its own — record that and leave the reader alone.
			intent_doc.transition_to(
				status,
				event_source="poll",
				payload_excerpt=f"terminal released ({status})",
				ignore_invalid=True,
			)
			frappe.db.commit()
			return

		try:
			response = driver.cancel_intent(intent_doc.provider_intent_id)
		except Exception:  # noqa: BLE001 - unconfirmed; that is what the loop is for
			time.sleep(_RELEASE_INTERVAL_SECONDS)
			continue

		intent_doc.reload()
		intent_doc.transition_to(
			response.status,
			event_source="api",
			payload_excerpt="terminal released after retry",
			ignore_invalid=True,
		)
		_set_device_status(intent_doc.device, "online")
		frappe.db.commit()
		return

	# Out of tries. A reader that acknowledges nothing over this long is not merely
	# slow — it is very likely asleep, off, or off the network, in which case no
	# amount of cancelling will reach it. Record that on the device so the till can
	# warn the cashier *before* the next sale instead of freezing again.
	_set_device_status(frappe.db.get_value("Payment Intent", intent_name, "device"), "offline")
	frappe.db.commit()
	frappe.log_error(
		"Terminal not released after repeated cancellations",
		f"Intent {intent_name}: the reader may still be waiting for a card, and is "
		f"most likely unreachable (asleep, powered off, or off the network). "
		f"Tried {_RELEASE_ROUNDS} times over "
		f"{int(_RELEASE_ROUNDS * _RELEASE_INTERVAL_SECONDS)}s; device marked offline.",
	)


def _set_device_status(device: str | None, status: str) -> None:
	"""Record what we just learned about a reader's reachability.

	Written from the only place that actually finds out: a cancellation either gets
	acknowledged or it does not. Nothing else in the stack talks to the hardware
	often enough to know.
	"""
	if not device:
		return
	if frappe.db.get_value("Payment Device", device, "status") != status:
		frappe.db.set_value("Payment Device", device, "status", status)


@frappe.whitelist()
def refund_intent(intent_name: str, amount: int | None = None) -> dict[str, Any]:
	"""Refund a settled Payment Intent. ``amount=None`` means full refund."""
	intent_doc = frappe.get_doc("Payment Intent", intent_name)
	if intent_doc.status != "succeeded":
		frappe.throw(_("Can only refund a Payment Intent in status 'succeeded' (current: {0})").format(intent_doc.status))
	if not intent_doc.provider_intent_id:
		frappe.throw(_("Payment Intent has no provider_intent_id; cannot refund"))

	driver = resolve_driver(intent_doc.provider, intent_doc.channel)
	response = driver.refund(intent_doc.provider_intent_id, amount=int(amount) if amount else None)

	# Only record a refund the provider actually performed. Transitioning on the call
	# merely returning would mark money as returned that never left — and a customer
	# holding a receipt that says "refunded" while their account says otherwise is the
	# one outcome worse than a plain failure. A Payrexx void demonstrated exactly this
	# on 2026-08-20: it answered 200 with the payment untouched.
	if response.status not in ("refunded", "succeeded"):
		frappe.log_error(
			"Refund refused by provider",
			f"intent={intent_doc.name} provider={intent_doc.provider} "
			f"channel={intent_doc.channel} status={response.status!r} "
			f"code={response.error_code!r} message={response.error_message!r}",
		)
		frappe.throw(
			_("The provider did not perform the refund: {0}").format(
				response.error_message or response.error_code or response.status
			)
		)

	intent_doc.transition_to(
		"refunded",
		event_source="api",
		error_code=response.error_code,
		error_message=response.error_message,
		payload_excerpt=f"{intent_doc.provider} refund -> {response.status}",
		ignore_invalid=True,
	)
	intent_doc.reload()
	return _serialize_intent_for_client(intent_doc)
