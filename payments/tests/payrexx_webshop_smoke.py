# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""Exercise the Payrexx checkout, from the button to the finalised order.

1. webshop.controllers.payment_handler.create_payment_request(payment_gateway='Payrexx')
   — our //// Neoffice patch: resolve the gateway by name rather than by its
   settings doctype.
2. payments.integrations.payrexx.api.create_web_transaction(payment_request_id)
   — the pivot that creates the Payment Intent and returns the hosted URL.

Run on a site where Payrexx is wired into the checkout::

    bench --site <site> execute payments.tests.payrexx_webshop_smoke.run

Then checks 8-10 cover the closed-tab case: a locally-injected ``confirmed``
delivery must produce the Sales Order on its own, and replaying the return page
afterwards must not produce a second one.

No money moves — nothing is ever paid on the Payrexx side; the hosted page is
created and left alone, and the confirmation is injected locally. Everything the
smoke creates is cancelled again on the way out, so it is safe to re-run on a live
site.
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

        _check_closed_tab(check, pi, pr_id, q.name)

    print(f"\n=== {sum(results)}/{len(results)} checks passed ===")


def _check_closed_tab(check, pi, pr_id, quotation):
    """Regression guard: the order must appear without the return page running.

    A shopper who closes the tab on the Payrexx page never triggers
    /payrexx/success. Before this was fixed, the webhook drove the intent to
    succeeded and stopped there — money in, Payment Request still Draft, no Sales
    Order, and nothing anywhere saying so. See
    Neoffice/Payments/Gotchas/04-Paid-Without-Order-Web-Checkout.

    The delivery is fed straight to ``process_event``, which is where finalisation
    now lives; the ingress signature check is a separate concern already covered by
    payrexx_smoke. Everything created here is cancelled again at the end, so the
    smoke can be re-run on a live site.
    """
    import json

    from payments.api.webhook_payrexx import process_event

    payload = json.dumps({"transaction": {
        "id": 1, "uuid": f"smoke-{pi.name}", "amount": pi.amount,
        "referenceId": pi.name, "status": "confirmed", "type": "E-Commerce",
        "mode": "TEST", "invoice": {"currency": pi.currency, "refundedAmount": 0},
    }})
    log = frappe.get_doc({
        "doctype": "Webhook Event Log", "event_id": f"payrexx_smoke-{pi.name}_confirmed",
        "provider": pi.provider, "event_type": "transaction.confirmed",
        "signature_valid": 1, "status": "Queued", "raw_payload": payload,
    }).insert(ignore_permissions=True)
    frappe.db.commit()

    process_event(log.name)

    pi.reload()
    check("8. webhook drove the intent to succeeded", pi.status == "succeeded", pi.status)

    pr = frappe.db.get_value("Payment Request", pr_id,
                             ["status", "reference_doctype", "reference_name"], as_dict=True)
    so_created = pr.reference_doctype == "Sales Order" and bool(pr.reference_name)
    check("9. the webhook alone created the Sales Order (closed-tab case)",
          so_created and pr.status in ("Paid", "Completed"),
          f"pr={pr.status} -> {pr.reference_doctype} {pr.reference_name}")

    # Both finalisation paths must be safe to run: the return page comes second here.
    before = frappe.db.count("Sales Order")
    try:
        from payments.www.payrexx.success import get_context

        frappe.form_dict = frappe._dict({"payment_intent": pi.name})
        try:
            get_context(frappe._dict())
        except frappe.Redirect:
            pass
        check("10. replaying the return page creates no second order",
              frappe.db.count("Sales Order") == before,
              f"count stayed at {before}")
    finally:
        frappe.local.flags.redirect_location = None

    _cancel_chain(pr.reference_name if so_created else None, quotation)


def _cancel_chain(so_name, quotation):
    """Undo what the smoke created, downstream first.

    Cancelled rather than deleted: a submitted Sales Invoice has already posted to
    the ledger, and docstatus 2 keeps both the audit trail and the reversing
    entries. Each document is fetched immediately before cancelling — cancelling a
    child rewrites its parent, and a stale copy raises TimestampMismatchError.
    """
    def cancel(doctype, name):
        if not name or frappe.db.get_value(doctype, name, "docstatus") != 1:
            return
        doc = frappe.get_doc(doctype, name)
        doc.flags.ignore_permissions = True
        doc.cancel()
        frappe.db.commit()
        print(f"  cleanup: cancelled {doctype} {name}")

    if so_name:
        for si in {r.parent for r in frappe.get_all(
                "Sales Invoice Item", filters={"sales_order": so_name, "docstatus": 1},
                fields=["parent"])}:
            for pe in {r.parent for r in frappe.get_all(
                    "Payment Entry Reference",
                    filters={"reference_doctype": "Sales Invoice", "reference_name": si,
                             "docstatus": 1}, fields=["parent"])}:
                cancel("Payment Entry", pe)
            cancel("Sales Invoice", si)
        cancel("Payment Request", frappe.db.get_value(
            "Payment Request",
            {"reference_doctype": "Sales Order", "reference_name": so_name}, "name"))
        cancel("Sales Order", so_name)
    cancel("Quotation", quotation)
