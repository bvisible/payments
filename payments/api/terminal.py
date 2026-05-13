# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""Stripe Terminal management API.

Whitelisted endpoints to provision the physical-terminal side of a Stripe
account: create Locations, register Readers, refresh their online status, and
push the standalone "card-present" attach when needed. Mirrors the doctrine of
the compass artifact §1 (server-driven mode) and follows the same Provider /
Channel / Driver resolution as :mod:`payments.api.intent`.

All endpoints accept a ``provider`` argument (default: the first enabled Stripe
``Payment Provider``) so multi-account installs can be addressed explicitly.
"""

from __future__ import annotations

import json
from typing import Any

import frappe
from frappe import _
from frappe.utils import now_datetime

STRIPE_PROVIDER_DEFAULT = "stripe"
TERMINAL_CHANNEL = "terminal"


def _resolve_stripe_provider(provider_name: str | None = None):  # noqa: ANN001
	"""Return the active Stripe StripeProvider instance for the given record."""
	from payments.drivers.stripe.provider import StripeProvider

	name = provider_name or STRIPE_PROVIDER_DEFAULT
	if not frappe.db.exists("Payment Provider", name):
		frappe.throw(_("Payment Provider {0} does not exist").format(name))
	provider_doc = frappe.get_doc("Payment Provider", name)
	if not provider_doc.enabled:
		frappe.throw(_("Payment Provider {0} is disabled").format(name))
	return StripeProvider(provider_doc), provider_doc


def _terminal_binding(provider_name: str) -> str:
	"""Return the Provider Channel Settings name for (provider, terminal). Throws if missing."""
	binding = frappe.db.get_value(
		"Provider Channel Settings",
		{"provider": provider_name, "channel": TERMINAL_CHANNEL},
		"name",
	)
	if not binding:
		frappe.throw(
			_("No Provider Channel Settings found for {0} × {1}. Create one first.").format(
				provider_name, TERMINAL_CHANNEL
			)
		)
	return binding


# ----------------------------------------------------------------------------
# Locations
# ----------------------------------------------------------------------------


@frappe.whitelist()
def create_stripe_location(
	display_name: str,
	country: str,
	line1: str,
	city: str,
	postal_code: str,
	line2: str | None = None,
	state: str | None = None,
	provider: str | None = None,
) -> dict[str, Any]:
	"""Create a Stripe Terminal Location and return the ``tml_xxx`` id.

	The country is immutable on Stripe's side once created.
	"""
	import stripe

	stripe_provider, _provider_doc = _resolve_stripe_provider(provider)
	address: dict[str, Any] = {
		"line1": line1,
		"city": city,
		"postal_code": postal_code,
		"country": country.upper(),
	}
	if line2:
		address["line2"] = line2
	if state:
		address["state"] = state

	location = stripe.terminal.Location.create(
		api_key=stripe_provider.secret_key,
		display_name=display_name,
		address=address,
		idempotency_key=f"loc_{display_name}_{country.upper()}",
	)
	return {
		"stripe_location_id": location.id,
		"display_name": location.display_name,
		"address": dict(location.address) if hasattr(location.address, "items") else location.address,
	}


@frappe.whitelist()
def list_stripe_locations(provider: str | None = None) -> list[dict[str, Any]]:
	"""Paginated list of Stripe Terminal Locations, normalized to dict."""
	import stripe

	stripe_provider, _provider_doc = _resolve_stripe_provider(provider)
	resp = stripe.terminal.Location.list(api_key=stripe_provider.secret_key, limit=100)
	return [
		{
			"stripe_location_id": loc.id,
			"display_name": loc.display_name,
			"address": dict(loc.address) if hasattr(loc.address, "items") else loc.address,
		}
		for loc in resp.data
	]


# ----------------------------------------------------------------------------
# Readers
# ----------------------------------------------------------------------------


@frappe.whitelist()
def register_stripe_reader(
	registration_code: str,
	location: str,
	label: str | None = None,
	device_label: str | None = None,
	provider: str | None = None,
) -> dict[str, Any]:
	"""Register a Stripe Reader against a Location and persist a Payment Device.

	On the physical reader: swipe from the left edge → Settings → admin code
	**``07139``** → *Generate pairing code* (three dash-separated words, e.g.
	``cool-cyan-fox``). Use that string as ``registration_code``.

	For sandbox/simulator, pass ``simulated-wpe`` / ``simulated-s700`` /
	``simulated-s710``.
	"""
	import stripe

	stripe_provider, provider_doc = _resolve_stripe_provider(provider)
	provider_name = provider_doc.name
	binding_name = _terminal_binding(provider_name)

	reader = stripe.terminal.Reader.create(
		api_key=stripe_provider.secret_key,
		registration_code=registration_code,
		location=location,
		label=label or registration_code,
		idempotency_key=f"reader_{registration_code}_{location}",
	)

	# Persist a Payment Device record. Use Stripe's reader id as a unique key.
	device_doc = frappe.get_doc(
		{
			"doctype": "Payment Device",
			"device_label": device_label or label or reader.label or registration_code,
			"provider_device_id": reader.id,
			"device_type": getattr(reader, "device_type", None),
			"serial_number": getattr(reader, "serial_number", None),
			"provider_channel_settings": binding_name,
			"location_ref": location,
			"status": "online" if (getattr(reader, "status", None) == "online") else "offline",
			"device_sw_version": getattr(reader, "device_sw_version", None),
			"last_seen_at": now_datetime(),
		}
	).insert(ignore_permissions=True)

	return {
		"payment_device": device_doc.name,
		"stripe_reader_id": reader.id,
		"device_type": getattr(reader, "device_type", None),
		"location": location,
		"status": getattr(reader, "status", None),
	}


@frappe.whitelist()
def list_stripe_readers(
	location: str | None = None, status: str | None = None, provider: str | None = None
) -> list[dict[str, Any]]:
	"""Paginated list of Readers attached to the account, normalized to dict."""
	import stripe

	stripe_provider, _provider_doc = _resolve_stripe_provider(provider)
	kwargs: dict[str, Any] = {"api_key": stripe_provider.secret_key, "limit": 100}
	if location:
		kwargs["location"] = location
	if status:
		kwargs["status"] = status
	resp = stripe.terminal.Reader.list(**kwargs)
	return [
		{
			"stripe_reader_id": r.id,
			"label": r.label,
			"device_type": getattr(r, "device_type", None),
			"serial_number": getattr(r, "serial_number", None),
			"location": r.location,
			"status": getattr(r, "status", None),
			"device_sw_version": getattr(r, "device_sw_version", None),
			"last_seen_at": getattr(r, "last_seen_at", None),
		}
		for r in resp.data
	]


def sync_stripe_readers_status() -> dict[str, Any]:
	"""Scheduler entrypoint: refresh ``status``/``last_seen_at`` for every Payment Device.

	Wired in ``hooks.py`` to run every 5 minutes. Compass §1 notes that
	``Reader.status`` flips to ``offline`` if Stripe hasn't seen the device for
	~2 minutes, and that ``last_seen_at`` is in milliseconds.
	"""
	import stripe
	from datetime import datetime, timezone

	stats = {"providers": 0, "devices_checked": 0, "devices_updated": 0, "errors": 0}

	for provider_name in frappe.get_all(
		"Payment Provider",
		filters={"enabled": 1, "provider_name": STRIPE_PROVIDER_DEFAULT},
		pluck="name",
	):
		stats["providers"] += 1
		try:
			stripe_provider, _provider_doc = _resolve_stripe_provider(provider_name)
		except Exception:  # noqa: BLE001 — keep scheduler running
			stats["errors"] += 1
			continue

		# Find devices bound to this provider via the binding table.
		bindings = frappe.get_all(
			"Provider Channel Settings",
			filters={"provider": provider_name, "channel": TERMINAL_CHANNEL},
			pluck="name",
		)
		if not bindings:
			continue
		devices = frappe.get_all(
			"Payment Device",
			filters={"provider_channel_settings": ["in", bindings], "enabled": 1},
			fields=["name", "provider_device_id", "status"],
		)
		stats["devices_checked"] += len(devices)
		for dev in devices:
			if not dev["provider_device_id"]:
				continue
			try:
				reader = stripe.terminal.Reader.retrieve(
					dev["provider_device_id"], api_key=stripe_provider.secret_key
				)
			except Exception as exc:  # noqa: BLE001
				stats["errors"] += 1
				frappe.log_error(
					"sync_stripe_readers_status retrieve failed",
					f"device={dev['name']} provider_id={dev['provider_device_id']}: {exc!r}",
				)
				continue

			new_status = (getattr(reader, "status", None) or "unknown")
			# Stripe returns last_seen_at in ms.
			last_seen_at = None
			ms = getattr(reader, "last_seen_at", None)
			if ms:
				try:
					last_seen_at = datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).replace(
						tzinfo=None
					)
				except (TypeError, ValueError):
					last_seen_at = None

			# Only write if something changed (avoid touch storms).
			updates: dict[str, Any] = {}
			if new_status != dev["status"]:
				updates["status"] = new_status
			if last_seen_at:
				updates["last_seen_at"] = last_seen_at
			sw = getattr(reader, "device_sw_version", None)
			if sw:
				updates["device_sw_version"] = sw
			if updates:
				frappe.db.set_value("Payment Device", dev["name"], updates, update_modified=False)
				stats["devices_updated"] += 1

	frappe.db.commit()
	return stats


# ----------------------------------------------------------------------------
# Attach / cancel from POS layer
# ----------------------------------------------------------------------------


@frappe.whitelist()
def attach_intent_to_reader(intent_name: str, device: str) -> dict[str, Any]:
	"""Push an already-created Payment Intent to a reader.

	Resolves the Stripe driver from the Payment Intent's provider/channel and
	calls ``confirm_intent(reader_id=...)``. The Frappe ``Payment Device`` record
	is used to find the Stripe reader id.
	"""
	from payments.api import intent as intent_api
	from payments.drivers.registry import resolve_driver

	intent_doc = frappe.get_doc("Payment Intent", intent_name)
	if intent_doc.status not in {"requires_action", "processing"}:
		frappe.throw(
			_("Payment Intent {0} is in status {1}; cannot attach to reader").format(
				intent_name, intent_doc.status
			)
		)
	if not intent_doc.provider_intent_id:
		frappe.throw(_("Payment Intent {0} has no provider_intent_id yet").format(intent_name))

	reader_id = frappe.db.get_value("Payment Device", device, "provider_device_id")
	if not reader_id:
		frappe.throw(_("Payment Device {0} has no provider_device_id").format(device))

	driver = resolve_driver(intent_doc.provider, intent_doc.channel)
	response = driver.confirm_intent(intent_doc.provider_intent_id, reader_id=reader_id)

	# Update the intent: stamp the device + apply FSM transition.
	intent_doc.device = device
	if response.next_action_payload:
		intent_doc.next_action_payload = json.dumps(response.next_action_payload)
	if response.next_action_type:
		intent_doc.next_action_type = response.next_action_type
	intent_doc.save(ignore_permissions=True)
	intent_doc.transition_to(
		response.status,
		event_source="api",
		error_code=response.error_code,
		error_message=response.error_message,
		ignore_invalid=True,
	)
	intent_doc.reload()
	return intent_api._serialize_intent_for_client(intent_doc)
