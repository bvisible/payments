#//// Neoffice — added file (no upstream equivalent). A probe, not a test: it answers the one
#//// question the whole Tap to Pay design depended on and that no documentation
#//// settles — does the `order_reference` handed to the SDK come back as
#//// `referenceId` in the webhook? If not, a phone-initiated payment cannot be tied
#//// to a Payment Intent and the design changes. Kept as the record of the answer.
#//// Commits: f9dc031 2026-08-11 "test(payrexx): probe what a Tap to Pay webhook
#//// actually contains".
# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""Read what Payrexx actually sends for a Tap to Pay payment.

Answers the one question the Tap to Pay design depends on and that no
documentation settles: **does the ``order_reference`` given to the SDK come back as
``referenceId`` in the webhook?** If it does, a phone-initiated payment can be tied
to a Payment Intent and the whole flow holds. If it does not, the link has to come
from somewhere else and the design changes.

There is nothing to build before running this. Type a recognisable reference by hand
into the Payrexx Tap to Pay app, take a CHF 1.50 payment with a real card, then::

    bench --site <site> execute payments.tests.payrexx_taptopay_probe.run

Read-only: it inspects Webhook Event Log rows already stored by
:mod:`payments.api.webhook_payrexx`. It creates nothing, changes nothing, and takes
no money.

See Neoffice/Payments/Payrexx/03-Tap-To-Pay-Mobile §9bis for the full procedure.
"""

from __future__ import annotations

import json
from typing import Any

import frappe


def run(limit: int = 25, expect: str | None = None) -> None:
	"""Print every recent Payrexx delivery, with its type and reference.

	Args:
		limit: how many recent deliveries to inspect.
		expect: the reference typed into the Payrexx app. When given, the probe says
			outright whether it came through — which is the actual question.
	"""
	rows = frappe.get_all(
		"Webhook Event Log",
		filters={"provider": ["like", "payrexx%"]},
		fields=["name", "event_id", "event_type", "status", "intent", "creation", "raw_payload"],
		order_by="creation desc",
		limit_page_length=limit,
	)

	if not rows:
		print("No Payrexx delivery recorded on this site yet.")
		print("Check the webhook is registered in the Payrexx back office, and that a")
		print("payment was actually taken — an unpaid attempt sends nothing.")
		return

	print(f"{'when':<20} {'type':<22} {'referenceId':<28} {'status':<11} intent")
	print("-" * 104)

	tap_to_pay: list[dict[str, Any]] = []
	found_expected = False

	for row in rows:
		tx = _transaction(row.raw_payload)
		tx_type = str(tx.get("type") or "-")
		reference = tx.get("referenceId") or tx.get("reference_id") or ""
		# The ECR/Tap to Pay side names it order_reference; look under both spellings
		# rather than assume they are unified.
		reference = reference or tx.get("order_reference") or tx.get("orderReference") or ""

		print(
			f"{str(row.creation)[:19]:<20} {tx_type[:22]:<22} {str(reference)[:28]:<28} "
			f"{(row.status or '-'):<11} {row.intent or '-'}"
		)

		if "tap" in tx_type.lower():
			tap_to_pay.append({"type": tx_type, "reference": reference, "status": tx.get("status")})
		if expect and str(reference) == expect:
			found_expected = True

	print()
	if expect:
		if found_expected:
			print(f"✅ CHAINING CONFIRMED — '{expect}' came back as referenceId.")
			print("   A Tap to Pay payment can carry a Payment Intent name. The design in")
			print("   03-Tap-To-Pay-Mobile §6 holds as written.")
		else:
			print(f"❌ '{expect}' was NOT found in any referenceId.")
			print("   Either the reference was not typed into the payment, or Payrexx does not")
			print("   propagate order_reference to the webhook. Before concluding, read a raw")
			print("   payload below and look for the value under another key — the answer")
			print("   changes the design, so it is worth being sure.")

	if tap_to_pay:
		print(f"\nTap to Pay deliveries seen: {len(tap_to_pay)}")
		for entry in tap_to_pay:
			print(f"  type={entry['type']!r} status={entry['status']!r} reference={entry['reference']!r}")
		print("\nNote the exact `type` string: webhook_payrexx routes on it, and the")
		print("documentation's \"Tap to Pay\" has never been seen on a real delivery.")
	else:
		print("\nNo delivery carried a Tap to Pay type. Either none was taken, or the type")
		print("string differs from what the documentation says — compare the `type` column")
		print("above against 'E-Commerce' and 'POS-Terminal'.")

	print("\n--- most recent raw payload, for the keys the table does not show ---")
	print(_pretty(rows[0].raw_payload)[:1500])


def _transaction(raw_payload: str | None) -> dict[str, Any]:
	"""The transaction object of a delivery, or an empty dict."""
	try:
		payload = json.loads(raw_payload or "{}")
	except (ValueError, TypeError):
		return {}
	tx = payload.get("transaction")
	return tx if isinstance(tx, dict) else {}


def _pretty(raw_payload: str | None) -> str:
	try:
		return json.dumps(json.loads(raw_payload or "{}"), indent=2, ensure_ascii=False)
	except (ValueError, TypeError):
		return str(raw_payload)
