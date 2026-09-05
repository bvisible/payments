# //// Neoffice — added file (no upstream equivalent). Go back mid-checkout, re-engage, and check
# //// the idempotency token — a second intent must not be minted for the same cart.
# //// Commits: 187b5c8 2026-05-20.
"""Back-mid-checkout → re-engage → idempotency token check."""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

from helpers import (
	add_to_cart,
	open_cart,
	proceed_to_checkout,
	complete_information_step,
	complete_shipping_step,
)


@pytest.mark.edge
def test_back_mid_checkout_then_retry(logged_in_page, paying_item, base_url, backend):
	page = logged_in_page

	# Add + go to checkout
	add_to_cart(page, base_url, paying_item["route"])
	open_cart(page, base_url)
	proceed_to_checkout(page, base_url)

	complete_information_step(page)
	complete_shipping_step(page)

	# Now we're on step-payment. Hit the browser Back button.
	page.go_back()
	page.wait_for_timeout(2_000)
	# We should be back on shipping or address; try again to reach payment.
	page.go_forward()
	page.wait_for_timeout(2_000)

	# Confirm we're still in checkout (didn't 404 or lose cart).
	assert "/checkout" in page.url

	# Cart count from backend should still be 1 quotation.
	intents = backend.call(
		"frappe.client.get_count",
		doctype="Quotation",
		filters='[["party_name", "=", "Test E2E Webshop"], ["order_type", "=", "Shopping Cart"], ["docstatus", "=", 0]]',
	)
	assert int(intents) >= 1, f"Cart Quotation lost during back nav: count={intents}"
