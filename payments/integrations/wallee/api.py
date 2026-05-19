# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""Wallee wizard helpers — config sync + terminal registration.

These methods power the ``payment_terminal_wizard`` Frappe Page. They are NOT
called during normal payment flows (the driver handles those) — only during
admin onboarding (list configurations / locations from Wallee, create a
terminal in Wallee, link a physical device by serial number).

All endpoints take a ``provider`` argument (Payment Provider name, e.g.
``wallee_test``) so the operator can target a specific Wallee account when
several cohabit on the same site.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _
from frappe.utils import now_datetime

from payments.drivers.wallee.provider import WalleeProvider


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _provider(provider_name: str) -> WalleeProvider:
	"""Resolve and instantiate WalleeProvider from a Payment Provider name."""
	if not provider_name:
		frappe.throw(_("provider is required"))
	provider_doc = frappe.get_doc("Payment Provider", provider_name)
	driver_cls = (provider_doc.driver_class or "").strip()
	if "payments.drivers.wallee" not in driver_cls:
		frappe.throw(
			_("Payment Provider {0} is not a Wallee provider (driver_class={1})").format(
				provider_name, driver_cls or "<empty>"
			)
		)
	return WalleeProvider(provider_doc)


def _terminal_binding(provider_name: str) -> str:
	"""Return the Provider Channel Settings name for (provider, terminal)."""
	binding = frappe.db.get_value(
		"Provider Channel Settings",
		{"provider": provider_name, "channel": "terminal"},
		"name",
	)
	if not binding:
		frappe.throw(
			_("No Provider Channel Settings for {0} × terminal — create the binding first").format(
				provider_name
			)
		)
	return binding


# ---------------------------------------------------------------------------
# Connection test
# ---------------------------------------------------------------------------


@frappe.whitelist()
def test_connection(provider: str) -> dict[str, Any]:
	"""Probe Wallee reachability for a given Payment Provider.

	Returns ``{ok: bool, space_id?, mode?, error?}``.
	"""
	return _provider(provider).health_check()


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------


@frappe.whitelist()
def get_existing_locations(provider: str) -> list[dict[str, Any]]:
	"""Return all Wallee Location records (local cache).

	The local records are populated by :func:`sync_locations_from_wallee` or
	manually by the operator. This endpoint just lists them — no API call.
	"""
	_provider(provider)  # validate access
	return frappe.get_all(
		"Wallee Location",
		fields=[
			"name",
			"location_name",
			"wallee_location_id",
			"wallee_location_version_id",
			"city",
			"country",
			"is_active",
			"is_default",
		],
		filters={"is_active": 1},
		order_by="is_default desc, location_name",
	)


@frappe.whitelist()
def sync_locations_from_wallee(provider: str) -> dict[str, Any]:
	"""Pull all Locations from the Wallee account and upsert local records.

	Returns ``{created, updated, total}``.
	"""
	from wallee.models import EntityQuery
	from wallee.service.payment_terminals_service import PaymentTerminalsService  # noqa: F401 — keeps wallee SDK imports resident

	prov = _provider(provider)
	config = prov.build_configuration()

	# Wallee Location is a separate service in the SDK.
	from wallee.service.payment_terminal_locations_service import (
		PaymentTerminalLocationsService,
	)

	service = PaymentTerminalLocationsService(config)
	# Empty query = first page of all locations in the space.
	query = EntityQuery()
	response = service.search(prov.space_id, query)

	stats = {"created": 0, "updated": 0, "total": len(response or [])}
	for loc in response or []:
		loc_id_str = str(loc.id)
		name = loc.name or f"Location {loc_id_str}"

		existing = frappe.db.get_value("Wallee Location", {"wallee_location_id": loc_id_str})
		if existing:
			doc = frappe.get_doc("Wallee Location", existing)
			doc.location_name = name
			doc.wallee_location_version_id = str(getattr(loc, "version", "") or "")
			doc.last_sync = now_datetime()
			doc.save(ignore_permissions=True)
			stats["updated"] += 1
		else:
			frappe.get_doc(
				{
					"doctype": "Wallee Location",
					"location_name": name,
					"wallee_location_id": loc_id_str,
					"wallee_location_version_id": str(getattr(loc, "version", "") or ""),
					"city": getattr(loc, "city", None) or "Unknown",
					"country": getattr(loc, "country", None) or "Switzerland",
					"is_active": 1,
					"last_sync": now_datetime(),
				}
			).insert(ignore_permissions=True)
			stats["created"] += 1
	frappe.db.commit()
	return stats


# ---------------------------------------------------------------------------
# Terminal Configurations
# ---------------------------------------------------------------------------


@frappe.whitelist()
def get_existing_configurations(provider: str) -> list[dict[str, Any]]:
	_provider(provider)
	return frappe.get_all(
		"Wallee Terminal Configuration",
		fields=[
			"name",
			"configuration_name",
			"wallee_configuration_id",
			"wallee_configuration_version_id",
			"description",
			"is_default",
		],
		order_by="is_default desc, configuration_name",
	)


