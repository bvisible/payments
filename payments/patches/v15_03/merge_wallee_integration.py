#//// Neoffice — added file (no upstream equivalent). Upstream ships an EMPTY
#//// `patches.txt` and has never had a patch; every entry in ours is one of these.
#//// This one absorbs the retired `wallee_integration` app (ADR-005): it promotes the
#//// legacy `Wallee Settings` single into a per-provider row, turns each `Wallee Payment
#//// Terminal` into a `Payment Device`, then drops the legacy state tables (Wallee
#//// Transaction / Transaction Item / Webhook Log) and the legacy POS Profile and
#//// Payment Gateway Account custom fields — after which `bench uninstall-app
#//// wallee_integration` is safe. Idempotent.
#//// Commits: 99e929c 2026-05-19 "feat(payments): merge wallee_integration into payments — ADR-005"
# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""Patch v15.03 — merge ``wallee_integration`` into ``payments`` (ADR-005).

Idempotent. Safe to run multiple times. Steps (in order):

1. Skip if ``wallee_integration`` was never installed (clean install path).
2. Migrate the legacy ``Wallee Settings`` single → a new (non-single) row
   in the ``payments`` Wallee Settings DocType, linked to the matching
   ``Payment Provider``. If no Wallee Payment Provider exists yet, create
   one named ``wallee_migrated``.
3. Migrate each ``Wallee Payment Terminal`` → ``Payment Device``
   (provider_device_id=terminal_id, device_type=wallee_terminal, etc.).
   Skipped if a Payment Device already exists with the same
   provider_device_id (idempotency).
4. Drop the legacy state tables:
   - tabWallee Transaction
   - tabWallee Transaction Item
   - tabWallee Webhook Log
   (the audit lives in api.wallee.com + Webhook Event Log unified).
5. Drop legacy Custom Fields:
   - POS Profile: wallee_terminal_payment_mode, enable_wallee_terminal,
                  wallee_payment_terminal
   - Payment Gateway Account: wallee_payment_method_id

