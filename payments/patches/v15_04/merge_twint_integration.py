# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""Patch v15.04 — merge ``twint_integration`` into ``payments`` (Phase 11).

Mirrors :mod:`payments.patches.v15_03.merge_wallee_integration`. The webshop
TWINT QR flow now lives entirely in the unified ``payments`` app:

- POS terminal (channel ``qr_bridge``) was already migrated in earlier phases.
- Webshop consumer (channel ``twint_web``, this patch) is now powered by the
  same neoservice bridge — no more local PHP service in ``twint_integration``.

Idempotent. Safe to run multiple times. Steps (in order):

1. Skip if ``twint_integration`` was never installed.
2. Ensure a ``Payment Channel`` record exists for ``twint_web``.
3. Ensure a ``Payment Provider`` exists (reuse if present, else create
   ``twint_migrated`` pointing at :class:`TwintPHPBridgeDriver` — both channels
   share the same provider).
4. Ensure a ``Provider Channel Settings`` binding exists for
   ``(provider, twint_web)`` with driver_class
   ``payments.drivers.twint.web_driver.TwintWebDriver``.
5. Make sure the existing ``Payment Gateway`` "Twint" points at
   ``Twint Bridge Settings`` (not the legacy ``Twint Settings`` singleton)
   so the generic ``webshop.controllers.payment_handler.create_payment_request``
   can find it. Create a ``Payment Gateway Account`` if missing.
6. Drop legacy tables: ``tabTwint Transaction`` (the audit lives in TWINT API
   + Webhook Event Log unified).
7. (Optional) Drop the legacy ``Twint Settings`` singleton record IF empty —
   we keep its DocType for now to avoid bricking the uninstall sequence,
   ``bench uninstall-app twint_integration`` will remove it cleanly.

