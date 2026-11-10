# //// Neoffice — added file (no upstream equivalent). The terminals card of « Payment settings »: every
# //// card terminal in one list — which provider, which till, whether it answers — and three gestures:
# //// add one (Payrexx by its pairing code, Wallee by picking it among the space's terminals), move it to
# //// another till, take it off. Jérémy (2026-11-10): « a simple terminal management, not as complicated as
# //// what we have now » — the terminal wizard asked for locations, configurations and devices first.
# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""Card terminals at the tills, managed from one list."""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _

from payments.setup.provider_setup import (
	offer_mode_on,
	terminal_tills,
	unwire_terminal,
	wire_terminal,
)

ROLES = ("System Manager", "Accounts Manager")


def _provider_of(device: dict[str, Any]) -> str | None:
	if not device.get("provider_channel_settings"):
		return None
	return frappe.db.get_value(
		"Provider Channel Settings", device["provider_channel_settings"], "provider"
	)


def overview() -> dict[str, Any]:
	"""The terminals, the tills, and which providers can add one."""
	devices = frappe.get_all(
		"Payment Device",
		filters={"enabled": 1},
		fields=[
			"name",
			"device_label",
			"serial_number",
			"provider_device_id",
			"provider_channel_settings",
			"status",
			"last_seen_at",
		],
		order_by="device_label asc",
	)
	rows = []
	for device in devices:
		provider = _provider_of(device)
		rows.append(
			{
				"name": device.name,
				"label": device.device_label or device.name,
				"serial": device.serial_number or device.provider_device_id,
				"provider": provider,
				"provider_label": frappe.db.get_value("Payment Provider", provider, "display_label")
				if provider
				else None,
				"status": device.status or "unknown",
				"last_seen_at": device.last_seen_at,
				"tills": terminal_tills(device.name),
			}
		)
	from payments.api import payrexx_setup, wallee_setup

	can_add = []
	if frappe.db.get_value("Payment Provider", payrexx_setup.PROVIDER, "enabled"):
		can_add.append("payrexx")
	if frappe.db.get_value("Payment Provider", wallee_setup._provider_name(), "enabled"):
		can_add.append("wallee")
	return {
		"devices": rows,
		"tills": frappe.get_all(
			"POS Profile", filters={"disabled": 0}, pluck="name", order_by="name asc"
		),
		"can_add": can_add,
	}


def _device_doc(device: str):  # noqa: ANN202
	if not frappe.db.exists("Payment Device", device):
		frappe.throw(_("Terminal {0} does not exist").format(device))
	return frappe.get_doc("Payment Device", device)


def _till_mode_of(provider: str) -> str:
	"""The till mode a provider's setup page made for its terminals."""
	from payments.api import payrexx_setup, wallee_setup

	if provider == payrexx_setup.PROVIDER:
		mode = payrexx_setup._status().get("till_mode")
	else:
		mode = wallee_setup._status().get("till_mode")
	if not mode:
		frappe.throw(
			_("Connect {0} on its page first: it prepares the till's payment mode.").format(provider)
		)
	return mode


def _wire(device: str, provider: str, pos_profile: str) -> None:
	if not frappe.db.exists("POS Profile", pos_profile):
		frappe.throw(_("Till {0} does not exist").format(pos_profile))
	mode = _till_mode_of(provider)
	if not offer_mode_on(pos_profile, mode):
		frappe.throw(
			_("Till {0} cannot offer {1}: no account for its company.").format(pos_profile, mode)
		)
	unwire_terminal(device, keep=pos_profile)
	wire_terminal(pos_profile, mode, device, provider)


# //// Neoffice — POST only (maintenance#1721): it changes which terminal a till drives.
@frappe.whitelist(methods=["POST"])
def move_terminal(device: str, pos_profile: str) -> dict[str, Any]:
	"""Wire the terminal to ``pos_profile`` and take it off every other till."""
	frappe.only_for(ROLES)
	doc = _device_doc(device)
	provider = _provider_of(doc.as_dict())
	if not provider:
		frappe.throw(_("Terminal {0} has no provider").format(device))
	_wire(device, provider, pos_profile)
	frappe.db.commit()
	return _page()


# //// Neoffice — POST only (maintenance#1721): it takes a terminal off the tills.
@frappe.whitelist(methods=["POST"])
def remove_terminal(device: str) -> dict[str, Any]:
	"""Take the terminal off every till and out of the list. Nothing changes at the provider."""
	frappe.only_for(ROLES)
	_device_doc(device)
	unwire_terminal(device)
	frappe.db.set_value("Payment Device", device, "enabled", 0)
	frappe.db.commit()
	return _page()


@frappe.whitelist()
def wallee_terminals() -> list[dict[str, Any]]:
	"""The active terminals of the Wallee space that no till has yet."""
	frappe.only_for(ROLES)
	from payments.api import wallee_setup
	from payments.integrations.wallee.api import _is_active, _space_terminals

	from payments.drivers.wallee.provider import WalleeProvider

	name = wallee_setup._provider_name()
	if not frappe.db.get_value("Payment Provider", name, "enabled"):
		frappe.throw(_("Wallee is not connected"))
	try:
		terminals = _space_terminals(WalleeProvider(frappe.get_doc("Payment Provider", name)))
	except Exception as exc:  # noqa: BLE001 - said on the page
		frappe.throw(_("Wallee did not answer: {0}").format(wallee_setup._wallee_message(exc)))
	known = set(frappe.get_all("Payment Device", filters={"enabled": 1}, pluck="provider_device_id"))
	out = []
	for terminal in terminals:
		if not _is_active(terminal) or str(terminal.id) in known:
			continue
		out.append(
			{
				"id": terminal.id,
				"name": getattr(terminal, "name", None) or str(terminal.id),
				"identifier": getattr(terminal, "identifier", None),
			}
		)
	return out


# //// Neoffice — POST only (maintenance#1721): it wires a terminal to a till.
@frappe.whitelist(methods=["POST"])
def add_wallee_terminal(terminal_id: str, pos_profile: str) -> dict[str, Any]:
	"""A terminal of the Wallee space, at a till."""
	frappe.only_for(ROLES)
	from payments.api import wallee_setup

	offered = {str(t["id"]): t for t in wallee_terminals()}
	terminal = offered.get(str(terminal_id))
	if not terminal:
		frappe.throw(
			_("Wallee terminal {0} is not active in your space, or a till has it already").format(
				terminal_id
			)
		)
	provider = wallee_setup._provider_name()
	pcs = frappe.db.get_value(
		"Provider Channel Settings", {"provider": provider, "channel": "terminal"}, "name"
	)
	if not pcs:
		frappe.throw(_("Connect Wallee on its page first."))
	device = frappe.db.get_value("Payment Device", {"provider_device_id": str(terminal_id)}, "name")
	if device:
		frappe.db.set_value("Payment Device", device, {"enabled": 1, "provider_channel_settings": pcs})
	else:
		doc = frappe.get_doc(
			{
				"doctype": "Payment Device",
				"device_label": f"Wallee {terminal['name']}",
				"provider_device_id": str(terminal_id),
				"serial_number": terminal.get("identifier"),
				"provider_channel_settings": pcs,
				"enabled": 1,
				"status": "online",
			}
		)
		doc.insert(ignore_permissions=True)
		device = doc.name
	_wire(device, provider, pos_profile)
	frappe.db.commit()
	return _page()


def _page() -> dict[str, Any]:
	"""What the Payment settings page shows, after a change."""
	from payments.api.payment_settings import get_overview

	return get_overview()
