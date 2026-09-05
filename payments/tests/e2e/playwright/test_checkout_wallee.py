# //// Neoffice — added file (no upstream equivalent). Checkout on Wallee up to the hosted page
# //// and back. Deliberately stops before the Sales Order: that half is finalised by
# //// the server-side poller `payments.drivers.wallee.terminal_driver.
# //// poll_pending_transactions`, not by the browser.
# //// Commits: 187b5c8 2026-05-20; 23a7fc1 2026-05-20 "server-side reconciliation
# //// finalizes the webshop Sales Order".
"""Webshop checkout with Wallee — login → cart → checkout → redirect → pay.

What this test asserts (deterministic, fast):
  1. The checkout creates a Wallee transaction and redirects to the hosted
     payment page (app-wallee.com).
  2. The card payment goes through on Wallee's side.
  3. Wallee redirects the buyer back to the shop.

The post-payment Sales Order finalisation (PI → succeeded → PR Paid → SO)
is driven by the server-side poller
``payments.drivers.wallee.terminal_driver.poll_pending_transactions`` (runs
every minute, also self-heals orphans). That convergence is verified by the
backend smoke + the poller's own logic — it is intentionally NOT asserted
here because the Wallee sandbox can take 1-3 min to settle a transaction and
that wait would make this UI test slow + flaky on a shared dev box.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

from helpers import (
	add_to_cart,
	open_cart,
	proceed_to_checkout,
	complete_information_step,
	complete_shipping_step,
	select_payment_method,
	click_pay,
	fill_wallee_redirect_card,
)


@pytest.mark.checkout
@pytest.mark.psp_wallee
@pytest.mark.slow
def test_checkout_wallee_redirect_and_pay(logged_in_page, paying_item, base_url):
	page = logged_in_page

	# Cart → checkout
	add_to_cart(page, base_url, paying_item["route"])
	open_cart(page, base_url)
	proceed_to_checkout(page, base_url)

	# 4-step
	complete_information_step(page)
	complete_shipping_step(page)
	select_payment_method(page, "wallee")
	click_pay(page)

	# (1) Checkout created the Wallee transaction + redirected to the hosted page.
	page.wait_for_url("**app-wallee.com**", timeout=30_000)
	assert "app-wallee.com" in page.url, f"Did not redirect to Wallee: {page.url}"

	# (2) Pay on the Wallee hosted sandbox page.
	fill_wallee_redirect_card(page)

	# (3) Wallee redirects the buyer back to the shop (either /wallee/success
	#     polling page or straight to /thank_you).
	page.wait_for_url("**osiris.neoffice.me/**", timeout=60_000)
	assert "osiris.neoffice.me" in page.url, f"Did not return to the shop: {page.url}"
	assert any(seg in page.url for seg in ("/wallee/success", "/thank_you")), (
		f"Unexpected return URL: {page.url}"
	)