After this patch, ``bench uninstall-app twint_integration`` is safe.
"""

from __future__ import annotations

import json

import frappe
from frappe.utils import now_datetime


LEGACY_TABLES_TO_DROP = [
	"tabTwint Transaction",
]


def execute() -> None:
	if "twint_integration" not in frappe.get_installed_apps():
		print("[v15_04] twint_integration not installed — skipping merger patch")
		return

	print("[v15_04] merging twint_integration → payments …")

	_ensure_payment_channel()
	provider_name = _ensure_payment_provider()
	_ensure_provider_channel_settings(provider_name)
	_ensure_payment_gateway()
	_drop_legacy_tables()

	frappe.db.commit()
	print("[v15_04] merger complete. You can now `bench uninstall-app twint_integration`.")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ensure_payment_channel() -> None:
	"""Register the twint_web Payment Channel for QR consumer flow."""
	if frappe.db.exists("Payment Channel", "twint_web"):
		print("[v15_04] Payment Channel twint_web already exists")
		return
	frappe.get_doc(
		{
			"doctype": "Payment Channel",
			"channel_code": "twint_web",
			"display_label": "TWINT Web (QR consumer)",
			"ui_kind": "qr_display",
			"capabilities_json": json.dumps(
				{
					"supports_refund": True,
					"supports_partial_refund": True,
					"async": True,
					"requires_qr_scan": True,
					"requires_device": False,
				}
			),
		}
	).insert(ignore_permissions=True)
	print("[v15_04] created Payment Channel twint_web")


def _ensure_payment_provider() -> str:
	"""Reuse existing TWINT provider or create ``twint_migrated``.

	Both POS terminal (qr_bridge) and webshop (twint_web) share the same
	provider — they only differ in channel binding.
	"""
	existing = frappe.db.get_value(
		"Payment Provider",
		{"driver_class": ["like", "payments.drivers.twint.%"], "enabled": 1},
		"name",
		order_by="modified desc",
	)
	if existing:
		print(f"[v15_04] reusing Payment Provider {existing}")
		return existing
	provider_doc = frappe.get_doc(
		{
			"doctype": "Payment Provider",
			"provider_name": "twint_migrated",
			"display_label": "TWINT",
			"driver_class": "payments.drivers.twint.php_bridge_driver.TwintPHPBridgeDriver",
			"enabled": 1,
			"mode": "live",
		}
	).insert(ignore_permissions=True)
	print(f"[v15_04] created Payment Provider {provider_doc.name}")
	return provider_doc.name


def _ensure_provider_channel_settings(provider_name: str) -> None:
	"""Bind the provider to the twint_web channel with the new web driver class."""
	binding_name = frappe.db.get_value(
		"Provider Channel Settings",
		{"provider": provider_name, "channel": "twint_web"},
		"name",
	)
	if binding_name:
		# Make sure the driver_class is the right one (legacy installs may
		# have pointed at the POS bridge driver by accident).
		current = frappe.db.get_value("Provider Channel Settings", binding_name, "driver_class")
		if current != "payments.drivers.twint.web_driver.TwintWebDriver":
			frappe.db.set_value(
				"Provider Channel Settings",
				binding_name,
				"driver_class",
				"payments.drivers.twint.web_driver.TwintWebDriver",
				update_modified=False,
			)
			print(f"[v15_04] updated driver_class on {binding_name}")
		else:
			print(f"[v15_04] Provider Channel Settings {binding_name} already correct")
		return
	frappe.get_doc(
		{
			"doctype": "Provider Channel Settings",
			"provider": provider_name,
			"channel": "twint_web",
			"driver_class": "payments.drivers.twint.web_driver.TwintWebDriver",
			"enabled": 1,
		}
	).insert(ignore_permissions=True)
	print(f"[v15_04] created Provider Channel Settings {provider_name}-twint_web")


def _ensure_payment_gateway() -> None:
	"""Point the ``Twint`` Payment Gateway at ``Twint Bridge Settings``.

	The legacy ``twint_integration`` shipped a Payment Gateway "Twint" that
	pointed at the now-redundant ``Twint Settings`` singleton. The generic
	``payment_handler.create_payment_request(gateway_settings="Twint Bridge Settings")``
	needs to find a Payment Gateway with ``gateway_settings`` set to that
	DocType — so we re-point the existing record (or create one).
	"""
	if not frappe.db.exists("Payment Gateway", "Twint"):
		# No existing record — only create if there is at least one
		# Twint Bridge Settings (else nothing to wire to).
		first_tbs = frappe.db.get_value("Twint Bridge Settings", {}, "name", order_by="modified desc")
		if not first_tbs:
			print("[v15_04] no Twint Bridge Settings — skipping Payment Gateway creation")
			return
		frappe.get_doc(
			{
				"doctype": "Payment Gateway",
				"gateway": "Twint",
				"gateway_settings": "Twint Bridge Settings",
				"gateway_controller": first_tbs,
			}
		).insert(ignore_permissions=True)
		print(f"[v15_04] created Payment Gateway Twint pointing at {first_tbs}")
	else:
		pg = frappe.get_doc("Payment Gateway", "Twint")
		current_settings = pg.gateway_settings
		if current_settings != "Twint Bridge Settings":
			pg.gateway_settings = "Twint Bridge Settings"
			# Pick the first Twint Bridge Settings record as the controller link.
			first_tbs = frappe.db.get_value("Twint Bridge Settings", {}, "name", order_by="modified desc")
			if first_tbs:
				pg.gateway_controller = first_tbs
			pg.save(ignore_permissions=True)
			print(f"[v15_04] updated Payment Gateway Twint: {current_settings} → Twint Bridge Settings")
		else:
			print("[v15_04] Payment Gateway Twint already points at Twint Bridge Settings")

	# Ensure a Payment Gateway Account exists so the webshop checkout can use it.
	if not frappe.db.exists("Payment Gateway Account", {"payment_gateway": "Twint"}):
		company = frappe.db.get_value("Company", {}, "name")
		payment_account = frappe.db.get_value(
			"Account", {"company": company, "account_type": "Bank"}, "name"
		)
		if not (company and payment_account):
			print("[v15_04] cannot create Payment Gateway Account — missing Company/Bank account")
			return
		frappe.get_doc(
			{
				"doctype": "Payment Gateway Account",
				"payment_gateway": "Twint",
				"currency": "CHF",
				"is_default": 0,
				"payment_account": payment_account,
			}
		).insert(ignore_permissions=True)
		print("[v15_04] created Payment Gateway Account for Twint")


def _drop_legacy_tables() -> None:
	"""Drop the per-payment Twint Transaction history table.

	The Payment Intent FSM is the new source of truth for state. Audit data
	is in the TWINT merchant dashboard via api.twint.com.
	"""
	for table in LEGACY_TABLES_TO_DROP:
		exists = frappe.db.sql(
			"""SELECT 1 FROM information_schema.tables WHERE table_schema = DATABASE() AND table_name = %s""",
			(table,),
		)
		if exists:
			frappe.db.sql(f"DROP TABLE `{table}`")
			print(f"[v15_04] dropped {table}")
		else:
			print(f"[v15_04] {table} already absent")
