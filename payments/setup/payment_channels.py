#//// Neoffice — added file (no upstream equivalent). The one idempotent registry of the
#//// Payment Channels this app ships, provisioned from `after_install`. Until 2026-09-03
#//// each channel was created by the patch that introduced it (v15_03 … v15_08), which
#//// works on a site that migrates and never on one that is installed — `bench
#//// install-app` marks every patch completed without running it — so a fresh site had
#//// no channel at all and installing payments died on a missing Payment Channel,
#//// taking every CI that installs payments as a dependency with it. Upstream has no
#//// Payment Channel doctype, so nothing to provision.
#//// Commits: 7a0f7ca 2026-09-03 "fix(install): provision the shipped Payment Channels on a fresh site"
# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""The Payment Channels this app ships, provisioned once per site.

A channel is *how* a payment is taken — a hosted redirect, a QR the customer scans,
a card presented to a phone or to a terminal. Providers bind to channels through
Provider Channel Settings; the driver registry resolves a driver from that binding.

Until 2026-09-03 each channel was created by the patch that introduced it
(v15_03 … v15_08). That works on a site that *migrates* and never on a site that is
*installed*: ``bench install-app`` marks every patch as completed without running it
(``frappe.installer.set_all_patches_as_completed``), so a fresh site had no channel
at all. Mobile Payment Settings made it visible — its ``on_update`` refuses to bind a
provider to a channel that does not exist, and frappe saves every Single once during
install (``frappe.installer.init_singles``) — and every CI that installs payments as a
dependency went red, as would any new instance of the fleet.

``provision_payment_channels`` is idempotent. It runs from ``after_install``
(hooks.py) and from the mobile-channels patch; the older patches keep their own copy
for the sites that already ran them. A new channel is added HERE and provisioned by a
patch that calls this function with its code.
"""

from __future__ import annotations

import json
from collections.abc import Iterable

import frappe

CHANNELS: list[dict] = [
	{
		"channel_code": "terminal",
		"display_label": "POS Terminal",
		"ui_kind": "card_present_modal",
		"capabilities": {"supports_refund": True},
	},
	{
		"channel_code": "twint_web",
		"display_label": "TWINT Web (QR consumer)",
		"ui_kind": "qr_display",
		"capabilities": {
			"supports_refund": True,
			"supports_partial_refund": True,
			"async": True,
			"requires_qr_scan": True,
			"requires_device": False,
		},
	},
	{
		"channel_code": "wallee_web",
		"display_label": "Wallee Web (hosted checkout)",
		"ui_kind": "redirect",
		"capabilities": {
			"supports_refund": True,
			"supports_partial_refund": True,
			"async": True,
			"requires_redirect": True,
			"requires_device": False,
		},
	},
	{
		"channel_code": "payrexx_web",
		"display_label": "Payrexx Web Checkout",
		"icon": "🌐",
		"ui_kind": "redirect",
		"capabilities": {
			"supports_refund": True,
			"supports_partial_refund": True,
			"supports_tip": False,
			"async": True,
			"requires_device": False,
			"requires_redirect": True,
		},
	},
	{
		"channel_code": "payrexx_tap_to_pay",
		"display_label": "Payrexx Tap to Pay",
		"icon": "📲",
		"ui_kind": "card_present_modal",
		"capabilities": {
			"supports_refund": True,
			"supports_partial_refund": True,
			# The SDK's Sale takes a tip alongside the amount.
			"supports_tip": True,
			"async": True,
			# The phone is not a registered Payment Device: pairing lives in the
			# Payrexx app, on their side.
			"requires_device": False,
			"requires_redirect": False,
		},
	},
	{
		# The card is tapped on the phone, inside our app, through the Stripe Terminal
		# SDK. Its own channel rather than ``terminal``: no device is addressed from
		# the server, capture is automatic, and reporting wants the two apart.
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
		# The same merchant-presented QR as the shop, drawn by the app. Its own channel
		# rather than ``twint_web``: a payment at the customer's door is not a webshop
		# order, and the shop's settlement hook must not run on it.
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


def provision_payment_channels(codes: Iterable[str] | None = None) -> list[str]:
	"""Create the shipped channels that are missing on this site.

	Idempotent — an existing channel is never touched (its label or capabilities may
	have been tuned by the merchant). ``codes`` restricts the pass to some channels,
	which is what a patch introducing new ones passes. Returns the codes created.
	Creates no provider and no binding: those are the merchant's choice.
	"""
	wanted = set(codes) if codes else None
	created: list[str] = []
	for spec in CHANNELS:
		code = spec["channel_code"]
		if wanted is not None and code not in wanted:
			continue
		if frappe.db.exists("Payment Channel", code):
			continue
		frappe.get_doc(
			{
				"doctype": "Payment Channel",
				"channel_code": code,
				"display_label": spec["display_label"],
				"icon": spec.get("icon"),
				"ui_kind": spec["ui_kind"],
				"capabilities_json": json.dumps(spec["capabilities"], indent=2),
			}
		).insert(ignore_permissions=True)
		created.append(code)
	return created
