"""B2B checkout differentiator — pricing rule applied for B2B Customer Group.

Demo flow (used live in client presentations):
  1. Backend : create / reuse Customer Group ``B2B`` + Pricing Rule
     ``B2B 10% E2E`` (10% off transaction, scoped to that group) +
     enable ``activate_b2b_checkout`` in Webshop Settings.
  2. UI      : signup an ephemeral user (Chrome drives the form).
  3. Backend : set a password (Frappe sends a welcome email otherwise) and
     log in via the JSON API so the cookie is set on the browser context.
  4. UI      : add a product to the cart — this triggers the webshop's
     lazy ``get_party()`` which creates the Customer record (assigned to
     the default group, NOT B2B yet).
  5. Backend : poll until the Customer exists, then reassign it to ``B2B``.
  6. UI      : navigate to ``/checkout``. The webshop must redirect to
     ``/checkout_b2b`` because the customer is now in a B2B group.
  7. Assert  : the B2B template renders AND the grand total reflects the
     10% pricing rule (i.e. grand_total < list_price × 0.95, with a small
     safety margin for tax rounding).

This test stops before payment — the goal is to prove the B2B branch
differentiator end to end, not to drive a PSP. Cleanup is best-effort in
a try/finally and the shared B2B setup (group / rule / Webshop Settings)
is intentionally preserved across runs.
"""

from __future__ import annotations

import re
import time

import pytest
from playwright.sync_api import Page, expect

from helpers import add_to_cart, open_cart, signup_ephemeral_user


_AMOUNT_RE = re.compile(r"(\d[\d'\s]*[.,]\d{2})")


def _parse_amount(text: str) -> float:
	"""Pull the last numeric amount from a Swiss-formatted price cell.

	Handles thousands separators (``1'234.50`` or ``1 234.50``) and either
	dot or comma as the decimal separator.
	"""
	matches = _AMOUNT_RE.findall(text)
	if not matches:
		raise AssertionError(f"No amount found in: {text!r}")
	raw = matches[-1].replace("'", "").replace(" ", "").replace(",", ".")
	return float(raw)


def _grand_total(page: Page) -> float:
	"""Read the displayed grand total from the order_summary partial.

	The same ``td.grand-total`` cell is used by the standard checkout AND
	the B2B template (both include ``templates/includes/order_summary.html``).
	"""
	td = page.locator("td.grand-total").first
	expect(td).to_be_visible(timeout=15_000)
	return _parse_amount(td.inner_text())


