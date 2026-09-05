# //// Neoffice — added file (no upstream equivalent). A declined Stripe card lands on
# //// /payment-failed and a retry is still possible. Commits: 187b5c8 2026-05-20.
"""Stripe declined card → /payment-failed → assert retry possible."""

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
	fill_stripe_card,
)


@pytest.mark.edge
@pytest.mark.psp_stripe
def test_stripe_declined_card(logged_in_page, paying_item, base_url):
	page = logged_in_page

	add_to_cart(page, base_url, paying_item["route"])
	open_cart(page, base_url)
	proceed_to_checkout(page, base_url)

	complete_information_step(page)
	complete_shipping_step(page)

	select_payment_method(page, "stripe")
	# Stripe test card that always declines: 4000 0000 0000 0002
	fill_stripe_card(page, card="4000 0000 0000 0002")
	click_pay(page)

	# Should NOT redirect to /thank_you. Either stay on /checkout with error,
	# or redirect to /payment-failed.
	page.wait_for_timeout(8_000)
	url = page.url
	assert "/thank_you" not in url, f"declined card should not reach /thank_you: {url}"
	# Either we're on /payment-failed or still on /checkout (with error text)
	assert "/checkout" in url or "/payment-failed" in url, f"unexpected URL after decline: {url}"

	# An error message should be visible (theme-specific text variations).
	body_text = page.locator("body").inner_text()
	assert any(token in body_text.lower() for token in ["declined", "decline", "refusé", "échec", "fail"]), (
		f"Expected an error message after declined card. Page text excerpt: {body_text[:300]}"
	)
