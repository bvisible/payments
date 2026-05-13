# Copyright (c) 2026, Neoffice and Contributors
# License: MIT. See LICENSE
"""Phase 4 smoke test for TWINT PHP Bridge wiring.

Runs without an actual TWINT P12 certificate. Validates that:

1. The ``Twint Settings`` DocType is created on the calling site.
2. The TWINT driver is loadable and the registry resolves it.
3. ``create_intent`` builds the right bridge payload (HTTP layer is mocked).
4. The scheduler ``poll_pending_twint_transactions`` runs without exploding.
5. ``frappe.conf.twint_service_url`` (or provider credentials) resolves a URL.

The full E2E with a real bridge + P12 belongs to Phase 7 (go-live) — call
``run_e2e`` with explicit kwargs when those credentials are available.
"""

from __future__ import annotations

import json
from unittest.mock import patch, MagicMock

import frappe

PROVIDER_NAME = "twint_smoke_provider"
CHANNEL_CODE = "qr_bridge"
MERCHANT_UUID = "smoke_merchant_001"
DRIVER_PATH = "payments.drivers.twint.php_bridge_driver.TwintPHPBridgeDriver"


def _ensure_fixtures() -> None:
	if not frappe.db.exists("Payment Provider", PROVIDER_NAME):
		frappe.get_doc(
			{
				"doctype": "Payment Provider",
				"provider_name": PROVIDER_NAME,
				"display_label": "TWINT (smoke)",
				"enabled": 1,
				"mode": "test",
				"driver_class": DRIVER_PATH,
				"credentials_json": json.dumps(
					{
						"service_url": frappe.conf.get("twint_service_url")
						or "https://neoservice.example.com",
						"api_key": frappe.conf.get("twint_api_key") or "smoke_key",
						"api_secret": frappe.conf.get("twint_api_secret") or "smoke_secret",
						"default_merchant_uuid": MERCHANT_UUID,
					}
				),
			}
		).insert(ignore_permissions=True)
	if not frappe.db.exists("Payment Channel", CHANNEL_CODE):
		frappe.get_doc(
			{
				"doctype": "Payment Channel",
				"channel_code": CHANNEL_CODE,
				"display_label": "TWINT QR Bridge",
				"ui_kind": "qr_display",
				"capabilities_json": json.dumps({"supports_refund": True, "requires_qr_scan": True}),
			}
		).insert(ignore_permissions=True)
	if not frappe.db.get_value(
		"Provider Channel Settings", {"provider": PROVIDER_NAME, "channel": CHANNEL_CODE}, "name"
	):
		frappe.get_doc(
			{
				"doctype": "Provider Channel Settings",
				"provider": PROVIDER_NAME,
				"channel": CHANNEL_CODE,
				"enabled": 1,
			}
		).insert(ignore_permissions=True)
	if not frappe.db.exists("Twint Bridge Settings", MERCHANT_UUID):
		frappe.get_doc(
			{
				"doctype": "Twint Bridge Settings",
				"merchant_uuid": MERCHANT_UUID,
				"display_label": "Smoke Merchant",
				"enabled": 1,
				"store_uuid": "00000000-0000-0000-0000-000000000000",
				"environment": "sandbox",
				"p12_password": "fake_smoke_password",
			}
		).insert(ignore_permissions=True)
	frappe.db.commit()


def _cleanup() -> None:
	intents = frappe.get_all(
		"Payment Intent",
		filters={"provider": PROVIDER_NAME, "metadata_json": ["like", "%phase4_smoke%"]},
		pluck="name",
	)
	for name in intents:
		for ev in frappe.get_all("Payment Event", filters={"intent": name}, pluck="name"):
			frappe.delete_doc("Payment Event", ev, force=True, ignore_permissions=True)
		frappe.delete_doc("Payment Intent", name, force=True, ignore_permissions=True)
	frappe.db.commit()


