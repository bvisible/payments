#//// Neoffice — added file (no upstream equivalent). Lays the card receipt of a terminal
#//// payment out as Star Document Markup and hands the job to CloudPRNT. The Payrexx
#//// NexGo N86 prints a slip branded by the acceptance platform and exposes no API to
#//// drive its printer, so the device's printing is switched off (`print_slip=False`)
#//// and the receipt is printed on the merchant's own paper from the `slip` the payment
#//// left on the intent. Upstream knows no terminal and no receipt.
#//// Commits: 23b7f0e 2026-08-21 "feat(payrexx): print the card receipt ourselves, on our own paper"
#////          40cf6d0 2026-08-21 "feat(payrexx): render the receipt as it comes off the roll"
# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""Print a card receipt for a terminal payment, on our own paper.

A payment terminal prints its own slip, and that slip carries the acceptance
platform's branding — on the Payrexx NexGo N86 it is a third party's, which a
merchant has no reason to hand their customer. There is no API to drive the device's
printer, so the answer is not to fight it: switch the device's printing off
(``print_slip=False``) and print the receipt here instead.

Everything a card receipt legally needs comes back from the terminal in the payment's
``slip`` — amount, date, masked PAN, authorisation code, terminal and merchant. The
Payrexx client turns that positional list into named fields
(:attr:`payrexx.models.EcrPayment.receipt`), and this module lays them out as Star
Document Markup and hands the job to CloudPRNT.

Usage::

    bench --site <site> execute payments.api.terminal_receipt.print_receipt \\
        --kwargs "{'intent_name': 'PI-2026-00000081'}"

