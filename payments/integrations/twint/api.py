# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""TWINT webshop helpers — pivot between ``payment_handler.create_payment_request``
and the unified ``payments.api.intent.create_intent``.

Mirrors :mod:`payments.integrations.wallee.api` (added in Phase 10). Two
endpoints are whitelisted for guest checkout:

- :func:`create_web_transaction` — pivot called by the JS dialog right after the
  generic ``payment_handler.create_payment_request`` succeeds; creates the
  Payment Intent on the ``twint_web`` channel and returns the QR SVG + pairing
  token for the buyer to scan.
- :func:`get_intent_state` — lightweight fallback the JS can poll if the
  SocketIO subscription drops mid-flow (the realtime push remains the primary
  status-change signal — see :func:`payments.api.twint.fast_poll_intent`).
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _


def _resolve_twint_provider_name(provider: str | None = None) -> str:
	"""Return a TWINT Payment Provider name — explicit, else first enabled."""
	if provider:
		return provider
	name = frappe.db.get_value(
		"Payment Provider",
		{"driver_class": ["like", "payments.drivers.twint.%"], "enabled": 1},
		"name",
		order_by="modified desc",
	)
	if not name:
		frappe.throw(_("No enabled TWINT Payment Provider found"))
	return name


def _resolve_merchant_uuid_for_provider(provider_name: str) -> str | None:
	"""First Twint Bridge Settings record linked to the given Payment Provider.

	Falls back to the most-recently-modified ``Twint Bridge Settings`` row when
	no explicit link exists (single-merchant deployments on Osiris).
	"""
	linked = frappe.db.get_value(
		"Twint Bridge Settings",
		{"linked_payment_provider": provider_name},
		"name",
		order_by="modified desc",
	)
	if linked:
		return linked
	# Fallback: any record (typical single-merchant deployment).
	return frappe.db.get_value("Twint Bridge Settings", {}, "name", order_by="modified desc")


@frappe.whitelist(allow_guest=True)
def create_web_transaction(
	payment_request_id: str,
	provider: str | None = None,
) -> dict[str, Any]:
	"""Webshop pivot — create the Payment Intent via :class:`TwintWebDriver`.

	Wraps :func:`payments.api.intent.create_intent` to align the response shape
	with what ``payments/public/js/twint_dialog.js`` expects. The PHP bridge on
	neoservice handles the actual TWINT SDK call (``Client::startOrder``); we
	wrap the returned pairing token as an inline SVG QR on the way out.

	Args:
	  payment_request_id: name of the Frappe Payment Request created by
	    ``webshop.controllers.payment_handler.create_payment_request``
	  provider: optional TWINT Payment Provider name; first enabled otherwise.

	Returns: ``{status, payment_request_id, payment_intent, transaction_id,
	  qr_svg, pairing_token}``.
	"""
	from payments.api.intent import create_intent

	pr = frappe.get_doc("Payment Request", payment_request_id)
	if pr.payment_request_type != "Inward":
		frappe.throw(_("Payment Request {0} is not Inward").format(payment_request_id))

	provider_name = _resolve_twint_provider_name(provider)
	merchant_uuid = _resolve_merchant_uuid_for_provider(provider_name)
	if not merchant_uuid:
		return {
			"status": "error",
			"message": _("No Twint Bridge Settings configured for provider {0}").format(provider_name),
			"payment_request_id": payment_request_id,
		}

	# Amount in minor units (rappen for CHF) — same convention as Stripe/Wallee.
	amount_minor = int(round((pr.grand_total or 0) * 100))

	intent = create_intent(
		provider=provider_name,
		channel="twint_web",
		amount=amount_minor,
		currency=pr.currency,
		reference_doctype="Payment Request",
		reference_name=payment_request_id,
		metadata={
			"twint_merchant_uuid": merchant_uuid,
			"description": f"Webshop {payment_request_id}",
		},
	)

	if intent.get("status") in ("failed", "canceled"):
		return {
			"status": "error",
			"message": intent.get("error_message") or _("TWINT transaction creation failed"),
			"payment_request_id": payment_request_id,
		}

	payload = intent.get("next_action_payload") or {}
	return {
		"status": "success",
		"payment_request_id": payment_request_id,
		"payment_intent": intent.get("intent_name"),
		"transaction_id": intent.get("provider_intent_id"),
		"pairing_token": payload.get("pairing_token"),
		"qr_svg": payload.get("qr_svg"),
		"merchant_uuid": merchant_uuid,
		# Convenience for the JS overlay: when the buyer cancels in-app we
		# also want a fallback URL for "back to cart".
		"failed_url": frappe.utils.get_url() + "/cart",
		# Dev-only hook: when the site has ``enable_e2e_simulators=True`` the
		# overlay shows a "simulate success" button so a human (or automated
		# test) can validate the post-payment flow without a real TWINT app.
		"simulators_enabled": bool(frappe.conf.get("enable_e2e_simulators")),
	}


@frappe.whitelist(allow_guest=True)
def get_intent_state(intent_name: str) -> dict[str, Any]:
	"""Lightweight status read for the JS dialog (SocketIO drop fallback)."""
	pi = frappe.get_doc("Payment Intent", intent_name)
	return {
		"intent_name": pi.name,
		"status": pi.status,
		"error_code": pi.error_code,
		"error_message": pi.error_message,
	}


# Canonical list of Swiss bank-issued TWINT app URL schemes (iOS only).
# Ported from the legacy ``twint_integration.api.twint_settings.get_ios_schemes``
# which used to call the TWINT SDK to fetch them dynamically — but the list
# is stable and small enough that hard-coding avoids a PHP round-trip on
# every checkout. Update when TWINT publishes new issuer schemes.
_IOS_SCHEMES: list[dict[str, str]] = [
	{"scheme": "twint-issuer1", "display_name": "UBS TWINT"},
	{"scheme": "twint-issuer2", "display_name": "Raiffeisen TWINT"},
	{"scheme": "twint-issuer3", "display_name": "PostFinance TWINT"},
	{"scheme": "twint-issuer4", "display_name": "ZKB TWINT"},
	{"scheme": "twint-issuer5", "display_name": "Credit Suisse TWINT"},
	{"scheme": "twint-issuer6", "display_name": "BCV TWINT"},
	{"scheme": "twint-issuer7", "display_name": "Other TWINT"},
]


@frappe.whitelist(allow_guest=True)
def get_ios_schemes() -> dict[str, Any]:
	"""Return TWINT iOS deep-link schemes for the in-app launcher buttons.

	Mirrors the legacy ``twint_integration`` API shape so the dialog JS can
	keep its existing iOS / bank-icon selector unchanged.
	"""
	return {"success": True, "schemes": list(_IOS_SCHEMES)}


@frappe.whitelist(allow_guest=True)
def cancel_web_transaction(intent_name: str) -> dict[str, Any]:
	"""Compat alias for the legacy ``cancel_payment(order_uuid)`` JS call.

	Delegates to :func:`payments.api.intent.cancel_intent` which calls the
	driver's ``cancel_intent`` (which routes through the bridge).
	"""
	from payments.api.intent import cancel_intent

	res = cancel_intent(intent_name)
	# Adapt to the {success, message} shape the legacy JS expects.
	if res.get("status") == "canceled":
		return {"success": True, "message": "Payment cancelled"}
	return {"success": False, "message": res.get("error_message") or "cancel failed"}
