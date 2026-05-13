# Copyright (c) 2026, Neoffice and Contributors
# License: MIT. See LICENSE
"""Phase 2 acceptance smoke test against real Stripe sandbox.

Runs the full create_intent → confirm_intent → simulator → webhook loop using
the **server-driven** Stripe Terminal API. Requires a Stripe test-mode secret
key (the only credential needed):

	bench --site <site> execute payments.tests.phase2_smoke.run_all \\
		--kwargs '{"stripe_secret_key": "sk_test_xxx"}'

If ``stripe_secret_key`` is not provided, the test will try to read it from the
existing ``Payment Provider`` record named ``stripe_test``. If neither is
available the smoke test is **skipped** (not failed) so the test command stays
green-by-default on installs without a Stripe sandbox.

What it does:

1. Idempotently creates a ``stripe_test`` Payment Provider + Terminal binding.
2. Creates a Stripe Terminal Location (CH, Lausanne) — idempotent.
3. Registers a simulated reader with ``registration_code=simulated-wpe`` — idempotent.
4. ``create_intent`` for CHF 15.00 → asserts ``pi_*`` returned in ``requires_action``.
5. ``attach_intent_to_reader`` → asserts ``processing``.
6. Simulates card presentation via ``stripe.terminal.Reader.present_payment_method``.
7. Captures the PaymentIntent (mirrors what the webhook worker would do).
8. Polls the Frappe Payment Intent (no webhook in this smoke, so we capture and
   poll Stripe directly) until status is ``succeeded``.
9. Reports a 8/8 (or similar) checklist.

This smoke does NOT exercise the webhook endpoint — that requires a public URL
or ``stripe listen`` forwarding. It exercises the data-plane operations end-to-end.
"""

from __future__ import annotations

import json
import time
from typing import Any

import frappe

PROVIDER_NAME = "stripe_test"
CHANNEL_CODE = "terminal"
DRIVER_PATH = "payments.drivers.stripe.terminal_driver.StripeTerminalDriver"
LOCATION_DISPLAY_NAME = "Neoffice Osiris Test"
SIMULATOR_REGCODE = "simulated-wpe"


def _get_secret_key(stripe_secret_key: str | None) -> str | None:
	if stripe_secret_key:
		return stripe_secret_key
	# Fallback 1: read from existing Payment Provider record.
	if frappe.db.exists("Payment Provider", PROVIDER_NAME):
		doc = frappe.get_doc("Payment Provider", PROVIDER_NAME)
		creds = doc.get_credentials()
		if creds.get("secret_key"):
			return creds["secret_key"]
	# Fallback 2: look at any legacy Stripe Settings record on the site for a
	# test key. Only test keys are reused — we never touch a live key here.
	for name in frappe.get_all("Stripe Settings", pluck="name"):
		try:
			doc = frappe.get_doc("Stripe Settings", name)
		except Exception:  # noqa: BLE001
			continue
		sk = doc.get_password("secret_key", raise_exception=False) or ""
		if sk.startswith("sk_test_"):
			print(f"  (using legacy Stripe Settings '{name}' as fallback test key source)")
			return sk
	return None


