#//// Neoffice — added file (no upstream equivalent). Creates the `payrexx_web` Payment
#//// Channel on the sites that already exist. Only the web channel needs creating:
#//// `terminal` already exists and is shared with Stripe Terminal and Wallee, since the
#//// Provider Channel Settings binding is what decides which driver a POS profile
#//// resolves to. No Payment Provider is created — credentials are per client.
#//// Commits: 4c05756 2026-08-11 "feat(payrexx): add Payrexx as a third payment provider"
# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""Create the ``payrexx_web`` Payment Channel.

Only the web channel needs creating: ``terminal`` already exists and is shared
with Stripe Terminal and Wallee, since the Provider Channel Settings binding is
what decides which driver a POS profile resolves to.

Idempotent — safe to re-run. No Payment Provider record is created here: the
credentials are per client and must be entered by an operator.
"""

from __future__ import annotations

import json

import frappe


def execute() -> None:
	if frappe.db.exists("Payment Channel", "payrexx_web"):
		return

	frappe.get_doc(
		{
			"doctype": "Payment Channel",
			"channel_code": "payrexx_web",
			"display_label": "Payrexx Web Checkout",
			"icon": "🌐",
			"ui_kind": "redirect",
			"capabilities_json": json.dumps(
				{
					"supports_refund": True,
					"supports_partial_refund": True,
					"supports_tip": False,
					"async": True,
					"requires_device": False,
					"requires_redirect": True,
				},
				indent=2,
			),
		}
	).insert(ignore_permissions=True)
