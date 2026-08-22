# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""Fill the card mentions on a POS invoice from the Payment Intent that took them.

A card receipt is not decoration. In Switzerland the customer's copy is expected to
carry the masked PAN, the scheme, the AID and the authorisation — the EMV fields
that let an acquirer trace the transaction and a customer dispute it. ``Sales
Invoice Payment`` already has custom fields for all of them (``card_brand``,
``card_last4``, ``card_dedicated_file_name`` = AID, ``card_authorization_code``…),
added for an earlier terminal integration and never populated by anything.

They matter now because the terminal no longer prints anything of its own: its
paper carries the acceptance platform's branding rather than the merchant's, so
the document the customer takes away is the one the till prints. If these fields
stay empty, that document is not a card receipt.

The data lives on the Payment Intent, written by
:func:`payments.api.webhook_payrexx._persist_terminal_receipt` at the one moment
it exists — when the payment settles. This copies it onto the invoice row, keyed
by ``card_payment_intent``, which the till sets when a terminal payment succeeds.
"""

from __future__ import annotations

import json
from typing import Any

import frappe

#: Invoice row field ← key in the intent's persisted ``receipt``.
#:
#: Only the fields the slip actually carries. The other EMV ones are left empty on
#: purpose: ``card_authorization_code`` is the acquirer's approval code, which
#: Payrexx does not return, and ARC, TVR, TSI and the cryptogram are absent too.
#: Filling ``card_authorization_code`` with the AID — the one value we do have —
#: would put a wrong number on a document a customer may use to dispute a charge.
#: An empty field is a missing mention; a wrong one is a false statement.
_FIELD_MAP = {
	"card_brand": "card_scheme",
	# Dedicated File Name — the EMV application identifier, e.g. A0000000041010.
	"card_dedicated_file_name": "authorisation",
	"card_charge_id": "payment_id",
}


def _receipt_for(intent_name: str) -> dict[str, Any]:
	payload = frappe.db.get_value("Payment Intent", intent_name, "next_action_payload")
	if not payload:
		return {}
	try:
		return (json.loads(payload) or {}).get("receipt") or {}
	except (ValueError, TypeError):
		return {}


def fill_card_details(doc, method=None) -> None:  # noqa: ANN001, ARG001
	"""Copy the card mentions onto every payment row that names an intent.

	Never overwrites a value already present: another provider's driver may fill
	these itself, and a row edited by hand should stay edited. Silent when the
	intent has no receipt — a cash row, or a terminal payment whose intent settled
	before the details were being kept.
	"""
	for row in doc.get("payments") or []:
		intent_name = row.get("card_payment_intent")
		if not intent_name:
			continue

		receipt = _receipt_for(intent_name)
		if not receipt:
			continue

		for field, source in _FIELD_MAP.items():
			if not row.get(field) and receipt.get(source):
				row.set(field, str(receipt[source]))

		# The masked PAN arrives as a whole (`535388******4256`); the field wants the
		# last four, which is what a receipt prints and what a customer recognises.
		if not row.get("card_last4"):
			pan = str(receipt.get("masked_pan") or "").strip()
			if len(pan) >= 4 and pan[-4:].isdigit():
				row.set("card_last4", pan[-4:])

		# Minor units, like everywhere else in this app.
		if not row.get("amount_authorized") and receipt.get("amount"):
			row.set("amount_authorized", int(receipt["amount"]))
