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


def _resolve_wallee_provider_name(provider: str | None) -> str:
	"""Return a Wallee Payment Provider name — explicit, else first enabled."""
	if provider:
		return provider
	name = frappe.db.get_value(
		"Payment Provider",
		{"driver_class": ["like", "payments.drivers.wallee.%"], "enabled": 1},
		"name",
		order_by="modified desc",
	)
	if not name:
		frappe.throw(_("No enabled Wallee Payment Provider found"))
	return name


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


# ---------------------------------------------------------------------------
# Webshop helpers — hosted checkout (Redirect / Lightbox / iFrame)
# ---------------------------------------------------------------------------
#
# These endpoints are called from the webshop checkout JS once a Payment
# Request has been created via the generic ``payment_handler.create_payment_request``.
# They wrap the existing :mod:`payments.drivers.wallee.web_driver` to:
#   1. create a Wallee transaction (via the unified Payment Intent FSM)
#   2. surface the right JS/redirect URL according to the requested mode
#
# Pre-fusion this lived in ``wallee_integration.api.transaction`` and
# ``webshop.templates.payments.wallee`` directly imported it; that module is
# gone, hence the port here.


def _build_line_items_from_quotation(quotation_doc) -> list[dict[str, Any]]:  # noqa: ANN001
	"""Map a Quotation to the dict format expected by ``WalleeWebDriver``.

	Mirrors what the legacy ``webshop.templates.payments.wallee`` did inline:
	one PRODUCT line per item, plus one SHIPPING/FEE line per ``Actual``
	tax-row (shipping, handling, etc.). Taxes are NOT attached here — caller
	can pass percentage-tax info via ``metadata['line_items'][i].taxes`` if
	needed (Wallee accepts items without explicit tax breakdown).
	"""
	# Wallee requires each line item's ``unique_id`` to be distinct WITHIN the
	# transaction. We assign an explicit indexed unique_id so two products
	# with the same SKU — or two ``Actual`` charges — never collide
	# (collision → Wallee HTTP 422 "The unique id ... is already used").
	line_items: list[dict[str, Any]] = []
	idx = 0
	for item in quotation_doc.items or []:
		idx += 1
		line_items.append(
			{
				"name": item.item_name or item.item_code,
				"sku": item.item_code,
				"unique_id": f"li-{idx}-{item.item_code}",
				"quantity": float(item.qty or 1),
				"amount_including_tax": float(item.amount or 0),
				"type": "PRODUCT",
			}
		)
	# Actual charges (shipping / fees not included in item prices) become
	# their own line items so the totals match Wallee's expectation.
	for tax_row in (quotation_doc.taxes or []):
		if tax_row.charge_type == "Actual" and (tax_row.tax_amount or 0) > 0:
			idx += 1
			desc_lower = (tax_row.description or "").lower()
			is_shipping = any(
				kw in desc_lower for kw in ("ship", "post", "livraison", "envoi", "frais de port")
			)
			line_items.append(
				{
					"name": tax_row.description or "Fee",
					"unique_id": f"li-{idx}-charge",
					"quantity": 1,
					"amount_including_tax": float(tax_row.tax_amount),
					"type": "SHIPPING" if is_shipping else "FEE",
				}
			)
	return line_items


def _build_billing_address_from_quotation(quotation_doc) -> dict[str, Any] | None:  # noqa: ANN001
	"""Best-effort mapping of Quotation → Wallee billing address dict."""
	if not (quotation_doc.contact_email or quotation_doc.customer_address):
		return None

	addr: dict[str, Any] = {"email_address": quotation_doc.contact_email or None}

	if quotation_doc.contact_display:
		parts = quotation_doc.contact_display.strip().split(" ", 1)
		addr["given_name"] = parts[0] if parts else ""
		addr["family_name"] = parts[1] if len(parts) > 1 else ""

	if quotation_doc.customer_address:
		try:
			addr_doc = frappe.get_doc("Address", quotation_doc.customer_address)
			addr["street"] = addr_doc.address_line1
			addr["city"] = addr_doc.city
			addr["postcode"] = addr_doc.pincode
			if addr_doc.country:
				country_code = frappe.db.get_value("Country", addr_doc.country, "code")
				addr["country"] = (country_code or "").upper()
		except frappe.DoesNotExistError:
			pass

	return addr


