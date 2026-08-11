# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""Prove a Payrexx terminal payment can be exercised without hardware.

The full till round-trip: start a payment on a simulated device, accept it from
the simulator panel, then return the money. Also checks the two guards, because a
simulator that leaked into production would mark real invoices paid.

Run with::

    bench --site <site> execute payments.tests.payrexx_simulator_smoke.run

Requires a Payment Device whose ``device_type`` starts with ``simulated`` and a
Payrexx provider in ``test`` mode; the test creates the device if absent.
"""

import json

import frappe

PROVIDER = "payrexx"
CHANNEL = "terminal"
POS_PROFILE = "Caisse"
MODE = "Payrexx Terminal"
SIM_LABEL = "Payrexx N86 SIMULATEUR"


def run():
    results = []

    def check(label, ok, detail=""):
        results.append(ok)
        print(f"{'  OK  ' if ok else ' FAIL '} {label}" + (f" — {detail}" if detail else ""))

    binding = frappe.db.get_value(
        "Provider Channel Settings", {"provider": PROVIDER, "channel": CHANNEL}, "name")

    # A simulated device: device_type is what flags it.
    device = frappe.db.get_value("Payment Device", {"device_label": SIM_LABEL}, "name")
    if not device:
        device = frappe.get_doc({
            "doctype": "Payment Device", "device_label": SIM_LABEL,
            "provider_channel_settings": binding, "device_type": "simulated-nexgo",
            "serial_number": "SIM-N86-0001", "provider_device_id": "SIM-N86-0001",
            "enabled": 1,
        }).insert(ignore_permissions=True).name
        frappe.db.commit()
    check("1. simulated Payment Device", bool(device),
          f"{device} type={frappe.db.get_value('Payment Device', device, 'device_type')}")

    # 1. Start a payment — this used to die on 404 Terminal not found.
    invoice = frappe.db.get_value("POS Invoice", {}, "name") or frappe.db.get_value(
        "Sales Invoice", {"docstatus": 1}, "name")
    from pos_next.api.payments import pos_start_payment
    intent = pos_start_payment(
        reference_doctype="POS Invoice" if frappe.db.exists("POS Invoice", invoice) else "Sales Invoice",
        reference_name=invoice, pos_profile=POS_PROFILE, mode_of_payment=MODE,
        amount=2500, currency="CHF", device=device,
        metadata=json.dumps({"payment_method": "card", "purpose": "simulator test"}))
    name = intent["intent_name"]
    # The driver answers requires_action; pos_start_payment then attaches the device
    # and moves the intent to processing. Either is a healthy start.
    check("2. payment starts on a simulated device",
          intent["status"] in ("requires_action", "processing"),
          f"{name} status={intent['status']} error={intent.get('error_code')}")
    check("3. synthetic payment id, no ECR call",
          str(intent.get("provider_intent_id") or "").startswith("sim_"),
          str(intent.get("provider_intent_id")))
    check("4. till renders its usual card-present dialog",
          intent.get("next_action_type") == "display_card_present_modal",
          str(intent.get("next_action_type")))

    # 2. Read the status back — must not reach for an ECR payment that does not exist.
    from payments.drivers.registry import resolve_driver
    driver = resolve_driver(PROVIDER, CHANNEL)
    status = driver.get_status(intent["provider_intent_id"], device_id=device)
    check("5. get_status stays local for a simulated payment",
          status.status == "processing" and status.next_action_payload.get("simulated") is True,
          f"{status.status} simulated={status.next_action_payload.get('simulated')}")

    # 3. Accept it from the simulator panel — the cashier's "Accept" button.
    from pos_next.api.payments import pos_simulate_terminal_outcome
    sim = pos_simulate_terminal_outcome(intent_name=name, outcome="succeeded")
    frappe.db.commit()
    doc = frappe.get_doc("Payment Intent", name)
    check("6. simulator drives the FSM to succeeded", doc.status == "succeeded",
          f"{doc.status} (panel returned {sim.get('status')})")

    # 4. Return the money — the POS return flow, without hardware.
    refund = driver.refund(intent["provider_intent_id"], amount=2500)
    check("7. refund of a simulated payment stays local",
          refund.status == "refunded" and refund.next_action_payload.get("simulated") is True,
          f"{refund.status} method={refund.next_action_payload.get('method')}")

    # 5. The guard: a simulated device on a live provider must be refused.
    prov = frappe.get_doc("Payment Provider", PROVIDER)
    prov.mode = "live"
    prov.save(ignore_permissions=True)
    frappe.db.commit()
    try:
        from payments.drivers.base import IntentRequest
        blocked = resolve_driver(PROVIDER, CHANNEL).create_intent(
            IntentRequest(intent_name="PI-GUARD-TEST", amount=100, currency="CHF",
                          device_id=device))
        check("8. simulator refused on a live provider",
              blocked.status == "failed" and blocked.error_code == "simulator_not_allowed",
              f"{blocked.status}/{blocked.error_code}")
    finally:
        prov.reload()
        prov.mode = "test"
        prov.save(ignore_permissions=True)
        frappe.db.commit()

    frappe.delete_doc("Payment Intent", name, force=True, ignore_permissions=True)
    frappe.db.commit()
    print(f"\n=== {sum(results)}/{len(results)} checks passed ===")
