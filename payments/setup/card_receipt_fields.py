# //// Neoffice — added file (no upstream equivalent). The EMV block a card receipt is
# //// printed from, on `Sales Invoice Payment`. Until now these thirteen fields were
# //// Custom Fields posted BY HAND on the instances — `module = null`, no app created
# //// them — so they exist where someone once typed them and nowhere else. On a fresh
# //// site (the CI, and every new instance of the fleet) the row had none of them:
# //// `payments.api.card_receipt` wrote its mentions onto attributes that are not
# //// columns, nothing was persisted, the till printed a sales receipt where a card
# //// receipt is required, and two tests died on `AttributeError` — issue #192.
# //// Upstream has neither a card terminal nor a card receipt, so nothing to provision.
# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""The EMV fields a card receipt is printed from, provisioned once per site.

A card receipt is not decoration. In Switzerland the customer's copy is expected to
carry the masked PAN, the scheme, the AID and the authorisation — the mentions that
let an acquirer trace a transaction and a customer dispute it. They are written by
:mod:`payments.api.card_receipt` onto the ``Sales Invoice Payment`` row from the
Payment Intent that took the payment, and printed by the POS receipt of
``neoffice_theme`` (``templates/print/oslo/documents/pos_receipt.html``).

They had no owner. Somebody created them by hand on the instances that needed them,
which is invisible until a site is built from nothing: ``bench install-app payments``
created no such field, ``frappe.new_doc("Sales Invoice")`` then produced a payment row
without them, and the two tests that assert a mention is *absent* raised
``AttributeError`` instead. The rest passed by accident — ``doc.set()`` happily writes
an undeclared name onto the in-memory document, so the fields the receipt fills looked
filled and would never have reached the database.

**Why all thirteen and not only the six the receipt writes.** They are one block and
one chain: each field anchors on the one above it, down to ``card_authorization_code``
which anchors on ``card_transaction_status_information``. Shipping only the six used
today would either break the chain (the rest lands at the end of the row) or force a
re-anchoring, and re-anchoring rewrites ``insert_after`` on every instance that already
carries the block — reordering, on live sites, a receipt that is correct today. The six
we do not fill yet — account type, application name, ARC, cryptogram, TVR, TSI — are
mentions a terminal can return and Payrexx does not; they belong to the receipt, and a
driver that starts returning one must find a column to put it in.

**Labels stay in English here on purpose.** A Custom Field label is stored once and
translated at render time; wrapping it in ``_()`` would freeze the language of whoever
ran the install into the database and give every other user the wrong one.

``provision_card_receipt_fields`` is idempotent. It runs from ``after_install``
(hooks.py) for a fresh site, where a patch never runs — ``bench install-app`` marks
every patch as completed without executing it — and from
``payments.patches.v15_09.provision_card_receipt_fields`` for the sites that migrate.
"""

from __future__ import annotations

import click
import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

DOCTYPE = "Sales Invoice Payment"

#: This app's module, so the fields have an owner. A Custom Field with no module is a
#: field no app delivers, which is exactly how the block came to be missing on a fresh
#: site: nothing named it, so nothing created it.
MODULE = "Payments"

#: The block, in the order it is laid out on the row: each entry anchors on the one
#: above it, starting at ``clearance_date``, the last field ERPNext ships. The list is
#: the chain — keep it ordered, and add a new field at the end of it.
CARD_RECEIPT_FIELDS: dict[str, list[dict]] = {
	DOCTYPE: [
		{
			"fieldname": "card_brand",
			"label": "Card Brand",
			"fieldtype": "Data",
			"insert_after": "clearance_date",
			"read_only": 1,
		},
		{
			"fieldname": "card_last4",
			"label": "Card Last4",
			"fieldtype": "Data",
			"insert_after": "card_brand",
			"read_only": 1,
		},
		{
			"fieldname": "card_account_type",
			"label": "Card Account Type",
			"fieldtype": "Data",
			"insert_after": "card_last4",
			"read_only": 1,
		},
		{
			"fieldname": "card_application_preferred_name",
			"label": "Card Application Name",
			"fieldtype": "Data",
			"insert_after": "card_account_type",
			"read_only": 1,
		},
		# Dedicated File Name — the EMV application identifier, e.g. A0000000041010.
		{
			"fieldname": "card_dedicated_file_name",
			"label": "AID",
			"fieldtype": "Data",
			"insert_after": "card_application_preferred_name",
			"read_only": 1,
		},
		{
			"fieldname": "card_authorization_response_code",
			"label": "ARC",
			"fieldtype": "Data",
			"insert_after": "card_dedicated_file_name",
			"read_only": 1,
		},
		{
			"fieldname": "card_application_cryptogram",
			"label": "Application Cryptogram",
			"fieldtype": "Data",
			"insert_after": "card_authorization_response_code",
			"read_only": 1,
		},
		{
			"fieldname": "card_terminal_verification_results",
			"label": "TVR",
			"fieldtype": "Data",
			"insert_after": "card_application_cryptogram",
			"read_only": 1,
		},
		{
			"fieldname": "card_transaction_status_information",
			"label": "TSI",
			"fieldtype": "Data",
			"insert_after": "card_terminal_verification_results",
			"read_only": 1,
		},
		{
			"fieldname": "card_authorization_code",
			"label": "Authorization Code",
			"fieldtype": "Data",
			"insert_after": "card_transaction_status_information",
			"read_only": 1,
		},
		{
			"fieldname": "card_charge_id",
			"label": "Charge ID",
			"fieldtype": "Data",
			"insert_after": "card_authorization_code",
			"read_only": 1,
		},
		# The intent that took the payment; the till writes it, `card_receipt` reads it
		# to find the mentions. Data rather than Link: the row must survive an intent
		# that was purged, and a receipt printed once is a record, not a live join.
		{
			"fieldname": "card_payment_intent",
			"label": "Payment Intent",
			"fieldtype": "Data",
			"insert_after": "card_charge_id",
			"read_only": 1,
		},
		# Minor units, like every amount this app exchanges with a PSP.
		{
			"fieldname": "amount_authorized",
			"label": "Amount Authorized",
			"fieldtype": "Int",
			"insert_after": "card_payment_intent",
			"read_only": 1,
		},
	]
}


def provision_card_receipt_fields() -> None:
	"""Create the card receipt block on ``Sales Invoice Payment``. Idempotent.

	Runs with Custom Field validation ON: should ERPNext one day ship a field of one
	of these names itself, the install must say so rather than quietly stack a Custom
	Field on top of a DocField and leave a site that migrates fine and reads wrong.
	"""
	if "erpnext" not in frappe.get_installed_apps():
		# `Sales Invoice Payment` is an ERPNext doctype and payments installs without
		# it — a gateway-only site has no invoice to print a card receipt from.
		return

	missing = [
		df["fieldname"]
		for df in CARD_RECEIPT_FIELDS[DOCTYPE]
		if not frappe.db.exists("Custom Field", {"dt": DOCTYPE, "fieldname": df["fieldname"]})
	]
	if missing:
		click.secho(f"* Installing {len(missing)} card receipt Custom Fields in {DOCTYPE}")

	create_custom_fields(
		{dt: [{**df, "module": MODULE} for df in fields] for dt, fields in CARD_RECEIPT_FIELDS.items()}
	)

	frappe.clear_cache(doctype=DOCTYPE)