@frappe.whitelist(allow_guest=True)
def create_web_transaction(
	payment_request_id: str,
	mode: str = "Redirect",
	line_items: list[dict[str, Any]] | str | None = None,
	billing_address: dict[str, Any] | str | None = None,
	success_url: str | None = None,
	failed_url: str | None = None,
	provider: str | None = None,
) -> dict[str, Any]:
	"""Create a Wallee web transaction and return the URL appropriate for ``mode``.

	Webshop pivot endpoint. Wraps :func:`payments.api.intent.create_intent` and
	enriches the response with Lightbox / iFrame URLs when needed.

	Args:
	  payment_request_id: name of the Frappe Payment Request created by
	    ``webshop.controllers.payment_handler.create_payment_request``.
	  mode: ``Redirect`` | ``Lightbox`` | ``iFrame``.
	  line_items: optional override. If omitted, built from the quotation
	    referenced by the Payment Request.
	  billing_address: optional override. If omitted, built from the quotation.
	  success_url / failed_url: optional overrides for the Wallee return URLs.
	    Defaults derived from the Payment Request id so ``/wallee/success`` can
	    resolve the right Payment Intent.
	  provider: optional Wallee provider name; first enabled is used otherwise.
	"""
	import json as _json

	from payments.api.intent import create_intent

	# Accept JSON strings (Frappe HTTP layer may pass them as strings).
	if isinstance(line_items, str):
		line_items = _json.loads(line_items) if line_items else None
	if isinstance(billing_address, str):
		billing_address = _json.loads(billing_address) if billing_address else None

	mode = (mode or "Redirect").strip()
	if mode not in ("Redirect", "Lightbox", "iFrame"):
		frappe.throw(_("Invalid Wallee mode: {0}").format(mode))

	pr = frappe.get_doc("Payment Request", payment_request_id)
	if pr.payment_request_type != "Inward":
		frappe.throw(_("Payment Request {0} is not Inward").format(payment_request_id))

	# Auto-build line_items / billing_address from the Quotation when not provided.
	if not line_items and pr.reference_doctype == "Quotation":
		try:
			quotation = frappe.get_doc("Quotation", pr.reference_name)
			line_items = _build_line_items_from_quotation(quotation)
			if not billing_address:
				billing_address = _build_billing_address_from_quotation(quotation)
		except frappe.DoesNotExistError:
			pass

	provider_name = _resolve_wallee_provider_name(provider)

	# Default callback URLs include the PR id so ``/wallee/success`` can
	# resolve the Payment Intent by reference (legacy compat path).
	base_url = frappe.utils.get_url()
	if not success_url:
		success_url = f"{base_url}/wallee/success?payment_request={payment_request_id}"
	if not failed_url:
		failed_url = f"{base_url}/wallee/failed?payment_request={payment_request_id}"

	metadata = {
		"line_items": line_items,
		"billing_address": billing_address,
		"success_url": success_url,
		"failed_url": failed_url,
		"description": f"Webshop {payment_request_id}",
	}

	# Frappe stores Payment Request amount in major units; the Intent FSM
	# expects minor units (cents) like Stripe/Wallee SDKs.
	amount_minor = int(round((pr.grand_total or 0) * 100))

	intent = create_intent(
		provider=provider_name,
		channel="wallee_web",
		amount=amount_minor,
		currency=pr.currency,
		reference_doctype="Payment Request",
		reference_name=payment_request_id,
		metadata=metadata,
	)

	if intent.get("status") in ("failed", "canceled"):
		return {
			"status": "error",
			"message": intent.get("error_message") or _("Wallee transaction creation failed"),
			"payment_request_id": payment_request_id,
		}

	transaction_id = intent.get("provider_intent_id")
	next_action_payload = intent.get("next_action_payload") or {}
	payment_url = next_action_payload.get("url")  # Redirect mode URL from driver

	# Persist payment_url on the Payment Request for legacy callers that
	# read PR.payment_url (Frappe convention).
	try:
		pr.db_set("payment_url", payment_url, update_modified=False)
	except Exception:  # noqa: BLE001 — non-fatal, just cosmetic
		pass

	result: dict[str, Any] = {
		"status": "success",
		"payment_mode": mode,
		"payment_request_id": payment_request_id,
		"payment_intent": intent.get("intent_name"),
		"transaction_id": transaction_id,
	}

	if mode == "Redirect":
		result["payment_url"] = payment_url
	elif mode == "Lightbox":
		result["javascript_url"] = get_lightbox_javascript_url(transaction_id, provider=provider_name)
	elif mode == "iFrame":
		result["javascript_url"] = get_iframe_javascript_url(transaction_id, provider=provider_name)
		methods = get_payment_method_configurations(
			transaction_id, integration_mode="IFRAME", provider=provider_name
		)
		if methods:
			result["payment_method_id"] = methods[0]["id"]
			result["payment_methods"] = methods

	return result


@frappe.whitelist(allow_guest=True)
def get_lightbox_javascript_url(transaction_id: int | str, provider: str | None = None) -> str:
	"""Fetch the Wallee Lightbox JS URL for a created transaction."""
	from wallee import TransactionsService

	prov = _provider(_resolve_wallee_provider_name(provider))
	service = TransactionsService(prov.build_configuration())
	# SDK signature: (transaction_id, space_id)
	return str(service.get_payment_transactions_id_lightbox_javascript_url(int(transaction_id), prov.space_id))


@frappe.whitelist(allow_guest=True)
def get_iframe_javascript_url(transaction_id: int | str, provider: str | None = None) -> str:
	"""Fetch the Wallee iFrame JS URL for a created transaction."""
	from wallee import TransactionsService

	prov = _provider(_resolve_wallee_provider_name(provider))
	service = TransactionsService(prov.build_configuration())
	return str(service.get_payment_transactions_id_iframe_javascript_url(int(transaction_id), prov.space_id))


@frappe.whitelist(allow_guest=True)
def get_payment_method_configurations(
	transaction_id: int | str,
	integration_mode: str = "IFRAME",
	provider: str | None = None,
) -> list[dict[str, Any]]:
	"""Return available payment method configurations for a transaction.

	``integration_mode`` is one of ``IFRAME``, ``LIGHTBOX``, ``PAYMENT_PAGE``.
	Used by the iFrame flow to pick which payment method tab to display.
	"""
	from wallee import TransactionsService

	prov = _provider(_resolve_wallee_provider_name(provider))
	service = TransactionsService(prov.build_configuration())

	response = service.get_payment_transactions_id_payment_method_configurations(
		int(transaction_id), integration_mode, prov.space_id
	)

	# Response can be a list, a paginated object with .data, or .items.
	data_list = response
	if hasattr(response, "data") and response.data is not None:
		data_list = response.data
	elif hasattr(response, "items") and response.items is not None:
		data_list = response.items

	methods: list[dict[str, Any]] = []
	for m in data_list or []:
		methods.append(
			{
				"id": getattr(m, "id", None),
				"name": getattr(m, "name", None),
				"resolved_title": getattr(m, "resolved_title", None),
				"resolved_description": getattr(m, "resolved_description", None),
				"image_url": getattr(m, "resolved_image_url", None),
			}
		)
	return methods


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
