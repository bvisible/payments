# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""Create the two channels a payment taken on the operator's phone can use.

- ``stripe_tap_to_pay`` — the card is tapped on the phone, inside our app, through
  the Stripe Terminal SDK. Its own channel rather than ``terminal``: no device is
  addressed from the server, capture is automatic, and reporting wants the two apart.
- ``twint_mobile`` — the same merchant-presented QR as the shop, drawn by the app.
  Its own channel rather than ``twint_web``: a payment at the customer's door is not
  a webshop order, and the shop's settlement hook must not run on it.

Idempotent — safe to re-run. Creates no provider and no binding: those are the
merchant's choice, made once in Mobile Payment Settings.
"""

from __future__ import annotations

import json

import frappe

CHANNELS = [
	{
		"channel_code": "stripe_tap_to_pay",
		"display_label": "Tap to Pay",
		"icon": "📱",
		"ui_kind": "card_present_modal",
		"capabilities": {
			"supports_refund": True,
			"supports_partial_refund": True,
			"supports_tip": False,
			"async": True,
			"requires_device": False,
			"requires_redirect": False,
		},
	},
	{
		"channel_code": "twint_mobile",
		"display_label": "TWINT sur place",
		"icon": "📲",
		"ui_kind": "qr_display",
		"capabilities": {
			"supports_refund": True,
			"supports_partial_refund": True,
			"supports_tip": False,
			"async": True,
			"requires_qr_scan": True,
			"requires_device": False,
			"requires_redirect": False,
		},
	},
]


def execute() -> None:
	for spec in CHANNELS:
		if frappe.db.exists("Payment Channel", spec["channel_code"]):
			continue
		frappe.get_doc(
			{
				"doctype": "Payment Channel",
				"channel_code": spec["channel_code"],
				"display_label": spec["display_label"],
				"icon": spec["icon"],
				"ui_kind": spec["ui_kind"],
				"capabilities_json": json.dumps(spec["capabilities"], indent=2),
			}
		).insert(ignore_permissions=True)
