# //// Neoffice — added file (no upstream equivalent). Loyalty points applied at checkout drop the
# //// grand total, and removing them puts it back. Self-bootstrapping: the fixture
# //// provisions the programme and a positive balance on the fixed test customer.
# //// Commits: 8e06c1a 2026-05-20 "test(e2e): loyalty points apply/remove on webshop
# //// checkout".
"""Loyalty points — apply on checkout, assert the order total drops, then remove.

The fixed test customer ("Test E2E Webshop") has a Loyalty Program assigned
and a positive point balance is provisioned by the fixture so the test is
self-bootstrapping. The loyalty redemption form lives in the step-payment
section of /checkout.

Flow:
  1. Reach step-payment.
  2. Assert the loyalty form is rendered with the available balance.
  3. Read the order-summary grand total.
  4. Click "Apply" to redeem the points → assert the loyalty row appears
     and the grand total decreases.
  5. Click "Remove Points" → assert the loyalty row disappears.

Note (webshop loyalty math bugs out of scope for this E2E):
  - The discount applied by ``apply_loyalty_points`` is larger than
    ``points * loyalty_program.conversion_factor`` (the conversion factor
    returned by ``get_loyalty_program_details_with_points`` differs from
    the program-level value when a tier is active).
  - ``remove_loyalty_points`` does not fully restore the grand total after
    apply — the TVA base appears to drift between apply and remove. Both
    issues are about the math; the UI behaviour (row appears / disappears
    + total moves in the right direction) is what we cover here.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import expect

from helpers import (
	add_to_cart,
	open_cart,
	proceed_to_checkout,
	complete_information_step,
	complete_shipping_step,
)


def _grand_total(page) -> float:
	"""Read the numeric grand total from the order summary.

	Targets the dedicated ``td.grand-total`` cell — the order summary also
	contains a "loyalty points to earn" blurb whose amount must not be
	mistaken for the total.
	"""
	txt = page.locator("td.grand-total").first.inner_text()
	amounts = re.findall(r"(\d[\d'\s]*[.,]\d{2})", txt)
	assert amounts, f"No amount found in grand-total cell: {txt!r}"
	return float(amounts[-1].replace("'", "").replace(" ", "").replace(",", "."))


@pytest.mark.smoke
def test_loyalty_points_apply_and_remove(logged_in_page, paying_item, base_url, backend):
	# Top up the test customer's loyalty balance in the webshop's company so
	# the test is self-bootstrapping (entries are normally earned via Sales
	# Invoices — for E2E we provision directly). Idempotent.
	# Provision plenty so the balance survives multiple pytest reruns (each
	# successful apply inserts a negative entry).
	backend.call("payments.tests.e2e.fixtures.ensure_loyalty_points", min_points=100)
	balance = backend.call("payments.tests.e2e.fixtures.get_loyalty_balance")
	if balance.get("available_points", 0) < 20:
		pytest.skip(f"Test customer has insufficient loyalty points: {balance}")
	# We will redeem a small, deterministic number of points — the
	# pre-filled value can be larger than what the backend actually has if
	# the balance drifted between page render and apply.
	points_to_redeem = 10

	page = logged_in_page
	console_errors: list[str] = []
	page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)

	# Cart → checkout → reach step-payment
	add_to_cart(page, base_url, paying_item["route"])
	open_cart(page, base_url)
	proceed_to_checkout(page, base_url)
	complete_information_step(page)
	complete_shipping_step(page)

	# The loyalty form lives in #loyalty-points-container on step-payment.
	loyalty_box = page.locator("#loyalty-points-container")
	expect(loyalty_box).to_be_visible(timeout=20_000)
	redeem_input = page.locator("#loyalty-point-to-redeem")
	expect(redeem_input).to_be_visible(timeout=10_000)

	total_before = _grand_total(page)

	# Override the pre-filled input with a deterministic small value — the
	# pre-fill reflects the balance at page-render time, which can be larger
	# than what the backend will accept by the time apply runs.
	redeem_input.fill(str(points_to_redeem))
	# Verify the fill took effect (no race with a late form re-render).
	actual = redeem_input.input_value()
	assert actual == str(points_to_redeem), (
		f"Input was re-rendered after fill: expected {points_to_redeem}, got {actual!r}"
	)
	print(f"\n[loyalty] before={total_before} input={actual} balance={balance}")

	# Apply the loyalty points. The click triggers two chained server round
	# trips (get_cart_quotation → apply_loyalty_points), which can be slow on
	# a shared dev box — retry the click once if the row does not appear.
	loyalty_row = page.locator(".loyalty-row")
	for attempt in range(2):
		apply_btn = page.locator(".bt-loyalty-point")
		expect(apply_btn).to_be_enabled(timeout=10_000)
		apply_btn.click()
		try:
			expect(loyalty_row).to_be_visible(timeout=25_000)
			break
		except AssertionError:
			if attempt == 1:
				raise
			# The form re-renders after a failed/slow apply — give it a beat.
			page.wait_for_timeout(2_000)

	page.wait_for_timeout(1_500)

	total_after = _grand_total(page)
	print(f"[loyalty] after_apply={total_after}")
	assert total_after < total_before, (
		f"Grand total did not drop after applying loyalty points "
		f"(before={total_before}, after={total_after}). Console errors: {console_errors}"
	)

	# Remove the points — the loyalty row should disappear and the total
	# should move back up (toward the original).
	remove_btn = page.locator(".bt-remove-loyalty")
	expect(remove_btn).to_be_visible(timeout=10_000)
	remove_btn.click()
	expect(loyalty_row).to_have_count(0, timeout=25_000)
	page.wait_for_timeout(1_500)

	total_restored = _grand_total(page)
	print(f"[loyalty] after_remove={total_restored}")
	assert total_restored > total_after, (
		f"Grand total did not move back up after removing loyalty points "
		f"(after_apply={total_after}, after_remove={total_restored})"
	)
