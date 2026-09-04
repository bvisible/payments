#//// Neoffice — added file (no upstream equivalent). Provisions the `wallee_web`
#//// Payment Channel and its (provider, wallee_web) binding on every Wallee provider,
#//// test and live. The v15_03 merger created only the `terminal` binding, so the web
#//// binding had been set up by hand on the early instances; without it
#//// `create_intent(channel="wallee_web")` raises DriverResolutionError and both the
#//// webshop and the POS guest Wallee checkout are dead on a fresh instance. Idempotent.
#//// Commits: f2543ee 2026-06-01 "feat(wallee-web): patch to provision the wallee_web channel binding on the fleet"
# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""Patch v15.05 — provision the Wallee web (hosted checkout) channel binding.

The ``wallee_web`` channel (:class:`WalleeWebDriver`, Wallee hosted payment
page) powers BOTH the webshop Wallee checkout and the POS guest / takeaway
Wallee checkout. The v15.03 merger
(:mod:`payments.patches.v15_03.merge_wallee_integration`) created only the
``terminal`` binding; the ``wallee_web`` channel + binding were set up manually
on the early instances.

Without this binding, ``payments.api.intent.create_intent(provider,
channel="wallee_web", …)`` raises ``DriverResolutionError`` (the registry needs
a ``Provider Channel Settings`` row for the (provider, channel) couple) — so a
fresh instance would have webshop AND POS-guest Wallee checkout broken. This
patch makes the binding part of the migration.

Idempotent. Safe to run multiple times. Steps:

1. Skip if there is no Wallee ``Payment Provider`` at all.
2. Ensure a ``Payment Channel`` record exists for ``wallee_web``.
3. For EVERY Wallee ``Payment Provider`` (``driver_class`` LIKE
   ``payments.drivers.wallee.%``) — covers test + live — ensure a
   ``Provider Channel Settings`` binding ``(provider, wallee_web)`` whose
   ``driver_class`` is the web driver. A provider whose own ``driver_class``
   points at the terminal driver still serves ``wallee_web`` because the
   binding's ``driver_class`` overrides it at resolution time.
"""

from __future__ import annotations

import json

import frappe

_WEB_DRIVER = "payments.drivers.wallee.web_driver.WalleeWebDriver"
_WALLEE_DRIVER_PREFIX = "payments.drivers.wallee.%"


def execute() -> None:
	providers = frappe.get_all(
		"Payment Provider",
		filters={"driver_class": ["like", _WALLEE_DRIVER_PREFIX]},
		pluck="name",
	)
	if not providers:
		print("[v15_05] no Wallee Payment Provider — skipping wallee_web provisioning")
		return

	print("[v15_05] provisioning wallee_web channel + bindings …")
	_ensure_payment_channel()
	for provider_name in providers:
		_ensure_binding(provider_name)

	frappe.db.commit()
	print("[v15_05] wallee_web provisioning complete")


def _ensure_payment_channel() -> None:
	"""Register the ``wallee_web`` Payment Channel (hosted redirect checkout)."""
	if frappe.db.exists("Payment Channel", "wallee_web"):
		print("[v15_05] Payment Channel wallee_web already exists")
		return
	frappe.get_doc(
		{
			"doctype": "Payment Channel",
			"channel_code": "wallee_web",
			"display_label": "Wallee Web (hosted checkout)",
			"ui_kind": "redirect",
			"capabilities_json": json.dumps(
				{
					"supports_refund": True,
					"supports_partial_refund": True,
					"async": True,
					"requires_redirect": True,
					"requires_device": False,
				}
			),
		}
	).insert(ignore_permissions=True)
	print("[v15_05] created Payment Channel wallee_web")


def _ensure_binding(provider_name: str) -> None:
	"""Bind ``provider_name`` to the ``wallee_web`` channel via the web driver."""
	binding_name = frappe.db.get_value(
		"Provider Channel Settings",
		{"provider": provider_name, "channel": "wallee_web"},
		"name",
	)
	if binding_name:
		# Legacy / manual installs may have a binding with the wrong driver
		# class — make sure it points at the web driver and is enabled.
		current = frappe.db.get_value(
			"Provider Channel Settings", binding_name, "driver_class"
		)
		if current != _WEB_DRIVER:
			frappe.db.set_value(
				"Provider Channel Settings",
				binding_name,
				"driver_class",
				_WEB_DRIVER,
				update_modified=False,
			)
			print(f"[v15_05] fixed driver_class on {binding_name}")
		else:
			print(f"[v15_05] Provider Channel Settings {binding_name} already correct")
		return

	frappe.get_doc(
		{
			"doctype": "Provider Channel Settings",
			"provider": provider_name,
			"channel": "wallee_web",
			"driver_class": _WEB_DRIVER,
			"enabled": 1,
		}
	).insert(ignore_permissions=True)
	print(f"[v15_05] created Provider Channel Settings {provider_name}-wallee_web")
