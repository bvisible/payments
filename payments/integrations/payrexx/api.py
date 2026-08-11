# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""Webshop pivot for Payrexx hosted checkout.

Mirrors :mod:`payments.integrations.twint.api`, but the flow is simpler: Payrexx
returns a hosted page URL and the buyer is redirected there. No QR, no overlay, no
in-page SDK.

Two-step contract with the webshop, same as Stripe/Wallee/TWINT:

1. ``webshop.controllers.payment_handler.create_payment_request`` creates the
   Frappe Payment Request.
2. :func:`create_web_transaction` creates the Payment Intent through
   :class:`~payments.drivers.payrexx.web_driver.PayrexxWebDriver` and returns the
   URL to send the buyer to.

The buyer comes back on ``/payrexx/success?payment_intent=PI-…``, which finalises
the Sales Order. The transaction webhook drives the same FSM and covers the case
where the buyer closes the tab before returning.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _

CHANNEL = "payrexx_web"


@frappe.whitelist(allow_guest=True)
def create_web_transaction(
	payment_request_id: str,
	provider: str | None = None,
) -> dict[str, Any]:
	"""Create the Payrexx gateway for a Payment Request and return its URL.

	Args:
	    payment_request_id: name of the Frappe Payment Request.
	    provider: optional Payrexx Payment Provider name; the first enabled one
	        otherwise, so a site with a single provider needs no argument.

	Returns:
	    ``{status, payment_request_id, payment_intent, transaction_id,
	    redirect_url, app_link, failed_url}``. On failure, ``status="error"`` with a
	    ``message`` — never an exception, because the caller is a checkout page and
	    a traceback there tells the buyer nothing.
	"""
	from payments.api.intent import create_intent

	pr = frappe.get_doc("Payment Request", payment_request_id)
	if pr.payment_request_type != "Inward":
		frappe.throw(_("Payment Request {0} is not Inward").format(payment_request_id))

	provider_name = _resolve_provider_name(provider)
	if not provider_name:
		return {
			"status": "error",
			"message": _("No enabled Payrexx Payment Provider is configured"),
			"payment_request_id": payment_request_id,
		}

	# Minor units (rappen for CHF) — same convention as every other provider here.
	amount_minor = int(round((pr.grand_total or 0) * 100))

	try:
		intent = create_intent(
			provider=provider_name,
			channel=CHANNEL,
			amount=amount_minor,
			currency=pr.currency,
			reference_doctype="Payment Request",
			reference_name=payment_request_id,
			metadata={
				"purpose": pr.subject or f"Webshop {payment_request_id}",
				"language": frappe.local.lang or None,
			},
		)
	except Exception as exc:  # noqa: BLE001 - a checkout page must not show a traceback
		frappe.log_error("Payrexx web transaction failed", f"{payment_request_id}: {exc!r}")
		return {
			"status": "error",
			"message": _("Could not start the Payrexx payment. Please try again."),
			"payment_request_id": payment_request_id,
		}

	if intent.get("status") in ("failed", "canceled"):
		return {
			"status": "error",
			"message": intent.get("error_message") or _("Payrexx transaction creation failed"),
			"payment_request_id": payment_request_id,
		}

	payload = intent.get("next_action_payload") or {}
	redirect_url = payload.get("url")
	if not redirect_url:
		return {
			"status": "error",
			"message": _("Payrexx did not return a payment page URL"),
			"payment_request_id": payment_request_id,
		}

	return {
		"status": "success",
		"payment_request_id": payment_request_id,
		"payment_intent": intent.get("intent_name"),
		"transaction_id": intent.get("provider_intent_id"),
		"redirect_url": redirect_url,
		# Payrexx also returns a mobile deep link; a native caller may prefer it.
		"app_link": payload.get("app_link"),
		"failed_url": frappe.utils.get_url() + "/cart",
	}


@frappe.whitelist(allow_guest=True)
def get_intent_state(intent_name: str) -> dict[str, Any]:
	"""Lightweight status read, for a checkout page polling after the redirect."""
	pi = frappe.get_doc("Payment Intent", intent_name)
	return {
		"intent_name": pi.name,
		"status": pi.status,
		"error_code": pi.error_code,
		"error_message": pi.error_message,
	}


@frappe.whitelist(allow_guest=True)
def is_payrexx_enabled() -> bool:
	"""Whether the checkout can offer Payrexx.

	True only when an enabled Payrexx provider exists **and** it has a binding for
	the web channel — a provider configured for the terminal alone cannot serve a
	webshop payment, and offering it would fail at the click.
	"""
	provider_name = _resolve_provider_name(None)
	if not provider_name:
		return False
	return bool(
		frappe.db.get_value(
			"Provider Channel Settings",
			{"provider": provider_name, "channel": CHANNEL, "enabled": 1},
			"name",
		)
	)


def _resolve_provider_name(provider: str | None) -> str | None:
	"""Resolve a Payrexx provider by name, or the first enabled one.

	Detected by driver class rather than record name so ``payrexx_test`` and
	``payrexx_live`` can cohabit on one site.
	"""
	if provider:
		return provider
	return frappe.db.get_value(
		"Payment Provider",
		{"driver_class": ["like", "payments.drivers.payrexx.%"], "enabled": 1},
		"name",
		order_by="modified desc",
	)
