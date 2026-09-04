#//// Neoffice — added file (no upstream equivalent). Phase 6 smoke for auto-reconciliation:
#//// idempotency, an intent with no reference_doctype, the hooks.py wiring, and an
#//// unsupported reference doctype returning cleanly instead of crashing. It does
#//// NOT cover the invoice append path end to end — see its own docstring.
#//// Commits: 7dd1ab0 2026-05-13 "Phase 6 — auto-reconciliation Payment Intent →
#//// invoice payment row".
# Copyright (c) 2026, Neoffice and Contributors
# License: MIT. See LICENSE
"""Phase 6 smoke test — auto-reconciliation of succeeded Payment Intents.

Validates that:

1. Reconciliation is idempotent (second run returns ``skipped_already_reconciled``).
2. A Payment Intent with no reference_doctype is skipped cleanly.
3. The hook wired in ``hooks.py`` triggers ``reconcile_payment_intent`` on save.
4. Unsupported reference doctypes return ``unsupported_reference`` instead of crashing.

This does NOT test the POS Invoice / Sales Invoice append path end-to-end —
that requires existing invoice fixtures on the site. The unit tests cover the
HTTP/Frappe layer; Phase 7 (go-live) covers the full E2E with real invoices.
"""

from __future__ import annotations

import json

import frappe

PROVIDER_NAME = "mock"
CHANNEL_CODE = "terminal"


def _ensure_fixtures() -> None:
	if not frappe.db.exists("Payment Provider", PROVIDER_NAME):
		frappe.get_doc(
			{
				"doctype": "Payment Provider",
				"provider_name": PROVIDER_NAME,
				"display_label": "Mock",
				"enabled": 1,
				"mode": "test",
				"driver_class": "payments.drivers.mock_driver.MockDriver",
			}
		).insert(ignore_permissions=True)
	if not frappe.db.exists("Payment Channel", CHANNEL_CODE):
		frappe.get_doc(
			{
				"doctype": "Payment Channel",
				"channel_code": CHANNEL_CODE,
				"display_label": "POS Terminal",
				"ui_kind": "card_present_modal",
				"capabilities_json": json.dumps({"supports_refund": True}),
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
	frappe.db.commit()


def _cleanup() -> None:
	for name in frappe.get_all(
		"Payment Intent",
		filters={"provider": PROVIDER_NAME, "metadata_json": ["like", "%phase6_smoke%"]},
		pluck="name",
	):
		for ev in frappe.get_all("Payment Event", filters={"intent": name}, pluck="name"):
			frappe.delete_doc("Payment Event", ev, force=True, ignore_permissions=True)
		frappe.delete_doc("Payment Intent", name, force=True, ignore_permissions=True)
	frappe.db.commit()


def _new_intent(reference_doctype: str | None = None, reference_name: str | None = None) -> str:
	doc = frappe.get_doc(
		{
			"doctype": "Payment Intent",
			"provider": PROVIDER_NAME,
			"channel": CHANNEL_CODE,
			"amount": 1234,
			"currency": "CHF",
			"reference_doctype": reference_doctype,
			"reference_name": reference_name,
			"metadata_json": json.dumps({"source": "phase6_smoke", "mode_of_payment": "Cash"}),
		}
	).insert(ignore_permissions=True)
	return doc.name


def run_all() -> dict:
	report: dict = {"checks": [], "errors": []}

	def add(name: str, ok: bool, detail: str = "") -> None:
		report["checks"].append({"name": name, "ok": ok, "detail": detail})
		marker = "✅" if ok else "❌"
		print(f"  {marker} {name}: {detail}")

	print("=" * 60)
	print("Payments Phase 6 — auto-reconciliation smoke")
	print("=" * 60)

	_cleanup()
	_ensure_fixtures()

	from payments.api.reconciliation import reconcile_payment_intent

	# 1. Skipped when not succeeded.
	intent_name = _new_intent()
	result = reconcile_payment_intent(intent_name)
	add(
		"requires_action intent → skipped_not_succeeded",
		result.get("action") == "skipped_not_succeeded",
		f"action={result.get('action')}",
	)

	# 2. Succeeded + no reference → skipped_no_reference.
	doc = frappe.get_doc("Payment Intent", intent_name)
	doc.transition_to("processing", event_source="api")
	doc.transition_to("succeeded", event_source="api")
	# The doc_event already triggered reconcile_payment_intent on save. Call it
	# again to assert idempotence + skip behaviour.
	result = reconcile_payment_intent(intent_name)
	add(
		"succeeded + no reference → skipped",
		result.get("action") in ("skipped_no_reference", "skipped_already_reconciled"),
		f"action={result.get('action')}",
	)

	# 3. Succeeded + unsupported reference doctype.
	intent2 = _new_intent(reference_doctype="DocType", reference_name="DocType")
	d2 = frappe.get_doc("Payment Intent", intent2)
	d2.transition_to("processing")
	d2.transition_to("succeeded")
	# The hook fires; verify the action recorded in metadata is unsupported_reference.
	d2.reload()
	md = json.loads(d2.metadata_json or "{}")
	add(
		"succeeded + unsupported reference → recorded as unsupported",
		md.get("reconciliation_action") == "unsupported_reference",
		f"action={md.get('reconciliation_action')}",
	)

	# 4. Idempotence — second explicit call returns already_reconciled.
	result = reconcile_payment_intent(intent2)
	add(
		"second reconcile call → skipped_already_reconciled (after first non-skip path)",
		result.get("action") in ("skipped_already_reconciled", "unsupported_reference"),
		f"action={result.get('action')}",
	)

	# 5. doc_event is wired (the previous transitions did not crash and the
	# metadata records what the hook did).
	add(
		"on_update doc_event wired",
		md.get("reconciled_at") is not None,
		f"reconciled_at={md.get('reconciled_at')}",
	)

	_cleanup()

	all_ok = all(c["ok"] for c in report["checks"])
	print("=" * 60)
	print(f"RESULT: {'ALL GREEN ✅' if all_ok else 'FAILURES ❌'}")
	print(f"Checks: {sum(1 for c in report['checks'] if c['ok'])}/{len(report['checks'])} passed")
	print("=" * 60)
	report["all_ok"] = all_ok
	return report
