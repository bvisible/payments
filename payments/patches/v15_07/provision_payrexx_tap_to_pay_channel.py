# //// Neoffice — added file (no upstream equivalent). Creates the `payrexx_tap_to_pay`
# //// Payment Channel on the sites that already exist. Its own channel rather than a
# //// reuse of `terminal`: a terminal payment is driven by REST to a device addressed by
# //// serial, a Tap to Pay payment is handed off to another app on the operator's phone
# //// and cannot be initiated from the server at all — and reporting wants them apart,
# //// the fees differ. Creates no provider and no binding: enabling Tap to Pay for a
# //// client is a commercial act, billed per active device.
# //// Commits: a8087dc 2026-08-11 "feat(payrexx): Tap to Pay server lot — the phone initiates, the server records"
# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""Create the ``payrexx_tap_to_pay`` Payment Channel.

Its own channel rather than a reuse of ``terminal``: the two behave differently
enough that sharing one would blur the distinction. A terminal payment is driven by
REST calls to a device we address by serial; a Tap to Pay payment is handed off to
another app on the operator's phone and cannot be initiated from the server at all.
Reporting also wants them apart — the fees differ (1.65% + 0.15 versus 1.25% + 0.15).

``ui_kind`` is ``card_present_modal`` because that is the closest of the five values
the doctype allows, and it is true as far as it goes: the operator is waiting for a
card. What differs is *who* draws the screen — the Payrexx app, not us — and that is
carried precisely by ``next_action_type="native_app_handoff"`` on the intent.

Idempotent — safe to re-run. Creates no Payment Provider and no binding: credentials
are per client, and enabling Tap to Pay for a client is a commercial act (it is
billed separately, with a monthly fee per active device).
"""

from __future__ import annotations

import json

import frappe

CHANNEL = "payrexx_tap_to_pay"


def execute() -> None:
	if frappe.db.exists("Payment Channel", CHANNEL):
		return

	frappe.get_doc(
		{
			"doctype": "Payment Channel",
			"channel_code": CHANNEL,
			"display_label": "Payrexx Tap to Pay",
			"icon": "📲",
			"ui_kind": "card_present_modal",
			"capabilities_json": json.dumps(
				{
					"supports_refund": True,
					"supports_partial_refund": True,
					# The SDK's Sale takes a tip alongside the amount.
					"supports_tip": True,
					"async": True,
					# The phone is not a registered Payment Device: pairing lives in
					# the Payrexx app, on their side.
					"requires_device": False,
					"requires_redirect": False,
				},
				indent=2,
			),
		}
	).insert(ignore_permissions=True)