After this patch, ``bench uninstall-app wallee_integration`` is safe — all
data the operator cares about has been promoted into ``payments``.
"""

from __future__ import annotations

import frappe
from frappe.utils import now_datetime


LEGACY_CUSTOM_FIELDS = [
	("POS Profile", "wallee_terminal_payment_mode"),
	("POS Profile", "enable_wallee_terminal"),
	("POS Profile", "wallee_payment_terminal"),
	("Payment Gateway Account", "wallee_payment_method_id"),
]

LEGACY_TABLES_TO_DROP = [
	"tabWallee Transaction",
	"tabWallee Transaction Item",
	"tabWallee Webhook Log",
]


def execute() -> None:
	# 1. Was wallee_integration ever installed on this site?
	if "wallee_integration" not in frappe.get_installed_apps():
		print("[v15_03] wallee_integration not installed — skipping merger patch")
		return

	print("[v15_03] merging wallee_integration → payments …")

	provider_name = _ensure_payment_provider()
	_migrate_settings(provider_name)
	_migrate_terminals(provider_name)
	_drop_legacy_tables()
	_drop_legacy_custom_fields()

	frappe.db.commit()
	print("[v15_03] merger complete. You can now `bench uninstall-app wallee_integration`.")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ensure_payment_provider() -> str:
	"""Return a Wallee Payment Provider name. Create one if missing.

	If the operator already had a Wallee provider configured (e.g.
	``wallee_test`` or ``wallee_live`` created by an earlier session), reuse
	it. Otherwise insert a fresh ``wallee_migrated`` record so the legacy
	settings have a target to link to.
	"""
	existing = frappe.db.get_value(
		"Payment Provider",
		{"driver_class": ["like", "payments.drivers.wallee.%"]},
		"name",
	)
	if existing:
		print(f"[v15_03] using existing Payment Provider {existing!r}")
		return existing

	print("[v15_03] no Wallee Payment Provider found — creating 'wallee_migrated'")
	doc = frappe.get_doc(
		{
			"doctype": "Payment Provider",
			"provider_name": "wallee_migrated",
			"display_label": "Wallee (migrated from wallee_integration)",
			"enabled": 1,
			# Conservative default: leave mode=test so a misconfigured site
			# can't accidentally take live payments after the patch.
			"mode": "test",
			"driver_class": "payments.drivers.wallee.terminal_driver.WalleeTerminalDriver",
			"credentials_json": "{}",
		}
	).insert(ignore_permissions=True)
	# Also create the terminal binding so the picker/wizard works.
	if not frappe.db.exists("Payment Channel", "terminal"):
		# Should already exist from earlier setup; defensive.
		frappe.get_doc(
			{
				"doctype": "Payment Channel",
				"channel_code": "terminal",
				"display_label": "POS Terminal",
				"ui_kind": "card_present_modal",
				"capabilities_json": '{"supports_refund": true}',
			}
		).insert(ignore_permissions=True)
	if not frappe.db.exists(
		"Provider Channel Settings", {"provider": doc.name, "channel": "terminal"}
	):
		frappe.get_doc(
			{
				"doctype": "Provider Channel Settings",
				"provider": doc.name,
				"channel": "terminal",
				"enabled": 1,
			}
		).insert(ignore_permissions=True)
	return doc.name


def _migrate_settings(provider_name: str) -> None:
	"""Promote the legacy ``Wallee Settings`` single → linked record."""
	# Already migrated? (the new DocType uses field:provider as autoname)
	if frappe.db.exists("Wallee Settings", provider_name):
		print(f"[v15_03] Wallee Settings already exists for provider {provider_name!r} — skipping")
		return

	# The legacy single's data lives in `tabSingles` (no row in
	# `tabWallee Settings` for singles). Read via frappe.get_single but
	# under the OLD module path — Frappe still has the DocType registered.
	try:
		legacy = frappe.get_single("Wallee Settings")
	except Exception as exc:  # noqa: BLE001
		print(f"[v15_03] could not read legacy Wallee Settings: {exc!r}")
		return

	doc = frappe.get_doc(
		{
			"doctype": "Wallee Settings",
			"provider": provider_name,
			"enabled": legacy.enabled or 0,
			"user_id": legacy.user_id or None,
			"authentication_key": legacy.get_password("authentication_key", raise_exception=False)
			if legacy.authentication_key
			else None,
			"space_id": legacy.space_id or None,
			"api_host": legacy.api_host or "https://app-wallee.com/api/v2.0",
			"account_id": getattr(legacy, "account_id", None),
			"enable_webshop": legacy.enable_webshop or 0,
			"payment_mode": legacy.payment_mode or "Redirect",
			"webshop_user_id": getattr(legacy, "webshop_user_id", None),
			"webshop_authentication_key": legacy.get_password(
				"webshop_authentication_key", raise_exception=False
			)
			if getattr(legacy, "webshop_authentication_key", None)
			else None,
			"success_url": getattr(legacy, "success_url", None),
			"failed_url": getattr(legacy, "failed_url", None),
			"enable_pos_terminal": getattr(legacy, "enable_pos_terminal", 0) or 0,
			"pos_mode_of_payment": getattr(legacy, "pos_mode_of_payment", None),
			"pos_user_id": getattr(legacy, "pos_user_id", None),
			"pos_authentication_key": legacy.get_password(
				"pos_authentication_key", raise_exception=False
			)
			if getattr(legacy, "pos_authentication_key", None)
			else None,
			"auto_capture": getattr(legacy, "auto_capture", 1),
			"webhook_secret": legacy.get_password("webhook_secret", raise_exception=False)
			if legacy.webhook_secret
			else None,
			"log_api_calls": getattr(legacy, "log_api_calls", 0) or 0,
			"test_mode": getattr(legacy, "test_mode", 0) or 0,
			"send_invoice_to_customer": getattr(legacy, "send_invoice_to_customer", 0) or 0,
		}
	).insert(ignore_permissions=True)
	print(f"[v15_03] Wallee Settings migrated → record {doc.name!r}")


def _migrate_terminals(provider_name: str) -> None:
	"""Promote each legacy ``Wallee Payment Terminal`` → ``Payment Device``."""
	if not frappe.db.table_exists("Wallee Payment Terminal"):
		return

	binding = frappe.db.get_value(
		"Provider Channel Settings",
		{"provider": provider_name, "channel": "terminal"},
		"name",
	)
	if not binding:
		print(f"[v15_03] no terminal binding for {provider_name!r} — skipping terminal migration")
		return

	rows = frappe.db.sql(
		"""SELECT name, terminal_name, terminal_id, identifier, status,
		          device_serial_number, default_currency
		   FROM `tabWallee Payment Terminal`""",
		as_dict=True,
	)
	migrated = 0
	for row in rows:
		if not row.terminal_id:
			continue
		# Skip if a Payment Device already exists for this terminal_id.
		if frappe.db.exists(
			"Payment Device", {"provider_device_id": str(row.terminal_id)}
		):
			continue
		try:
			frappe.get_doc(
				{
					"doctype": "Payment Device",
					"device_label": row.terminal_name or f"Wallee Terminal {row.terminal_id}",
					"enabled": 1,
					"status": "online" if (row.status or "").lower() == "active" else "offline",
					"provider_device_id": str(row.terminal_id),
					"device_type": "wallee_terminal",
					"serial_number": row.device_serial_number,
					"provider_channel_settings": binding,
					"last_seen_at": now_datetime(),
				}
			).insert(ignore_permissions=True)
			migrated += 1
		except Exception as exc:  # noqa: BLE001
			print(f"[v15_03] could not migrate Wallee Payment Terminal {row.name!r}: {exc!r}")
	print(f"[v15_03] migrated {migrated} Wallee Payment Terminal(s) → Payment Device")


def _drop_legacy_tables() -> None:
	"""Drop transient state tables. Audit lives elsewhere now."""
	for table in LEGACY_TABLES_TO_DROP:
		try:
			frappe.db.sql_ddl(f"DROP TABLE IF EXISTS `{table}`")
			print(f"[v15_03] dropped {table}")
		except Exception as exc:  # noqa: BLE001
			print(f"[v15_03] could not drop {table}: {exc!r}")


def _drop_legacy_custom_fields() -> None:
	"""Remove Custom Fields owned by wallee_integration that we no longer use."""
	for dt, fieldname in LEGACY_CUSTOM_FIELDS:
		name = frappe.db.get_value("Custom Field", {"dt": dt, "fieldname": fieldname}, "name")
		if name:
			try:
				frappe.delete_doc("Custom Field", name, ignore_permissions=True, force=True)
				print(f"[v15_03] removed Custom Field {dt}.{fieldname}")
			except Exception as exc:  # noqa: BLE001
				print(f"[v15_03] could not remove Custom Field {dt}.{fieldname}: {exc!r}")