@frappe.whitelist()
def sync_configurations_from_wallee(provider: str) -> dict[str, Any]:
	"""Pull all Terminal Configurations from Wallee and upsert local records."""
	from wallee.models import EntityQuery
	from wallee.service.payment_terminal_configurations_service import (
		PaymentTerminalConfigurationsService,
	)

	prov = _provider(provider)
	config = prov.build_configuration()
	service = PaymentTerminalConfigurationsService(config)
	response = service.search(prov.space_id, EntityQuery())

	stats = {"created": 0, "updated": 0, "total": len(response or [])}
	for cfg in response or []:
		cfg_id_str = str(cfg.id)
		name = cfg.name or f"Config {cfg_id_str}"

		existing = frappe.db.get_value(
			"Wallee Terminal Configuration", {"wallee_configuration_id": cfg_id_str}
		)
		if existing:
			doc = frappe.get_doc("Wallee Terminal Configuration", existing)
			doc.configuration_name = name
			doc.wallee_configuration_version_id = str(getattr(cfg, "version", "") or "")
			doc.last_sync = now_datetime()
			doc.save(ignore_permissions=True)
			stats["updated"] += 1
		else:
			frappe.get_doc(
				{
					"doctype": "Wallee Terminal Configuration",
					"configuration_name": name,
					"wallee_configuration_id": cfg_id_str,
					"wallee_configuration_version_id": str(getattr(cfg, "version", "") or ""),
					"description": "",
					"last_sync": now_datetime(),
				}
			).insert(ignore_permissions=True)
			stats["created"] += 1
	frappe.db.commit()
	return stats


# ---------------------------------------------------------------------------
# Terminal create + link
# ---------------------------------------------------------------------------


@frappe.whitelist()
def create_terminal(
	provider: str,
	terminal_name: str,
	configuration: str,
	location: str,
	device_label: str | None = None,
) -> dict[str, Any]:
	"""Create a Wallee Payment Terminal and persist a Payment Device locally.

	Args:
	  ``provider``        : Payment Provider name (e.g. ``wallee_test``)
	  ``terminal_name``   : user-facing name on the Wallee side
	  ``configuration``   : local ``Wallee Terminal Configuration`` name
	  ``location``        : local ``Wallee Location`` name
	  ``device_label``    : optional friendly label for the Payment Device

	Returns ``{payment_device, wallee_terminal_id, device_type, location, status}``.
	"""
	from wallee.models import PaymentTerminalCreate
	from wallee.service.payment_terminals_service import PaymentTerminalsService

	prov = _provider(provider)
	config = prov.build_configuration()

	cfg_doc = frappe.get_doc("Wallee Terminal Configuration", configuration)
	loc_doc = frappe.get_doc("Wallee Location", location)

	if not cfg_doc.wallee_configuration_version_id:
		frappe.throw(_("Configuration {0} has no Wallee version id — sync first").format(configuration))
	if not loc_doc.wallee_location_id:
		frappe.throw(_("Location {0} has no Wallee location id — sync first").format(location))

	service = PaymentTerminalsService(config)

	terminal_create = PaymentTerminalCreate(
		name=terminal_name,
		configuration_version=cfg_doc.wallee_configuration_version_id,
		location_version=loc_doc.wallee_location_version_id or None,
	)
	response = service.post_payment_terminals(
		space=prov.space_id, payment_terminal_create=terminal_create
	)

	binding = _terminal_binding(provider)

	device_doc = frappe.get_doc(
		{
			"doctype": "Payment Device",
			"device_label": device_label or terminal_name,
			"provider_device_id": str(response.id),
			"device_type": "wallee_terminal",
			"provider_channel_settings": binding,
			"location_ref": loc_doc.wallee_location_id,
			"status": "online" if getattr(response, "state", None) else "unknown",
			"last_seen_at": now_datetime(),
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()

	return {
		"payment_device": device_doc.name,
		"wallee_terminal_id": str(response.id),
		"device_type": "wallee_terminal",
		"location": loc_doc.wallee_location_id,
		"status": device_doc.status,
	}


@frappe.whitelist()
def link_terminal_device(provider: str, payment_device: str, serial_number: str) -> dict[str, Any]:
	"""Link a physical device (by serial number) to an existing Wallee terminal.

	The Payment Device record's ``serial_number`` is updated; the Wallee API
	call associates the SIM/hardware with the terminal in the Wallee dashboard.
	"""
	from wallee.service.payment_terminals_service import PaymentTerminalsService

	prov = _provider(provider)
	device_doc = frappe.get_doc("Payment Device", payment_device)
	if not device_doc.provider_device_id:
		frappe.throw(_("Payment Device {0} has no provider_device_id").format(payment_device))

	config = prov.build_configuration()
	service = PaymentTerminalsService(config)

	service.post_payment_terminals_id_link(
		id=int(device_doc.provider_device_id),
		serial_number=serial_number,
		space=prov.space_id,
	)

	device_doc.serial_number = serial_number
	device_doc.last_seen_at = now_datetime()
	device_doc.save(ignore_permissions=True)
	frappe.db.commit()

	return {"payment_device": device_doc.name, "serial_number": serial_number, "linked": True}


# ---------------------------------------------------------------------------
# Static helpers
# ---------------------------------------------------------------------------


@frappe.whitelist()
def get_terminal_types() -> list[dict[str, Any]]:
	"""Return the list of supported Wallee terminal hardware models.

	This is currently hard-coded from the Wallee documentation. A future
	extension could fetch it from the Wallee API when their SDK exposes it.
	"""
	return [
		{
			"id": "1568379599819",
			"name": "BBPOS WisePOS E",
			"description": "Bluetooth + Wi-Fi, écran tactile noir cylindrique. Modèle le plus courant.",
		},
		{
			"id": "wisepos_pro",
			"name": "BBPOS WisePOS Pro",
			"description": "Successeur WisePOS, écran plus large.",
		},
	]