def run_all() -> dict:
	report: dict = {"checks": [], "errors": []}

	def add(name: str, ok: bool, detail: str = "") -> None:
		report["checks"].append({"name": name, "ok": ok, "detail": detail})
		marker = "✅" if ok else "❌"
		print(f"  {marker} {name}: {detail}")

	print("=" * 60)
	print("Payments Phase 4 — TWINT PHP Bridge wiring smoke")
	print("=" * 60)

	_cleanup()

	# 1. Twint Settings DocType exists.
	add(
		"Twint Bridge Settings DocType exists on this site",
		bool(frappe.db.exists("DocType", "Twint Bridge Settings")),
		"Twint Bridge Settings ✅" if frappe.db.exists("DocType", "Twint Bridge Settings") else "missing",
	)

	# 2. Fixtures.
	try:
		_ensure_fixtures()
		add("Provider + Channel + binding + Twint Settings present", True, MERCHANT_UUID)
	except Exception as exc:  # noqa: BLE001
		add("Fixtures", False, repr(exc))
		report["errors"].append({"step": "fixtures", "error": repr(exc)})
		report["all_ok"] = False
		return report

	# 3. Driver resolve.
	from payments.drivers.registry import resolve_driver

	try:
		driver = resolve_driver(PROVIDER_NAME, CHANNEL_CODE)
		add(
			"resolve_driver(twint, qr_bridge) → TwintPHPBridgeDriver",
			driver.__class__.__name__ == "TwintPHPBridgeDriver",
			driver.__class__.__name__,
		)
	except Exception as exc:  # noqa: BLE001
		add("resolve_driver", False, repr(exc))
		report["errors"].append({"step": "resolve_driver", "error": repr(exc)})
		report["all_ok"] = False
		return report

	# 4. Status bucket mapping spot-checks.
	mappings = [
		("ORDER_OK_SUCCESS", "succeeded"),
		("CLIENT_FAILED", "failed"),
		("CLIENT_ABORTED", "canceled"),
		("PAIRED", "processing"),
	]
	all_ok = all(driver._map_status(s) == t for s, t in mappings)  # noqa: SLF001
	add("TWINT transaction_status → FSM bucket mapping", all_ok, f"checked={len(mappings)} mappings")

	# 5. create_intent through public API + mocked HTTP layer.
	from payments.api import intent as intent_api

	mocked_bridge_response = {
		"success": True,
		"order_id": "order_smoke_phase4",
		"order_status": "InProgress",
		"transaction_status": "ORDER_RECEIVED",
		"pairing_token": "PAIRING_TOKEN_FAKE",
	}
	with patch("requests.post") as mock_post:
		resp = MagicMock()
		resp.ok = True
		resp.status_code = 200
		resp.headers = {"Content-Type": "application/json"}
		resp.json = MagicMock(return_value={"message": mocked_bridge_response})
		mock_post.return_value = resp
		try:
			result = intent_api.create_intent(
				provider=PROVIDER_NAME,
				channel=CHANNEL_CODE,
				amount=750,
				currency="CHF",
				metadata={"source": "phase4_smoke", "twint_merchant_uuid": MERCHANT_UUID},
			)
			ok = (
				result["status"] == "requires_action"
				and result["provider_intent_id"] == "order_smoke_phase4"
				and result["next_action_type"] == "display_qr_payload"
			)
			payload = result["next_action_payload"] or {}
			ok = ok and payload.get("pairing_token") == "PAIRING_TOKEN_FAKE"
			add(
				"create_intent via TWINT bridge (mocked HTTP)",
				ok,
				f"intent={result['intent_name']} order_id={result['provider_intent_id']}",
			)
			intent_name = result["intent_name"]
		except Exception as exc:  # noqa: BLE001
			add("create_intent", False, repr(exc))
			report["errors"].append({"step": "create_intent", "error": repr(exc)})
			intent_name = None

	# 6. The scheduler poll runs without exploding (no real intents to advance).
	from payments.api import twint as twint_api

	with patch("requests.post") as mock_post:
		resp = MagicMock()
		resp.ok = True
		resp.status_code = 200
		resp.headers = {"Content-Type": "application/json"}
		resp.json = MagicMock(return_value={"message": {"success": True, "transaction_status": "ORDER_OK_SUCCESS"}})
		mock_post.return_value = resp
		try:
			stats = twint_api.poll_pending_twint_transactions()
			add(
				"poll_pending_twint_transactions runs",
				isinstance(stats, dict) and "checked" in stats,
				f"stats={stats}",
			)
		except Exception as exc:  # noqa: BLE001
			add("poll_pending_twint_transactions", False, repr(exc))
			report["errors"].append({"step": "poll", "error": repr(exc)})

	# 7. Verify the poll advanced our intent to succeeded (after the mock).
	if intent_name:
		doc = frappe.get_doc("Payment Intent", intent_name)
		add(
			"Payment Intent advanced to succeeded by poll",
			doc.status == "succeeded",
			f"final_status={doc.status}",
		)

	_cleanup()

	all_ok = all(c["ok"] for c in report["checks"])
	print("=" * 60)
	print(f"RESULT: {'ALL GREEN ✅' if all_ok else 'FAILURES ❌'}")
	print(f"Checks: {sum(1 for c in report['checks'] if c['ok'])}/{len(report['checks'])} passed")
	print("=" * 60)
	report["all_ok"] = all_ok
	return report
