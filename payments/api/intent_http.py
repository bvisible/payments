# //// Neoffice — added file (no upstream equivalent). maintenance#1732: the HTTP doors of the Payment Intent API.
# //// payments.api.intent.* (and terminal.attach_intent_to_reader) are also the Python API every checkout calls —
# //// the TWINT, Payrexx and Wallee web pivots and the webshop as a guest, the theme's booking, the phone, POSNext
# //// as a cashier — so they cannot check the session user themselves without stopping those payments. Instead,
# //// hooks.override_whitelisted_methods sends the HTTP path of each one here, where the Payment Intent DocType's own
# //// permissions decide; the originals are no longer whitelisted, so a lost override fails closed. Before this, any
# //// signed-in account, a shop customer included, could create, read, cancel or refund any intent by its name.
"""HTTP entry points of :mod:`payments.api.intent` and of :func:`payments.api.terminal.attach_intent_to_reader`.

The paths do not change (``/api/method/payments.api.intent.refund_intent`` still answers): Frappe resolves them
to the functions below through ``override_whitelisted_methods``, for ``/api/method``, ``/api/v2/method`` and the
old ``?cmd=`` alike. Each one answers only an account the Payment Intent DocType permits: ``read`` to read a status,
``create`` to create, ``write`` to cancel, refund or push to a reader. The DocType is checked before the name is
looked up, so an account without the permission learns nothing about which intents exist.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _

from payments.api import intent as intent_api


def require_intent_permission(ptype: str, intent_name: str | None = None) -> None:
	"""Refuse (403) unless the session user has ``ptype`` on Payment Intent, and on this intent when it exists."""
	if not frappe.has_permission("Payment Intent", ptype):
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	if (
		intent_name
		and frappe.db.exists("Payment Intent", intent_name)
		and not frappe.has_permission("Payment Intent", ptype, doc=intent_name)
	):
		frappe.throw(_("Not permitted"), frappe.PermissionError)


@frappe.whitelist(methods=["POST"])
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
	"""HTTP door of :func:`payments.api.intent.create_intent`."""
	require_intent_permission("create")
	return intent_api.create_intent(
		provider=provider,
		channel=channel,
		amount=amount,
		currency=currency,
		reference_doctype=reference_doctype,
		reference_name=reference_name,
		device=device,
		metadata=metadata,
	)


@frappe.whitelist()
def get_intent_status(intent_name: str) -> dict[str, Any]:
	"""HTTP door of :func:`payments.api.intent.get_intent_status`."""
	require_intent_permission("read", intent_name)
	return intent_api.get_intent_status(intent_name)


@frappe.whitelist(methods=["POST"])
def cancel_intent(intent_name: str, reason: str | None = None) -> dict[str, Any]:
	"""HTTP door of :func:`payments.api.intent.cancel_intent`."""
	require_intent_permission("write", intent_name)
	return intent_api.cancel_intent(intent_name, reason=reason)


@frappe.whitelist(methods=["POST"])
def refund_intent(intent_name: str, amount: int | None = None) -> dict[str, Any]:
	"""HTTP door of :func:`payments.api.intent.refund_intent`."""
	require_intent_permission("write", intent_name)
	return intent_api.refund_intent(intent_name, amount=amount)


@frappe.whitelist(methods=["POST"])
def attach_intent_to_reader(intent_name: str, device: str) -> dict[str, Any]:
	"""HTTP door of :func:`payments.api.terminal.attach_intent_to_reader`."""
	from payments.api import terminal

	require_intent_permission("write", intent_name)
	return terminal.attach_intent_to_reader(intent_name, device)
