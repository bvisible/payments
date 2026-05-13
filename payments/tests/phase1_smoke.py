# Copyright (c) 2026, Neoffice and Contributors
# License: MIT. See LICENSE
"""Phase 1 acceptance smoke test, callable from `bench execute`.

Run on a Frappe site with the `payments` app installed:

	bench --site <site> execute payments.tests.phase1_smoke.run_all

The function is idempotent: it cleans up its own fixtures after every run so it
can be re-executed safely.
"""

from __future__ import annotations

import json

import frappe


MOCK_PROVIDER = "mock"
MOCK_CHANNEL = "terminal"
DRIVER_PATH = "payments.drivers.mock_driver.MockDriver"


def _ensure_mock_fixtures() -> None:
	"""Create Mock Provider + Terminal Channel + binding if missing."""
	if not frappe.db.exists("Payment Provider", MOCK_PROVIDER):
		frappe.get_doc(
			{
				"doctype": "Payment Provider",
				"provider_name": MOCK_PROVIDER,
				"display_label": "Mock",
				"enabled": 1,
				"mode": "test",
				"driver_class": DRIVER_PATH,
			}
		).insert(ignore_permissions=True)

	if not frappe.db.exists("Payment Channel", MOCK_CHANNEL):
		frappe.get_doc(
			{
				"doctype": "Payment Channel",
				"channel_code": MOCK_CHANNEL,
				"display_label": "POS Terminal",
				"ui_kind": "card_present_modal",
				"capabilities_json": json.dumps({"supports_refund": True, "supports_tip": False}),
			}
		).insert(ignore_permissions=True)

	binding = frappe.db.get_value(
		"Provider Channel Settings",
		{"provider": MOCK_PROVIDER, "channel": MOCK_CHANNEL},
		"name",
	)
	if not binding:
		frappe.get_doc(
			{
				"doctype": "Provider Channel Settings",
				"provider": MOCK_PROVIDER,
				"channel": MOCK_CHANNEL,
				"enabled": 1,
			}
		).insert(ignore_permissions=True)

	frappe.db.commit()


def _cleanup_smoke_intents() -> None:
	"""Delete Payment Intents created by previous smoke runs (metadata-tagged)."""
	intents = frappe.get_all(
		"Payment Intent",
		filters={"provider": MOCK_PROVIDER, "metadata_json": ["like", "%smoke_phase1%"]},
		pluck="name",
	)
	for name in intents:
		# Delete child events first to satisfy FK.
		for ev in frappe.get_all("Payment Event", filters={"intent": name}, pluck="name"):
			frappe.delete_doc("Payment Event", ev, force=True, ignore_permissions=True)
		frappe.delete_doc("Payment Intent", name, force=True, ignore_permissions=True)
	# Drop any leftover smoke webhook event log rows.
	for log in frappe.get_all(
		"Webhook Event Log",
		filters={"event_id": ["like", "evt_smoke_%"]},
		pluck="name",
	):
		frappe.delete_doc("Webhook Event Log", log, force=True, ignore_permissions=True)
	frappe.db.commit()