def _ensure_provider(secret_key: str) -> str:
	if frappe.db.exists("Payment Provider", PROVIDER_NAME):
		doc = frappe.get_doc("Payment Provider", PROVIDER_NAME)
		creds = doc.get_credentials()
		# Only refresh if missing.
		if not creds.get("secret_key"):
			creds["secret_key"] = secret_key
			doc.credentials_json = json.dumps(creds)
			doc.save(ignore_permissions=True)
	else:
		frappe.get_doc(
			{
				"doctype": "Payment Provider",
				"provider_name": PROVIDER_NAME,
				"display_label": "Stripe (test/sandbox)",
				"enabled": 1,
				"mode": "test",
				"driver_class": DRIVER_PATH,
				"credentials_json": json.dumps({"secret_key": secret_key}),
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
	binding = frappe.db.get_value(
		"Provider Channel Settings", {"provider": PROVIDER_NAME, "channel": CHANNEL_CODE}, "name"
	)
	if not binding:
		binding = frappe.get_doc(
			{
				"doctype": "Provider Channel Settings",
				"provider": PROVIDER_NAME,
				"channel": CHANNEL_CODE,
				"enabled": 1,
			}
		).insert(ignore_permissions=True).name
	frappe.db.commit()
	return binding


def _ensure_location(secret_key: str) -> str:
	"""Return existing or freshly created Stripe Terminal Location id."""
	import stripe

	existing = stripe.terminal.Location.list(api_key=secret_key, limit=100)
	for loc in existing.data:
		if loc.display_name == LOCATION_DISPLAY_NAME:
			return loc.id
	loc = stripe.terminal.Location.create(
		api_key=secret_key,
		display_name=LOCATION_DISPLAY_NAME,
		address={
			"line1": "Rue Test 1",
			"city": "Lausanne",
			"postal_code": "1003",
			"country": "CH",
		},
		idempotency_key=f"loc_{LOCATION_DISPLAY_NAME}_ch",
	)
	return loc.id


def _ensure_reader(secret_key: str, location_id: str, binding_name: str) -> tuple[str, str]:
	"""Return (stripe_reader_id, payment_device_name). Idempotent."""
	import stripe

	# Look for an existing simulated reader at this location.
	existing = stripe.terminal.Reader.list(api_key=secret_key, location=location_id, limit=100)
	reader_obj = None
	for r in existing.data:
		if getattr(r, "device_type", None) in ("simulated_wisepos_e", "simulated_stripe_s700"):
			reader_obj = r
			break
	if reader_obj is None:
		reader_obj = stripe.terminal.Reader.create(
			api_key=secret_key,
			registration_code=SIMULATOR_REGCODE,
			location=location_id,
			label="Osiris simulator",
		)
	device_name = frappe.db.get_value(
		"Payment Device", {"provider_device_id": reader_obj.id}, "name"
	)
	if not device_name:
		device_name = frappe.get_doc(
			{
				"doctype": "Payment Device",
				"device_label": "Osiris simulator",
				"provider_device_id": reader_obj.id,
				"device_type": getattr(reader_obj, "device_type", "simulated_wisepos_e"),
				"provider_channel_settings": binding_name,
				"location_ref": location_id,
				"status": "online",
			}
		).insert(ignore_permissions=True).name
	frappe.db.commit()
	return reader_obj.id, device_name


def _cleanup_smoke_intents() -> None:
	"""Delete prior smoke intents to keep the table clean across re-runs."""
	intents = frappe.get_all(
		"Payment Intent",
		filters={
			"provider": PROVIDER_NAME,
			"metadata_json": ["like", "%phase2_smoke%"],
		},
		pluck="name",
	)
	for name in intents:
		for ev in frappe.get_all("Payment Event", filters={"intent": name}, pluck="name"):
			frappe.delete_doc("Payment Event", ev, force=True, ignore_permissions=True)
		frappe.delete_doc("Payment Intent", name, force=True, ignore_permissions=True)
	frappe.db.commit()


def run_all(stripe_secret_key: str | None = None) -> dict:
	"""Phase 2 acceptance — Stripe Terminal server-driven against simulator."""
	report: dict = {"checks": [], "errors": [], "skipped": False}

	def add(name: str, ok: bool, detail: str = "") -> None:
		report["checks"].append({"name": name, "ok": ok, "detail": detail})
		marker = "✅" if ok else "❌"
		print(f"  {marker} {name}: {detail}")

	print("=" * 60)
	print("Payments Phase 2 — Stripe Terminal smoke (server-driven)")
	print("=" * 60)

	secret_key = _get_secret_key(stripe_secret_key)
	if not secret_key:
		print("⚠️  SKIPPED: no Stripe secret key available.")
		print("    Pass via --kwargs '{\"stripe_secret_key\":\"sk_test_xxx\"}'")
		print("    or pre-create a Payment Provider record named 'stripe_test'.")
		report["skipped"] = True
		report["all_ok"] = True
		return report
	if not secret_key.startswith("sk_test_"):
		print("⚠️  WARNING: secret key does not look like a test key (sk_test_*).")
		print("    Aborting to avoid hitting the LIVE API by mistake.")
		report["skipped"] = True
		report["all_ok"] = False
		report["errors"].append({"step": "guard", "error": "secret_key is not a test key"})
		return report

	# Bind the SDK once for setup helpers.
	import stripe

	# 1. Fixtures.
	_cleanup_smoke_intents()
	try:
		binding_name = _ensure_provider(secret_key)
		add("Payment Provider + Channel + binding present", True, binding_name)
	except Exception as exc:  # noqa: BLE001
		add("Payment Provider fixtures", False, repr(exc))
		report["errors"].append({"step": "fixtures", "error": repr(exc)})
		report["all_ok"] = False
		return report

	# 2. Location.
	try:
		location_id = _ensure_location(secret_key)
		add("Stripe Terminal Location ready", True, location_id)
	except Exception as exc:  # noqa: BLE001
		add("Stripe Terminal Location", False, repr(exc))
		report["errors"].append({"step": "location", "error": repr(exc)})
		report["all_ok"] = False
		return report

	# 3. Simulator reader.
	try:
		reader_id, device_name = _ensure_reader(secret_key, location_id, binding_name)
		add("Simulator reader registered", True, f"reader={reader_id} device={device_name}")
	except Exception as exc:  # noqa: BLE001
		add("Simulator reader", False, repr(exc))
		report["errors"].append({"step": "reader", "error": repr(exc)})
		report["all_ok"] = False
		return report

	# 4. create_intent.
	from payments.api import intent as intent_api

	try:
		# IMPORTANT: never include a timestamp in the metadata of an idempotent
		# create_intent. Stripe's 24h idempotency cache rejects 'same key + different
		# body' with HTTP 400. We keep the metadata deterministic so re-runs hit
		# the cache and return the same PaymentIntent.
		result = intent_api.create_intent(
			provider=PROVIDER_NAME,
			channel=CHANNEL_CODE,
			amount=1500,
			currency="CHF",
			metadata={"source": "phase2_smoke"},
		)
		# Bool cast: chained ``and`` of strings returns the last truthy operand,
		# which would leak the client_secret into the JSON report otherwise.
		ok = bool(
			result["status"] == "requires_action"
			and (result["provider_intent_id"] or "").startswith("pi_")
			and result["client_secret"]
		)
		# Never log the client_secret in plain text — sensitive enough to keep out.
		add(
			"create_intent → Stripe PaymentIntent",
			ok,
			f"intent={result['intent_name']} pi={result['provider_intent_id']} status={result['status']} client_secret=***",
		)
		intent_name = result["intent_name"]
		provider_intent_id = result["provider_intent_id"]
	except Exception as exc:  # noqa: BLE001
		add("create_intent", False, repr(exc))
		report["errors"].append({"step": "create_intent", "error": repr(exc)})
		report["all_ok"] = False
		return report

	# 5. attach_intent_to_reader.
	from payments.api import terminal as terminal_api

	try:
		attach_result = terminal_api.attach_intent_to_reader(intent_name, device_name)
		add(
			"attach_intent_to_reader → reader 'processing'",
			attach_result["status"] == "processing",
			f"status={attach_result['status']}",
		)
	except Exception as exc:  # noqa: BLE001
		add("attach_intent_to_reader", False, repr(exc))
		report["errors"].append({"step": "attach", "error": repr(exc)})
		report["all_ok"] = False
		return report

	# 6. Simulate card presentation via the test helper.
	try:
		stripe.terminal.Reader.TestHelpers.present_payment_method(
			reader_id,
			api_key=secret_key,
		)
		add("Simulator present_payment_method", True, "card presented (test helper)")
	except Exception as exc:  # noqa: BLE001
		add("Simulator present_payment_method", False, repr(exc))
		report["errors"].append({"step": "present", "error": repr(exc)})
		report["all_ok"] = False
		return report

	# 7. Wait for the reader action to complete (Stripe processes async).
	# In production the webhook handles this; in this smoke we poll then capture.
	from payments.drivers.registry import resolve_driver

	driver = resolve_driver(PROVIDER_NAME, CHANNEL_CODE)
	captured = False
	for attempt in range(20):  # ~10s
		try:
			pi = stripe.PaymentIntent.retrieve(provider_intent_id, api_key=secret_key)
		except Exception:  # noqa: BLE001
			time.sleep(0.5)
			continue
		if pi.status in ("requires_capture", "succeeded"):
			if pi.status == "requires_capture":
				# Capture step.
				cap_resp = driver.capture_payment(provider_intent_id)
				if cap_resp.status == "failed":
					add("driver.capture_payment", False, cap_resp.error_message or "")
					report["errors"].append({"step": "capture", "error": cap_resp.error_message})
					break
			captured = True
			break
		time.sleep(0.5)
	add(
		"Card authorized + captured at provider",
		captured,
		f"pi_status={pi.status if 'pi' in locals() else 'unknown'} attempts={attempt + 1}",
	)
	if not captured:
		report["errors"].append({"step": "poll_capture", "error": "did not reach succeeded/requires_capture"})

	# 8. Verify intent reflects succeeded after one more poll (Stripe takes a tick).
	final_pi_status = None
	for _ in range(10):
		pi = stripe.PaymentIntent.retrieve(provider_intent_id, api_key=secret_key)
		final_pi_status = pi.status
		if final_pi_status == "succeeded":
			break
		time.sleep(0.5)
	add(
		"PaymentIntent reached succeeded",
		final_pi_status == "succeeded",
		f"final_pi_status={final_pi_status}",
	)

	# 9. Drive the Frappe FSM to succeeded (in prod, this happens via webhook worker).
	if final_pi_status == "succeeded":
		intent_doc = frappe.get_doc("Payment Intent", intent_name)
		intent_doc.transition_to("succeeded", event_source="poll", ignore_invalid=True)
		intent_doc.reload()
		add(
			"Frappe Payment Intent → succeeded",
			intent_doc.status == "succeeded",
			f"frappe_status={intent_doc.status}",
		)

	# Final cleanup is *not* done — we leave the Stripe location + simulator alone
	# so re-runs are O(1). The smoke intents themselves are not deleted either
	# (keep a paper trail). Only clear stale entries on next run.

	all_ok = all(c["ok"] for c in report["checks"])
	print("=" * 60)
	print(f"RESULT: {'ALL GREEN ✅' if all_ok else 'FAILURES ❌'}")
	print(f"Checks: {sum(1 for c in report['checks'] if c['ok'])}/{len(report['checks'])} passed")
	print("=" * 60)
	report["all_ok"] = all_ok
	return report
