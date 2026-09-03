# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""Collecting on site from the mobile app — what the phone asks the server.

Two ways to be paid at the customer's door, chosen once in Mobile Payment Settings:

- **card** — Stripe Tap to Pay. The server creates the PaymentIntent and hands its
  ``client_secret`` to the phone; the Stripe Terminal SDK in our app collects the card
  and confirms; the ``payment_intent.succeeded`` webhook settles the Payment Intent.
- **twint** — a merchant-presented QR for the customer's TWINT app, the same flow as
  the shop and the till, drawn by the phone. The TWINT pollers settle it.

Whatever the method, the phone initiates and the **server is the only authority** on
whether the money arrived: the app follows the Payment Intent (realtime event, with a
poll underneath) and never trusts an SDK result on its own.

These endpoints exist so the app does not have to know provider record names, the
site's currency, or :func:`payments.api.intent.create_intent`'s generic signature —
and so starting a payment against a document checks the caller can read it.
"""

from __future__ import annotations

import time
from typing import Any

import frappe
from frappe import _

from payments.api.intent import cancel_intent, create_intent, get_intent_status

CARD_CHANNEL = "stripe_tap_to_pay"
TWINT_CHANNEL = "twint_mobile"
MOBILE_CHANNELS = (CARD_CHANNEL, TWINT_CHANNEL)

METHODS = {"card": CARD_CHANNEL, "twint": TWINT_CHANNEL}

# States in which a payment is still open on the phone's side — the ones a
# relaunched app has to resume watching rather than start over.
OPEN_STATES = ("requires_action", "processing")


def _settings():  # noqa: ANN202
	return frappe.get_cached_doc("Mobile Payment Settings")


def _binding(channel: str, provider: str | None) -> dict[str, Any] | None:
	"""The enabled binding for ``channel`` on ``provider``, or ``None``.

	Both toggles are checked: the merchant's choice in the settings, and the
	technical binding that resolves the driver. One without the other is a
	half-configured site, and the app must not offer the method.
	"""
	if not provider:
		return None
	row = frappe.db.get_value(
		"Provider Channel Settings",
		{"channel": channel, "provider": provider, "enabled": 1},
		["name", "provider", "config_json"],
		as_dict=True,
	)
	if not row:
		return None
	if not frappe.db.get_value("Payment Provider", provider, "enabled"):
		return None
	return row


def _currency() -> str:
	return (frappe.db.get_single_value("Global Defaults", "default_currency") or "CHF").upper()


def _card_context(settings) -> dict[str, Any]:  # noqa: ANN001
	binding = _binding(CARD_CHANNEL, settings.tap_to_pay_provider) if settings.enable_tap_to_pay else None
	location = (settings.stripe_location or "").strip() or None
	return {
		"enabled": bool(binding and location),
		"provider": binding.provider if binding else None,
		"location_id": location if binding else None,
	}


def _twint_context(settings) -> dict[str, Any]:  # noqa: ANN001
	binding = _binding(TWINT_CHANNEL, settings.twint_provider) if settings.enable_twint else None
	return {"enabled": bool(binding), "provider": binding.provider if binding else None}


@frappe.whitelist()
def mobile_context() -> dict[str, Any]:
	"""What the phone may offer here, and in which currency.

	The device's own side of the answer (NFC, OS version, not rooted) belongs to
	the Stripe SDK on the phone; the app combines both before drawing a button.
	"""
	settings = _settings()
	card = _card_context(settings)
	twint = _twint_context(settings)
	return {
		"currency": _currency(),
		"card": card,
		"twint": twint,
		"methods": [m for m, on in (("card", card["enabled"]), ("twint", twint["enabled"])) if on],
		# So the app shows its simulate affordance only where simulate_success
		# would accept the call — never on a customer site.
		"simulators_enabled": bool(frappe.conf.get("enable_e2e_simulators")),
	}


@frappe.whitelist()
def connection_token() -> dict[str, Any]:
	"""A Stripe Terminal connection token for the phone's SDK.

	Short-lived, scoped to the merchant's Location, minted with the server-side
	secret key so the app never holds it. The SDK asks for a fresh one whenever it
	needs to; this is the ``fetchConnectionToken`` the provider is initialised with.
	"""
	settings = _settings()
	card = _card_context(settings)
	if not card["enabled"]:
		frappe.throw(_("Tap to Pay is not set up on this site"))

	from payments.drivers.stripe.provider import StripeProvider

	provider = StripeProvider(frappe.get_doc("Payment Provider", card["provider"]))
	import stripe

	token = stripe.terminal.ConnectionToken.create(api_key=provider.secret_key, location=card["location_id"])
	return {"secret": token.secret, "location_id": card["location_id"]}


@frappe.whitelist()
def mobile_start_payment(
	amount: int,
	reference_doctype: str,
	reference_name: str,
	method: str = "card",
) -> dict[str, Any]:
	"""Record a payment the phone is about to take, against one document.

	Returns the serialized intent. For a card, ``client_secret`` is what the
	Terminal SDK confirms; for TWINT, ``next_action_payload`` carries the QR and
	the pairing token.
	"""
	channel = METHODS.get(str(method or "").lower())
	if not channel:
		frappe.throw(_("Unknown payment method {0}").format(method))

	settings = _settings()
	ctx = _card_context(settings) if channel == CARD_CHANNEL else _twint_context(settings)
	if not ctx["enabled"]:
		frappe.throw(_("This payment method is not set up on this site"))

	amount = int(amount or 0)
	if amount <= 0:
		frappe.throw(_("amount must be > 0"))

	if not reference_doctype or not reference_name:
		frappe.throw(_("A document to pay for is required"))
	if not frappe.db.exists(reference_doctype, reference_name):
		frappe.throw(_("{0} {1} does not exist").format(reference_doctype, reference_name))
	# The generic create_intent inserts with ignore_permissions. Here the caller
	# is a person on site, and an intent number is enough to watch a payment, so
	# they must at least be allowed to read the document they claim to collect for.
	if not frappe.has_permission(reference_doctype, "read", doc=reference_name):
		frappe.throw(
			_("Not permitted to collect a payment for {0} {1}").format(reference_doctype, reference_name),
			frappe.PermissionError,
		)

	return create_intent(
		provider=ctx["provider"],
		channel=channel,
		amount=amount,
		currency=_currency(),
		reference_doctype=reference_doctype,
		reference_name=reference_name,
		metadata={"origin": "mobile"},
	)


@frappe.whitelist()
def mobile_payments_for(reference_doctype: str, reference_name: str) -> list[dict[str, Any]]:
	"""The on-site payments recorded against one document, newest first.

	Two readers: the screen, to show what was already collected; and the app on
	relaunch, to find an intent still open and resume watching it — the payment
	may have completed while the process was dead.
	"""
	if not reference_doctype or not reference_name:
		return []
	if not frappe.has_permission(reference_doctype, "read", doc=reference_name):
		frappe.throw(_("Not permitted"), frappe.PermissionError)

	rows = frappe.get_all(
		"Payment Intent",
		filters={
			"channel": ["in", list(MOBILE_CHANNELS)],
			"reference_doctype": reference_doctype,
			"reference_name": reference_name,
		},
		fields=[
			"name",
			"status",
			"channel",
			"amount",
			"currency",
			"creation",
			"modified",
			"provider_intent_id",
			"error_code",
			"error_message",
		],
		order_by="creation desc",
		limit_page_length=20,
	)
	return [
		{
			"intent_name": row.name,
			"status": row.status,
			"method": "card" if row.channel == CARD_CHANNEL else "twint",
			"amount": row.amount,
			"currency": row.currency,
			"created_at": str(row.creation),
			"updated_at": str(row.modified),
			"provider_intent_id": row.provider_intent_id,
			"error_code": row.error_code,
			"error_message": row.error_message,
			"open": row.status in OPEN_STATES,
		}
		for row in rows
	]


@frappe.whitelist()
def mobile_abandon_payment(intent_name: str) -> dict[str, Any]:
	"""The operator gave up before the customer paid.

	Thin wrapper over :func:`payments.api.intent.cancel_intent` with the reason
	filled in, so the record says it was a choice and not a timeout. The generic
	endpoint leaves a paid intent alone even when asked to cancel it — see its
	docstring — so this is safe to call on a race with the webhook.
	"""
	return cancel_intent(intent_name, reason="mobile_abandoned")


@frappe.whitelist()
def simulate_success(intent_name: str) -> dict[str, Any]:
	"""DEV-ONLY — finish an on-site intent as the webhook or the poller would.

	Raises ``frappe.PermissionError`` (HTTP 403) unless
	``frappe.conf.enable_e2e_simulators`` is truthy. Idempotent: re-running
	re-publishes the realtime event, which is what a debugging session wants.
	"""
	if not frappe.conf.get("enable_e2e_simulators"):
		frappe.throw(_("E2E simulators not enabled on this site"), frappe.PermissionError)

	doc = frappe.get_doc("Payment Intent", intent_name)
	if doc.channel not in MOBILE_CHANNELS:
		frappe.throw(_("{0} is not an on-site payment").format(intent_name))

	if doc.status != "succeeded":
		moved = doc.transition_to(
			"succeeded",
			event_source="manual",
			payload_excerpt="E2E simulate_success (dev-only)",
			ignore_invalid=True,
		)
		if not moved:
			return {"ok": False, "error": f"could not transition to succeeded from {doc.status}"}
		doc.reload()

	# The webhook fills this in for a real payment. A simulated one gets a value
	# that says so, rather than leaving the card with nothing to name.
	if not doc.provider_intent_id:
		doc.db_set("provider_intent_id", f"sim-{int(time.time())}", update_modified=False)

	frappe.publish_realtime(
		event=f"payment.intent.{doc.name}.updated",
		message={"intent_name": doc.name, "status": "succeeded", "channel": doc.channel},
		after_commit=True,
	)
	frappe.db.commit()
	return get_intent_status(doc.name)

