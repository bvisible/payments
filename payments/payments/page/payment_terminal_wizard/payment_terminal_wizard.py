# //// Neoffice — added file (no upstream equivalent). Backend of the
# //// `payment-terminal-wizard` desk page (the page itself is JS, no DocType): the
# //// Step 1 provider chooser, which derives kind = stripe / wallee / other from
# //// `driver_class`, and the Step 4 binding of a `Payment Device` into
# //// `POS Profile.custom_active_payment_devices`. Provider-specific endpoints stay in
# //// `payments.api.terminal` and `payments.integrations.wallee.api`. Came in with the
# //// fold-in of the retired `wallee_integration` app (99e929c, ADR-005), replacing its
# //// `wallee_terminal_wizard`. Upstream enrols no hardware — it builds checkout URLs.
# //// Commits: 99e929c 2026-05-19 "feat(payments): merge wallee_integration into payments — ADR-005"
# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""Backend helpers for the unified ``payment_terminal_wizard`` Frappe Page.

The wizard itself is a JavaScript page (no DocType — see
``payment_terminal_wizard.js``). The whitelisted endpoints below are
shared across both Stripe and Wallee provider branches:

- :func:`list_terminal_providers` — Step 1 (provider chooser)
- :func:`link_device_to_pos_profile` — Step 4 (POS Profile binding)

Provider-specific endpoints live in:
- ``payments.api.terminal``                 (Stripe: locations, readers)
- ``payments.integrations.wallee.api``      (Wallee: locations, configs, terminals)
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _


# ---------------------------------------------------------------------------
# Step 1 — Provider chooser
# ---------------------------------------------------------------------------


@frappe.whitelist()
def list_terminal_providers() -> list[dict[str, Any]]:
	"""Return Payment Providers that expose a ``terminal``-class driver.

	The wizard uses this for the Step 1 dropdown. We filter on
	``driver_class`` (matches Stripe Terminal / Wallee Terminal / future PSPs)
	and only return enabled providers.

	Each row carries a ``kind`` field (``stripe`` / ``wallee`` / ``other``)
	derived from the driver_class so the Vue/jQuery wizard can pick the
	right Step 2/3 components.
	"""
	providers = frappe.get_all(
		"Payment Provider",
		filters={"enabled": 1},
		fields=["name", "display_label", "mode", "driver_class"],
		order_by="display_label",
	)
	out = []
	for p in providers:
		dc = (p.get("driver_class") or "").lower()
		if "stripe" in dc and "terminal" in dc:
			kind = "stripe"
		elif "wallee" in dc and ("terminal" in dc or "web" in dc):
			# Wallee providers serve both `terminal` and `wallee_web` from the
			# same Payment Provider record (one Wallee Settings backs both).
			kind = "wallee"
		elif "terminal" in dc:
			kind = "other"
		else:
			# Skip non-terminal drivers (TWINT qr_bridge, future web-only).
			continue
		p["kind"] = kind
		out.append(p)
	return out


# ---------------------------------------------------------------------------
# Step 4 — Link to POS Profile
# ---------------------------------------------------------------------------


@frappe.whitelist()
def list_pos_profiles() -> list[dict[str, Any]]:
	"""Return active POS Profiles for the wizard's Step 4."""
	return frappe.get_all(
		"POS Profile",
		filters={"disabled": 0},
		fields=["name", "company", "warehouse"],
		order_by="name",
	)


@frappe.whitelist()
def link_device_to_pos_profile(
	payment_device: str,
	pos_profile: str,
	mode_of_payment: str,
	is_default: int = 0,
) -> dict[str, Any]:
	"""Add the Payment Device to ``POS Profile.custom_active_payment_devices``.

	Idempotent: if the same (mode_of_payment, payment_device) row already
	exists on the profile, returns the existing row name without re-inserting.
	"""
	if not (payment_device and pos_profile and mode_of_payment):
		frappe.throw(_("payment_device, pos_profile and mode_of_payment are required"))

	profile = frappe.get_doc("POS Profile", pos_profile)
	for row in profile.get("custom_active_payment_devices") or []:
		if row.payment_device == payment_device and row.mode_of_payment == mode_of_payment:
			return {
				"pos_profile": pos_profile,
				"row": row.name,
				"already_present": True,
			}

	profile.append(
		"custom_active_payment_devices",
		{
			"mode_of_payment": mode_of_payment,
			"payment_device": payment_device,
			"is_default": int(is_default) if is_default else 0,
		},
	)
	profile.save(ignore_permissions=True)
	frappe.db.commit()

	# Find the row we just added (last one with this device).
	added = next(
		(r for r in profile.custom_active_payment_devices if r.payment_device == payment_device),
		None,
	)
	return {
		"pos_profile": pos_profile,
		"row": added.name if added else None,
		"already_present": False,
	}


@frappe.whitelist()
def list_modes_of_payment_for_profile(pos_profile: str) -> list[dict[str, Any]]:
	"""Return Modes of Payment declared on this POS Profile.

	Used in Step 4 to constrain the MoP dropdown to ones the cashier can
	actually pick at checkout. Falls back to the global list if the profile
	has no payments table.
	"""
	if not pos_profile:
		return []
	profile = frappe.get_doc("POS Profile", pos_profile)
	rows = profile.get("payments") or []
	if not rows:
		return frappe.get_all("Mode of Payment", filters={"enabled": 1}, fields=["name"])
	return [{"name": r.mode_of_payment} for r in rows if r.mode_of_payment]
