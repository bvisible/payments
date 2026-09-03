# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""Payrexx Tap to Pay — what the phone needs from the server, and nothing more.

The phone initiates the payment (see :mod:`payments.drivers.payrexx.tap_to_pay_driver`);
the server records it and is the only authority on whether the money arrived. These
endpoints exist so the mobile app does not have to know the provider record's name,
the site's currency, or the generic :func:`payments.api.intent.create_intent`
signature — and so that starting a payment against a document checks the caller can
actually read that document, which the generic endpoint does not.

Endpoints:

- ``mobile_context`` — is Tap to Pay set up on this site, and in which currency.
  The app hides the feature entirely when the answer is no.
- ``mobile_start_payment`` — create the Payment Intent for one document and return
  the handoff payload the phone hands to the Payrexx app.
- ``mobile_payments_for`` — the payments already taken against one document, so the
  screen can show them, and so a payment interrupted by Android killing the process
  can be picked up again on the next launch.
- ``simulate_success`` — DEV-ONLY: finish an intent as if the webhook had. Mirrors
  :func:`payments.api.twint.simulate_consumer_success`; an emulator has no NFC, and
  this is what lets the whole follow-up path be tested without a card.
"""

from __future__ import annotations

import json
import time
from typing import Any

import frappe
from frappe import _

from payments.api.intent import cancel_intent, create_intent, get_intent_status

CHANNEL = "payrexx_tap_to_pay"

# States in which a payment is still open on the phone's side — the ones a
# relaunched app has to resume watching rather than start over.
OPEN_STATES = ("requires_action", "processing")


def _binding() -> dict[str, Any] | None:
	"""The enabled provider ↔ channel binding for Tap to Pay, or ``None``.

	One site, one Payrexx account, one binding: a second enabled binding on this
	channel would be a configuration mistake, and the newest one is taken so a
	re-run of the setup wizard wins over a stale row.
	"""
	rows = frappe.get_all(
		"Provider Channel Settings",
		filters={"channel": CHANNEL, "enabled": 1},
		fields=["name", "provider"],
		order_by="modified desc",
		limit_page_length=1,
	)
	if not rows:
		return None
	row = rows[0]
	if not frappe.db.get_value("Payment Provider", row.provider, "enabled"):
		return None
	return row


def _currency() -> str:
	return (frappe.db.get_single_value("Global Defaults", "default_currency") or "CHF").upper()


@frappe.whitelist()
def mobile_context() -> dict[str, Any]:
	"""Whether the phone should offer Tap to Pay here, and in which currency.

	``enabled`` is the merchant's side of the answer (an account is set up); the
	device's side (NFC, Payrexx app installed) is the native module's, and the app
	combines both before drawing a button.
	"""
	binding = _binding()
	return {
		"enabled": bool(binding),
		"provider": binding.provider if binding else None,
		"channel": CHANNEL,
		"currency": _currency(),
		# So the app shows its simulate affordance only where the endpoint below
		# would accept the call — never on a customer site.
		"simulators_enabled": bool(frappe.conf.get("enable_e2e_simulators")),
	}


@frappe.whitelist()
def mobile_start_payment(
	amount: int,
	reference_doctype: str,
	reference_name: str,
	payment_method: str | None = None,
	tip_amount: int | None = None,
) -> dict[str, Any]:
	"""Record a payment the phone is about to take, against one document.

	Returns the serialized intent. Its ``next_action_payload`` carries what the
	native module's ``sale`` needs — above all ``order_reference``, which is the
	intent name and the only thing tying the Payrexx transaction back to this
	record once the webhook lands.
	"""
	binding = _binding()
	if not binding:
		frappe.throw(_("Payrexx Tap to Pay is not set up on this site"))

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
			_("Not permitted to collect a payment for {0} {1}").format(
				reference_doctype, reference_name
			),
			frappe.PermissionError,
		)

	metadata: dict[str, Any] = {"origin": "mobile"}
	if payment_method:
		metadata["payment_method"] = str(payment_method).upper()
	if tip_amount:
		metadata["tip_amount"] = int(tip_amount)

	return create_intent(
		provider=binding.provider,
		channel=CHANNEL,
		amount=amount,
		currency=_currency(),
		reference_doctype=reference_doctype,
		reference_name=reference_name,
		metadata=metadata,
	)


@frappe.whitelist()
def mobile_payments_for(reference_doctype: str, reference_name: str) -> list[dict[str, Any]]:
	"""The Tap to Pay intents recorded against one document, newest first.

	Two readers: the screen, to show what was already collected; and the app on
	relaunch, to find an intent still in an open state and resume watching it —
	the payment may have completed while the process was dead.
	"""
	if not reference_doctype or not reference_name:
		return []
	if not frappe.has_permission(reference_doctype, "read", doc=reference_name):
		frappe.throw(_("Not permitted"), frappe.PermissionError)

	rows = frappe.get_all(
		"Payment Intent",
		filters={
			"channel": CHANNEL,
			"reference_doctype": reference_doctype,
			"reference_name": reference_name,
		},
		fields=[
			"name",
			"status",
			"amount",
			"currency",
			"creation",
			"modified",
			"provider_intent_id",
			"error_code",
			"error_message",
			"metadata_json",
		],
		order_by="creation desc",
		limit_page_length=20,
	)
	out = []
	for row in rows:
		meta = {}
		if row.metadata_json:
			try:
				meta = json.loads(row.metadata_json)
			except (ValueError, TypeError):
				meta = {}
		out.append(
			{
				"intent_name": row.name,
				"status": row.status,
				"amount": row.amount,
				"currency": row.currency,
				"created_at": str(row.creation),
				"updated_at": str(row.modified),
				"provider_intent_id": row.provider_intent_id,
				"payment_method": meta.get("payment_method"),
				"error_code": row.error_code,
				"error_message": row.error_message,
				"open": row.status in OPEN_STATES,
			}
		)
	return out


@frappe.whitelist()
def mobile_abandon_payment(intent_name: str) -> dict[str, Any]:
	"""The operator gave up before a card was tapped.

	Thin wrapper over :func:`payments.api.intent.cancel_intent` with the reason
	filled in, so the record says it was a choice and not a timeout. The generic
	endpoint leaves a paid intent alone even when asked to cancel it — see its
	docstring — so this is safe to call on a race with the webhook.
	"""
	return cancel_intent(intent_name, reason="mobile_abandoned")


@frappe.whitelist()
def simulate_success(intent_name: str) -> dict[str, Any]:
	"""DEV-ONLY — finish a Tap to Pay intent as the webhook would.

	Raises ``frappe.PermissionError`` (HTTP 403) unless
	``frappe.conf.enable_e2e_simulators`` is truthy. Idempotent: re-running
	re-publishes the realtime event, which is what a debugging session wants.
	"""
	if not frappe.conf.get("enable_e2e_simulators"):
		frappe.throw(_("E2E simulators not enabled on this site"), frappe.PermissionError)

	doc = frappe.get_doc("Payment Intent", intent_name)
	if doc.channel != CHANNEL:
		frappe.throw(_("{0} is not a Tap to Pay intent").format(intent_name))

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
		message={"intent_name": doc.name, "status": "succeeded", "channel": CHANNEL},
		after_commit=True,
	)
	frappe.db.commit()
	return get_intent_status(doc.name)
