# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""Exercise the POSNext path for Payrexx terminal payments.

What is testable without hardware: the till resolves the mapping to the Payrexx
terminal driver, create_intent reaches the ECR API, and the failure a missing
terminal produces is clean and actionable. What is not: an actual card read.

Run with::

    bench --site <site> execute payments.tests.payrexx_pos_smoke.run

Everything here is testable without hardware **except** an actual card read:
the terminal is deliberately an unpaired serial, so check 7 asserts that the
failure is a clean TerminalNotFoundError rather than a stack trace — and check 8
that it is not misreported as transport_error, which would wrongly tell the till
the outcome is unknown when in fact nothing was charged.
"""

import json

import frappe

PROVIDER = "payrexx"
TERMINAL_CHANNEL = "terminal"
POS_PROFILE = "Caisse"
MODE = "Payrexx Terminal"

# The simulated device, never the real terminal. This smoke calls pos_start_payment,
# which on paired hardware sends a genuine payment request to the counter — a test
# suite must not put a live amount on a device someone might tap a card against.
# Before the N86 arrived this was academic; the day it was paired, this smoke started
# addressing it for real.
SIMULATED_SERIAL = "SIM-N86-0001"
SERIAL = SIMULATED_SERIAL
DEVICE_LABEL = "Payrexx N86 SIMULATEUR"


def run():
    results = []

    def check(label, ok, detail=""):
        results.append(ok)
        print(f"{'  OK  ' if ok else ' FAIL '} {label}" + (f" — {detail}" if detail else ""))

    # 1. terminal binding for the Payrexx provider
    binding = frappe.db.get_value(
        "Provider Channel Settings", {"provider": PROVIDER, "channel": TERMINAL_CHANNEL}, "name")
    if not binding:
        binding = frappe.get_doc({
            "doctype": "Provider Channel Settings", "provider": PROVIDER,
            "channel": TERMINAL_CHANNEL, "enabled": 1,
            "driver_class": "payments.drivers.payrexx.terminal_driver.PayrexxTerminalDriver",
        }).insert(ignore_permissions=True).name
        frappe.db.commit()
    check("1. terminal binding exists", bool(binding), binding)

    # 2. Payment Device standing in for the NexGo
    device = frappe.db.get_value("Payment Device", {"device_label": DEVICE_LABEL}, "name")
    if not device:
        device = frappe.get_doc({
            "doctype": "Payment Device", "device_label": DEVICE_LABEL,
            "provider_channel_settings": binding, "serial_number": SERIAL,
            "provider_device_id": SERIAL, "enabled": 1,
            # device_type starting with "simulated" is what routes the driver down
            # its simulated path — without it this smoke would address real hardware.
            "device_type": "simulated-nexgo",
        }).insert(ignore_permissions=True).name
        frappe.db.commit()
    check("2. Payment Device created", bool(device), f"{device} serial={SERIAL}")

    # POS Payment Driver Mapping is autonamed "{pos_profile}-{mode_of_payment}",
    # so a mode of payment carries exactly ONE provider per till. Offering both
    # Stripe and Payrexx at the same till therefore needs two distinct modes —
    # which is why this test creates its own rather than hijacking
    # "Carte de crédit", already mapped to Stripe.
    if not frappe.db.exists("Mode of Payment", MODE):
        frappe.get_doc({
            "doctype": "Mode of Payment", "mode_of_payment": MODE, "type": "Bank",
            "enabled": 1,
        }).insert(ignore_permissions=True)
        frappe.db.commit()

    # 3. POS mapping — what the till reads to pick a driver
    mapping = frappe.db.get_value(
        "POS Payment Driver Mapping",
        {"pos_profile": POS_PROFILE, "mode_of_payment": MODE, "provider": PROVIDER}, "name")
    if not mapping:
        mapping = frappe.get_doc({
            "doctype": "POS Payment Driver Mapping", "pos_profile": POS_PROFILE,
            "mode_of_payment": MODE, "provider": PROVIDER, "channel": TERMINAL_CHANNEL,
            "default_device": device, "enabled": 1, "auto_attach_device": 1,
        }).insert(ignore_permissions=True).name
        frappe.db.commit()
    check("3. POS Payment Driver Mapping created", bool(mapping),
          f"{POS_PROFILE}/{MODE} -> {PROVIDER}/{TERMINAL_CHANNEL}")

    # 4. the registry resolves the terminal driver
    from payments.drivers.registry import resolve_driver
    driver = resolve_driver(PROVIDER, TERMINAL_CHANNEL)
    check("4. registry resolves the Payrexx terminal driver",
          type(driver).__name__ == "PayrexxTerminalDriver", type(driver).__name__)

    # 5. the till's own resolution path — what the Vue layer actually calls
    from pos_next.api.payments import pos_get_mapping
    resolved = pos_get_mapping(pos_profile=POS_PROFILE, mode_of_payment=MODE)
    check("5. pos_get_mapping resolves the mode to Payrexx",
          (resolved or {}).get("provider") == PROVIDER, json.dumps(resolved, default=str)[:150])

    # 6. the till's payment entry point — the same call the Vue dialog makes
    invoice = frappe.db.get_value("POS Invoice", {}, "name") or frappe.db.get_value(
        "Sales Invoice", {"docstatus": 1}, "name")
    from pos_next.api.payments import pos_start_payment
    intent = pos_start_payment(
        reference_doctype="POS Invoice" if frappe.db.exists("POS Invoice", invoice) else "Sales Invoice",
        reference_name=invoice, pos_profile=POS_PROFILE, mode_of_payment=MODE,
        amount=1500, currency="CHF", device=device,
        metadata=json.dumps({"payment_method": "TWINT", "purpose": "POS test"}))
    name = intent["intent_name"]
    check("6. pos_start_payment ran through the Payrexx driver", bool(name),
          f"{name} status={intent['status']}")

    # 7. the unpaired terminal fails cleanly, not with a stack trace
    code = intent.get("error_code") or ""
    check("7. unpaired terminal reported as TerminalNotFoundError",
          code == "TerminalNotFoundError",
          f"error_code={code} message={(intent.get('error_message') or '')[:80]}")

    # 8. and it is NOT reported as an unknown outcome — nothing was charged, so the
    #    till may safely retry; a transport_error would mean the opposite.
    check("8. not misreported as transport_error", code != "transport_error", code)

    frappe.delete_doc("Payment Intent", name, force=True, ignore_permissions=True)
    frappe.db.commit()

    print(f"\n=== {sum(results)}/{len(results)} checks passed ===")