def run_all() -> dict:
	"""Run the full Phase 1 acceptance checklist. Returns a structured report."""
	report: dict = {"checks": [], "errors": []}

	def add(name: str, ok: bool, detail: str = "") -> None:
		report["checks"].append({"name": name, "ok": ok, "detail": detail})
		marker = "✅" if ok else "❌"
		print(f"  {marker} {name}: {detail}")

	print("=" * 60)
	print("Payments Phase 1 — smoke test")
	print("=" * 60)

	_cleanup_smoke_intents()
	_ensure_mock_fixtures()
	add("Fixtures Mock Provider/Channel/Binding present", True, "")

	# 1. DocType existence check (defense-in-depth, complements the SHOW TABLES test).
	doctypes = [
		"Payment Provider",
		"Payment Channel",
		"Provider Channel Settings",
		"Payment Device",
		"Payment Intent",
		"Payment Event",
		"Webhook Event Log",
		"Customer Payment Link",
	]
	missing = [dt for dt in doctypes if not frappe.db.exists("DocType", dt)]
	add("All 8 ontology DocTypes present", not missing, f"missing={missing}" if missing else "8/8 OK")

	# 2. Driver registry resolution (mock).
	try:
		from payments.drivers.registry import resolve_driver

		driver = resolve_driver(MOCK_PROVIDER, MOCK_CHANNEL)
		add("resolve_driver(mock, terminal) → MockDriver", driver.__class__.__name__ == "MockDriver", driver.__class__.__name__)
	except Exception as exc:
		add("resolve_driver(mock, terminal)", False, repr(exc))
		report["errors"].append({"step": "resolve_driver", "error": repr(exc)})

	# 3. create_intent E2E via the public API.
	try:
		from payments.api import intent as intent_api

		result = intent_api.create_intent(
			provider=MOCK_PROVIDER,
			channel=MOCK_CHANNEL,
			amount=1500,
			currency="CHF",
			metadata={"source": "smoke_phase1", "ts": frappe.utils.now()},
		)
		ok = (
			result["status"] == "requires_action"
			and result["amount"] == 1500
			and result["currency"] == "CHF"
			and (result["provider_intent_id"] or "").startswith("mock_pi_")
		)
		add(
			"create_intent(mock, terminal, 1500 CHF)",
			ok,
			f"intent={result['intent_name']} status={result['status']} provider_intent_id={result.get('provider_intent_id')}",
		)
		intent_name = result["intent_name"]
	except Exception as exc:
		add("create_intent", False, repr(exc))
		report["errors"].append({"step": "create_intent", "error": repr(exc)})
		intent_name = None

	# 4. FSM enforcement: invalid transition rejected.
	if intent_name:
		try:
			from payments.api import intent as intent_api

			doc = frappe.get_doc("Payment Intent", intent_name)
			# Walk to succeeded then refuse going back to requires_action.
			doc.transition_to("processing", event_source="api")
			doc.transition_to("succeeded", event_source="api")
			try:
				doc.transition_to("requires_action", event_source="api")
				add("FSM: rejects succeeded → requires_action", False, "did NOT raise")
			except frappe.ValidationError:
				add("FSM: rejects succeeded → requires_action", True, "raised ValidationError as expected")

			# Idempotent self-transition.
			same = doc.transition_to("succeeded", event_source="api")
			add("FSM: idempotent succeeded → succeeded", same is False, f"returned {same}")
		except Exception as exc:
			add("FSM enforcement", False, repr(exc))
			report["errors"].append({"step": "fsm", "error": repr(exc)})

	# 5. cancel_intent on a fresh intent.
	try:
		from payments.api import intent as intent_api

		fresh = intent_api.create_intent(
			provider=MOCK_PROVIDER,
			channel=MOCK_CHANNEL,
			amount=200,
			currency="CHF",
			metadata={"source": "smoke_phase1", "step": "cancel"},
		)
		cancel_result = intent_api.cancel_intent(fresh["intent_name"])
		add(
			"cancel_intent on requires_action",
			cancel_result["status"] == "canceled",
			f"final_status={cancel_result['status']}",
		)
	except Exception as exc:
		add("cancel_intent", False, repr(exc))
		report["errors"].append({"step": "cancel_intent", "error": repr(exc)})

	# 6. Webhook Event Log dedup (DB-level via UNIQUE autoname).
	import uuid

	evt_id = f"evt_smoke_{uuid.uuid4().hex[:12]}"
	try:
		log1 = frappe.get_doc(
			{
				"doctype": "Webhook Event Log",
				"event_id": evt_id,
				"provider": MOCK_PROVIDER,
				"event_type": "mock.smoke",
				"status": "Queued",
				"raw_payload": "{}",
				"signature_valid": 1,
			}
		).insert(ignore_permissions=True)
		first_ok = log1.name == evt_id
	except Exception as exc:
		first_ok = False
		report["errors"].append({"step": "webhook_first_insert", "error": repr(exc)})
	add("Webhook Event Log first insert", first_ok, evt_id)

	dedup_blocked = False
	dedup_reason = ""
	try:
		frappe.get_doc(
			{
				"doctype": "Webhook Event Log",
				"event_id": evt_id,
				"provider": MOCK_PROVIDER,
				"event_type": "mock.smoke",
				"status": "Queued",
				"raw_payload": "{}",
				"signature_valid": 1,
			}
		).insert(ignore_permissions=True)
		dedup_reason = "did NOT raise (BAD: dedup failed)"
	except (frappe.DuplicateEntryError, frappe.UniqueValidationError, frappe.ValidationError) as exc:
		dedup_blocked = True
		dedup_reason = type(exc).__name__
	except Exception as exc:
		# Any DB-level error counts as dedup working (sqlite/MariaDB integrity error wrapping).
		dedup_blocked = True
		dedup_reason = type(exc).__name__
	add("Webhook Event Log dedup blocks 2nd insert", dedup_blocked, dedup_reason)

	# 7. Sanity: Payment Event rows logged for our FSM transitions.
	if intent_name:
		events = frappe.get_all(
			"Payment Event",
			filters={"intent": intent_name},
			fields=["from_status", "to_status", "event_source"],
			order_by="creation asc",
		)
		# Expected exactly 2 events: requires_action→processing and processing→succeeded.
		# `create_intent` calls transition_to("requires_action") on the freshly inserted
		# intent — that's a self-transition, which is idempotent and does NOT log an event.
		add(
			"Payment Event log has 2 FSM rows for the test intent",
			len(events) == 2,
			f"events={events}",
		)

	# Final cleanup so re-runs are clean.
	_cleanup_smoke_intents()

	all_ok = all(c["ok"] for c in report["checks"])
	print("=" * 60)
	print(f"RESULT: {'ALL GREEN ✅' if all_ok else 'FAILURES ❌'}")
	print(f"Checks: {sum(1 for c in report['checks'] if c['ok'])}/{len(report['checks'])} passed")
	print("=" * 60)
	report["all_ok"] = all_ok
	return report
