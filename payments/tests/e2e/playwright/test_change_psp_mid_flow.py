# //// Neoffice — added file (no upstream equivalent). Pick Stripe, switch to Wallee, assert no
# //// half-baked state is left behind — the failure mode a multi-PSP checkout invites
# //// and that upstream, with one gateway per page, cannot have.
# //// Commits: 187b5c8 2026-05-20; c2b14af 2026-06-02 (fixed assertions).
"""Select Stripe → switch to Wallee → assert no half-baked state."""

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
)


@pytest.mark.edge
def test_change_psp_mid_flow(logged_in_page, paying_item, base_url):
	page = logged_in_page

	add_to_cart(page, base_url, paying_item["route"])
	open_cart(page, base_url)
	proceed_to_checkout(page, base_url)

	complete_information_step(page)
	complete_shipping_step(page)

	# Select Stripe first. The data-method-id is a slug of the Payment Gateway
	# Account name (e.g. "Stripe___CHF"), so the attribute selectors MUST be
	# case-insensitive (`i` flag) — same as helpers.select_payment_method.
	select_payment_method(page, "stripe")
	expect(page.locator(".payment-method-item[data-method-id*='stripe' i].selected").first).to_be_visible()

	# Switch to Wallee
	select_payment_method(page, "wallee")
	expect(page.locator(".payment-method-item[data-method-id*='wallee' i].selected").first).to_be_visible()
	# Stripe must no longer be the selected method.
	expect(page.locator(".payment-method-item[data-method-id*='stripe' i].selected")).to_have_count(0)

	# Switch to TWINT
	select_payment_method(page, "twint")
	expect(page.locator(".payment-method-item[data-method-id*='twint' i].selected").first).to_be_visible()

	# Final selection is TWINT. Submit button enabled if terms checked (already done by select_payment_method).
	# We don't actually click pay here — just validate the state transitions cleanly.
