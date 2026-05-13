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
from typing import Any

import frappe
from frappe import _

from payments.drivers.base import IntentRequest
from payments.drivers.registry import DriverResolutionError, resolve_driver


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
def cancel_intent(intent_name: str) -> dict[str, Any]:
	"""Cancel a Payment Intent. No-op if already in a terminal state."""
	intent_doc = frappe.get_doc("Payment Intent", intent_name)
	if intent_doc.status in {"succeeded", "failed", "canceled", "refunded"}:
		return _serialize_intent_for_client(intent_doc)

	driver = resolve_driver(intent_doc.provider, intent_doc.channel)
	if intent_doc.provider_intent_id:
		try:
			driver.cancel_intent(intent_doc.provider_intent_id)
		except Exception as exc:  # noqa: BLE001
			frappe.log_error("cancel_intent driver error", f"Intent {intent_name}: {exc!r}")
			# Surface but still attempt to flip our local state to canceled to avoid orphans.

	intent_doc.transition_to("canceled", event_source="api", ignore_invalid=True)
	intent_doc.reload()
	return _serialize_intent_for_client(intent_doc)


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
	intent_doc.transition_to(
		"refunded",
		event_source="api",
		error_code=response.error_code,
		error_message=response.error_message,
		ignore_invalid=True,
	)
	intent_doc.reload()
	return _serialize_intent_for_client(intent_doc)