or from the till, over the whitelisted endpoint of the same name.
"""

from __future__ import annotations

import uuid
from typing import Any

import frappe
from frappe import _

# 42 characters is the printable width of a 80 mm Star roll in font A.
_WIDTH = 42


def _line(left: str, right: str = "", width: int = _WIDTH) -> str:
	"""One row with the label left and the value right-aligned."""
	left, right = str(left), str(right)
	pad = max(1, width - len(left) - len(right))
	return f"{left}{' ' * pad}{right}"


def _money(minor: Any, currency: str | None) -> str:
	try:
		return f"{int(minor) / 100:.2f} {currency or ''}".strip()
	except (TypeError, ValueError):
		return f"{minor} {currency or ''}".strip()


def build_markup(receipt: dict[str, Any], *, copy_for: str = "client") -> str:
	"""Lay the receipt out as Star Document Markup.

	``copy_for`` follows the card-scheme convention of two slips: the customer keeps
	one, the merchant files the other. Only the footer differs.
	"""
	currency = receipt.get("currency")
	rows: list[str] = ["[align: centre][font: a]"]

	name = receipt.get("merchant_name")
	if name:
		rows.append(f"[magnify: width 2; height 2]{name}[magnify: width 1; height 1]")
	for part in (
		receipt.get("merchant_address"),
		" ".join(x for x in (receipt.get("merchant_zip"), receipt.get("merchant_city")) if x),
	):
		if part:
			rows.append(str(part))

	rows += ["", "[align: left]", "-" * _WIDTH]
	rows.append(_line(_("Card payment")))
	rows.append("-" * _WIDTH)

	if receipt.get("datetime"):
		rows.append(_line(_("Date"), receipt["datetime"]))
	if receipt.get("payment_method"):
		rows.append(_line(_("Method"), receipt["payment_method"]))
	if receipt.get("masked_pan"):
		rows.append(_line(_("Card"), receipt["masked_pan"]))
	if receipt.get("authorisation"):
		rows.append(_line(_("Authorisation"), receipt["authorisation"]))
	if receipt.get("terminal_label"):
		rows.append(_line(_("Terminal"), receipt["terminal_label"]))
	if receipt.get("transaction_uuid"):
		rows.append(_line(_("Transaction"), receipt["transaction_uuid"]))

	rows.append("-" * _WIDTH)
	if receipt.get("tip_amount"):
		rows.append(_line(_("Tip"), _money(receipt["tip_amount"], currency)))
	rows.append(
		"[magnify: width 2; height 2]"
		+ _line(_("TOTAL"), _money(receipt.get("amount"), currency), width=_WIDTH // 2)
		+ "[magnify: width 1; height 1]"
	)
	rows.append("-" * _WIDTH)

	rows += [
		"",
		"[align: centre]",
		_("Customer copy") if copy_for == "client" else _("Merchant copy"),
		_("Thank you for your visit"),
		"",
		"",
		"[cut: feed; partial]",
	]
	return "\n".join(rows)


def _receipt_from_intent(intent_name: str) -> dict[str, Any]:
	"""The receipt data for an intent, from the payload or read back from the device.

	The payload is preferred because it costs nothing; a live read is the fallback for
	an intent recorded before this existed, and for anything the till may have dropped.
	"""
	intent = frappe.get_doc("Payment Intent", intent_name)
	payload = frappe.parse_json(intent.next_action_payload or "{}") or {}
	receipt = payload.get("receipt")
	if receipt:
		return receipt

	if not intent.provider_intent_id:
		frappe.throw(_("Payment Intent {0} has no provider payment to read").format(intent_name))

	from payments.drivers.registry import resolve_driver

	driver = resolve_driver(intent.provider, intent.channel)
	response = driver.get_status(intent.provider_intent_id, device_id=intent.device)
	receipt = (response.next_action_payload or {}).get("receipt")
	if not receipt:
		frappe.throw(_("The terminal returned no receipt data for {0}").format(intent_name))
	return receipt


@frappe.whitelist()
def print_receipt(
	intent_name: str, printer: str | None = None, copies: int = 1
) -> dict[str, Any]:
	"""Queue a card receipt for a terminal payment.

	Args:
		intent_name: the Payment Intent behind the terminal payment.
		printer: CloudPRNT printer label or MAC; the configured default otherwise.
		copies: 1 for the customer slip, 2 to add the merchant's.
	"""
	receipt = _receipt_from_intent(intent_name)

	if not printer:
		printer = frappe.db.get_value(
			"Singles", {"doctype": "CloudPRNT Settings", "field": "default_printer"}, "value"
		)
	if not printer:
		frappe.throw(_("No CloudPRNT printer given and no default configured"))

	mac = printer
	if ":" not in printer:
		mac = frappe.db.get_value(
			"CloudPRNT Printers", {"label": printer}, "mac_address"
		) or frappe.db.get_value("CloudPRNT Printers", printer, "mac_address")
	if not mac:
		frappe.throw(_("Printer {0} not found").format(printer))

	from cloudprnt.print_queue_manager import add_job_to_queue

	queued = []
	for index in range(max(1, int(copies))):
		markup = build_markup(receipt, copy_for="client" if index == 0 else "merchant")
		result = add_job_to_queue(
			job_token=f"payrexx-{intent_name}-{index}-{uuid.uuid4().hex[:8]}",
			printer_mac=mac,
			job_data=markup,
			media_types=[
				"application/vnd.star.starprnt",
				"application/vnd.star.line",
				"text/vnd.star.markup",
			],
		)
		queued.append(result)

	return {
		"status": "success",
		"intent": intent_name,
		"printer": mac,
		"copies": len(queued),
		"receipt": receipt,
	}


@frappe.whitelist()
def preview_text(intent_name: str) -> str:
	"""The receipt as it would come off the roll, markup stripped.

	Star markup is unreadable on screen — ``[magnify: width 2]`` tells you nothing
	about whether a line fits on 42 characters or wraps into an ugly mess. This
	renders what the paper will actually show, so the layout can be judged before a
	customer is the one judging it.
	"""
	import re

	markup = build_markup(_receipt_from_intent(intent_name))
	lines: list[str] = []
	centre = False
	for raw in markup.split("\n"):
		if "[align: centre]" in raw:
			centre = True
		if "[align: left]" in raw:
			centre = False
		text = re.sub(r"\[[^\]]*\]", "", raw)
		if not text.strip() and "[cut" in raw:
			continue
		lines.append(text.center(_WIDTH).rstrip() if centre and text.strip() else text)
	border = "+" + "-" * (_WIDTH + 2) + "+"
	return "\n".join([border] + [f"| {line:<{_WIDTH}} |" for line in lines] + [border])


@frappe.whitelist()
def preview_receipt(intent_name: str) -> str:
	"""The markup that would be printed, without printing it.

	Useful precisely because printing is the one step you cannot take back, and
	because a receipt is read by a customer — a layout mistake is visible to them
	before it is visible to us.
	"""
	return build_markup(_receipt_from_intent(intent_name))
