# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""Exercise the exact two calls the Payrexx checkout button makes.

1. webshop.controllers.payment_handler.create_payment_request(payment_gateway='Payrexx')
   — our //// Neoffice patch: resolve the gateway by name rather than by its
   settings doctype.
2. payments.integrations.payrexx.api.create_web_transaction(payment_request_id)
   — the pivot that creates the Payment Intent and returns the hosted URL.

Run on a site where Payrexx is wired into the checkout::

    bench --site <site> execute payments.tests.payrexx_webshop_smoke.run

Creates a throwaway Quotation and stops at the hosted page URL — no payment is
completed, so no money moves.
"""

import frappe


def run():
    results = []

    def check(label, ok, detail=""):
        results.append(ok)
        print(f"{'  OK  ' if ok else ' FAIL '} {label}" + (f" — {detail}" if detail else ""))

    # A minimal submitted-less Quotation, like a webshop cart.
    customer = frappe.db.get_value("Customer", {}, "name")
    # A Shopping Cart quotation requires the item to have a Website Item, so pick
    # one that is actually published in the shop.
    item = frappe.db.get_value("Website Item", {"published": 1}, "item_code")
    company = frappe.db.get_value("Payment Gateway Account", {"payment_gateway": "Payrexx"}, "company")

    q = frappe.get_doc({
        "doctype": "Quotation", "quotation_to": "Customer", "party_name": customer,
        "company": company, "currency": "CHF", "order_type": "Shopping Cart",
        "items": [{"item_code": item, "qty": 1, "rate": 15.0}],
    })
    q.flags.ignore_permissions = True
    q.insert(ignore_permissions=True)
    frappe.db.commit()
    check("1. test Quotation created", bool(q.name), f"{q.name} total={q.grand_total}")

    # STEP 1 — our patched handler, gateway named directly.
    from webshop.controllers.payment_handler import create_payment_request
    step1 = create_payment_request(quotation_id=q.name, payment_gateway="Payrexx",
                                  idempotency_token=f"px-test-{q.name}")
    ok1 = isinstance(step1, dict) and step1.get("status") == "success"
    check("2. create_payment_request(payment_gateway='Payrexx')", ok1,
          str(step1.get("payment_request_id") if ok1 else step1)[:120])
    if not ok1:
        print(f"\n=== {sum(results)}/{len(results)} checks passed ===")
        return

    pr_id = step1["payment_request_id"]
    pr = frappe.get_doc("Payment Request", pr_id)
    check("3. Payment Request carries the Payrexx gateway", pr.payment_gateway == "Payrexx",
          f"gateway={pr.payment_gateway} total={pr.grand_total}")

    # STEP 2 — the Payrexx pivot.
    from payments.integrations.payrexx.api import create_web_transaction
    step2 = create_web_transaction(payment_request_id=pr_id)
    ok2 = step2.get("status") == "success"
    check("4. create_web_transaction returns a hosted URL", ok2,
          str(step2.get("redirect_url") or step2.get("message"))[:110])
    if ok2:
        check("5. URL is on the Payrexx instance subdomain",
              "payrexx.com" in (step2.get("redirect_url") or "") and "payment=" in step2["redirect_url"])
        pi = frappe.get_doc("Payment Intent", step2["payment_intent"])
        check("6. Payment Intent links back to the Payment Request",
              pi.reference_doctype == "Payment Request" and pi.reference_name == pr_id,
              f"{pi.name} status={pi.status}")
        check("7. Payment Intent is on the payrexx_web channel",
              pi.channel == "payrexx_web" and pi.provider == "payrexx",
              f"{pi.provider}/{pi.channel}")
        print(f"\n  REDIRECT_URL {step2['redirect_url']}")
        print(f"  PAYMENT_INTENT {pi.name}")

    print(f"\n=== {sum(results)}/{len(results)} checks passed ===")
