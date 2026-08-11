# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""End-to-end smoke test: real Payrexx API through the real driver stack.

Creates a Payment Intent, drives it through the Payrexx web driver against the
live test account, checks the hosted page URL comes back, then cancels and cleans
up. **No payment is ever completed, so no money moves** — but point it at a test
account regardless.

Run it with the credentials from ``~/.config/payrexx-secrets.env``::

    bench --site <site> execute payments.tests.payrexx_smoke.run \\
      --kwargs '{"instance": "...", "api_secret": "...", "pos_secret": "..."}'

Check 8 is the one that matters most: it proves the indexed-bracket encoding
(``pm[0]=twint``) actually reached the API. A wrongly-encoded filter comes back
``200 OK`` with an empty ``pm`` and every payment method still on the hosted page,
which would let a shopper pay by a method we never recorded.
"""

import json

import frappe

PROVIDER = "payrexx_smoke"
CHANNEL = "payrexx_web"
results = []


def check(label, ok, detail=""):
    results.append((label, ok, detail))
    print(f"{'  OK  ' if ok else ' FAIL '} {label}" + (f" — {detail}" if detail else ""))


def run(instance, api_secret, pos_secret):
    # 1. Provider record with the live test credentials.
    if not frappe.db.exists("Payment Provider", PROVIDER):
        frappe.get_doc({
            "doctype": "Payment Provider", "provider_name": PROVIDER,
            "display_label": "Payrexx (smoke)", "enabled": 1, "mode": "test",
            "driver_class": "payments.drivers.payrexx.web_driver.PayrexxWebDriver",
            "credentials_json": json.dumps({
                "instance": instance, "api_secret": api_secret, "pos_api_secret": pos_secret,
            }),
        }).insert(ignore_permissions=True)
    else:
        doc = frappe.get_doc("Payment Provider", PROVIDER)
        doc.credentials_json = json.dumps({
            "instance": instance, "api_secret": api_secret, "pos_api_secret": pos_secret,
        })
        doc.save(ignore_permissions=True)
    if not frappe.db.get_value("Provider Channel Settings", {"provider": PROVIDER, "channel": CHANNEL}, "name"):
        frappe.get_doc({
            "doctype": "Provider Channel Settings", "provider": PROVIDER,
            "channel": CHANNEL, "enabled": 1,
        }).insert(ignore_permissions=True)
    frappe.db.commit()
    check("1. Provider + channel binding provisioned", True)

    # 2. Health check against the live account.
    from payments.drivers.payrexx.provider import PayrexxProvider
    health = PayrexxProvider(frappe.get_doc("Payment Provider", PROVIDER)).health_check()
    check("2. health_check reaches the live account", health.get("ok") is True,
          f"methods={health.get('active_payment_methods')}")

    # 3. Driver resolves through the registry.
    from payments.drivers.registry import resolve_driver
    driver = resolve_driver(PROVIDER, CHANNEL)
    check("3. registry resolves the Payrexx web driver", type(driver).__name__ == "PayrexxWebDriver",
          type(driver).__name__)

    # 4. Real intent through the public API.
    from payments.api.intent import create_intent
    intent = create_intent(provider=PROVIDER, channel=CHANNEL, amount=1500, currency="CHF",
                           metadata=json.dumps({"purpose": "payrexx smoke test",
                                                "payment_methods": ["twint"]}))
    # create_intent returns "intent_name", not "name".
    name = intent["intent_name"]
    doc = frappe.get_doc("Payment Intent", name)
    check("4. create_intent produced a Payment Intent", bool(name), name)
    check("5. status is requires_action", doc.status == "requires_action", doc.status)
    check("6. provider_intent_id is the Payrexx gateway id", bool(doc.provider_intent_id),
          str(doc.provider_intent_id))

    url = (intent.get("next_action_payload") or {}).get("url", "")
    check("7. hosted page URL on the instance subdomain", instance in url and "payment=" in url, url)

    # 8. Payrexx honoured the pm filter — the silent-drop trap.
    with driver._client() as client:
        gw = client.gateway.retrieve(doc.provider_intent_id)
    check("8. pm filter honoured (indexed encoding reached the API)",
          gw.filter_was_applied and "twint" in gw.payment_methods, str(list(gw.payment_methods)))
    check("9. referenceId round-trips as our intent name", gw.reference_id == name, str(gw.reference_id))

    # 10. get_status maps the live status.
    response = driver.get_status(doc.provider_intent_id)
    check("10. get_status maps waiting -> requires_action", response.status == "requires_action",
          response.status)

    # 11. Cancel removes the gateway (nothing was paid).
    cancelled = driver.cancel_intent(doc.provider_intent_id)
    check("11. cancel_intent deletes the gateway", cancelled.status == "canceled", cancelled.status)

    # Clean up our own trace.
    frappe.delete_doc("Payment Intent", name, force=True, ignore_permissions=True)
    frappe.db.commit()
    check("12. test Payment Intent cleaned up", not frappe.db.exists("Payment Intent", name))

    # Disable the provider this smoke provisioned. Leaving it enabled makes it a
    # candidate for real payments: provider resolution prefers a live provider but
    # otherwise picks by name, so a second enabled test provider is at best ambiguity
    # logged on every payment, and on a site whose real provider sorts after
    # "payrexx_smoke" it would serve customers from this smoke's credentials.
    # Disabled rather than deleted — re-running the smoke re-enables it, and the record
    # keeps its credentials so nobody has to paste them again.
    frappe.db.set_value("Payment Provider", PROVIDER, "enabled", 0)
    frappe.db.commit()
    check("13. smoke provider disabled again",
          not frappe.db.get_value("Payment Provider", PROVIDER, "enabled"),
          f"{PROVIDER} left disabled so it cannot capture a real payment")

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n=== {passed}/{len(results)} checks passed ===")
    return passed == len(results)