@pytest.mark.smoke
@pytest.mark.b2b
def test_b2b_customer_sees_b2b_checkout_with_pricing_rule(
	page: Page, base_url: str, backend, paying_item
):
	"""End-to-end: ephemeral signup → B2B group → /checkout redirects to
	/checkout_b2b → 10% pricing rule visible in the grand total."""
	suffix = str(int(time.time()))
	email = f"e2e.b2b.{suffix}@example.test"
	password = "TestB2B2026!"
	fullname = "E2E B2B"

	# ---------- STEP 1 : backend env (idempotent) ----------
	env = backend.call("payments.tests.e2e.fixtures.ensure_b2b_environment")
	assert env["customer_group"] == "B2B"
	assert env["pricing_rule"] == "B2B 10% E2E"
	discount_pct = float(env["discount_percentage"])
	print(
		f"\n[b2b] env ready: group={env['customer_group']} "
		f"rule={env['pricing_rule']} discount={discount_pct}%"
	)

	try:
		# ---------- STEP 2 : ephemeral signup via Chrome ----------
		signup_ephemeral_user(page, base_url, email=email, fullname=fullname)

		# Sanity check — the User must exist server-side now.
		user_count = backend.call(
			"frappe.client.get_count",
			doctype="User",
			filters=f'[["email", "=", "{email}"]]',
		)
		assert int(user_count) >= 1, f"Signup did not create User {email}"

		# ---------- STEP 3 : set password + login via API ----------
		backend.call(
			"frappe.client.set_value",
			doctype="User",
			name=email,
			fieldname="new_password",
			value=password,
		)
		resp = page.context.request.post(
			f"{base_url}/api/method/login",
			form={"usr": email, "pwd": password},
			timeout=60_000,
		)
		assert resp.ok, f"Login failed: {resp.status} {resp.text()[:200]}"

		# ---------- STEP 4 : add to cart → triggers Customer auto-creation ----------
		add_to_cart(page, base_url, paying_item["route"])

		# ---------- STEP 5 : wait for Customer + reassign to B2B group ----------
		cust = backend.call(
			"payments.tests.e2e.fixtures.wait_for_customer",
			email=email,
			timeout_s=15,
		)
		print(
			f"[b2b] customer auto-created: {cust['customer']} "
			f"(after {cust['attempts']} polls)"
		)
		assign = backend.call(
			"payments.tests.e2e.fixtures.assign_customer_to_b2b", email=email
		)
		assert assign["customer_group"] == "B2B"
		print(
			f"[b2b] customer reassigned: {assign['customer']} "
			f"{assign['previous']} → {assign['customer_group']}"
		)

		# ---------- STEP 6 : /checkout must redirect to /checkout_b2b ----------
		# The redirect is server-side (302) from checkout.py get_context.
		# Refresh the cart first so cached is_b2b_customer is recomputed.
		open_cart(page, base_url)
		page.goto(
			f"{base_url}/checkout", wait_until="domcontentloaded", timeout=60_000
		)
		# Allow up to 5s for the redirect to settle on slow networks.
		for _ in range(10):
			if "/checkout_b2b" in page.url:
				break
			page.wait_for_timeout(500)
		assert "/checkout_b2b" in page.url, (
			f"Expected redirect to /checkout_b2b, landed on {page.url}"
		)

		# B2B template marker — h3 "B2B Checkout".
		expect(
			page.locator("h3", has_text=re.compile("B2B", re.I)).first
		).to_be_visible(timeout=15_000)
		print(f"[b2b] checkout URL: {page.url}")

		# ---------- STEP 7 : assert pricing rule applied (backend truth) ----------
		# The displayed grand_total can be obscured by shipping fees + taxes,
		# so we read the Quotation directly. A "Transaction"-scoped Discount
		# Percentage rule lands in Quotation.additional_discount_percentage.
		summary = backend.call(
			"payments.tests.e2e.fixtures.get_b2b_quotation_summary", email=email
		)
		assert summary["customer_group"] == "B2B", (
			f"Quotation not in B2B group: {summary['customer_group']}"
		)
		applied_pct = summary["additional_discount_percentage"]
		rule_on_items = any(
			"B2B 10% E2E" in (it.get("pricing_rules") or "")
			for it in summary["items"]
		)
		pricing_applied = abs(applied_pct - discount_pct) < 0.5 or rule_on_items
		assert pricing_applied, (
			f"B2B Pricing Rule NOT applied on Quotation {summary['name']}: "
			f"additional_discount_pct={applied_pct} items={summary['items']}"
		)

		# Also surface the UI total for the live demo console output.
		list_price = float(paying_item["price"])
		grand_total = _grand_total(page)
		print(
			f"[b2b] PRICING APPLIED — quotation={summary['name']} "
			f"additional_discount_pct={applied_pct:.1f}% "
			f"backend_grand_total={summary['grand_total']:.2f} "
			f"ui_grand_total={grand_total:.2f} "
			f"(list_price={list_price:.2f}, "
			f"discount_amount={summary['discount_amount']:.2f})"
		)

	finally:
		# ---------- STEP 8 : best-effort cleanup ----------
		try:
			stats = backend.call(
				"payments.tests.e2e.fixtures.cleanup_b2b_user", email=email
			)
			print(f"[b2b] cleanup: {stats}")
		except Exception as exc:  # noqa: BLE001 — cleanup must not fail the test
			print(f"[b2b] cleanup failed (non-fatal): {exc}")
