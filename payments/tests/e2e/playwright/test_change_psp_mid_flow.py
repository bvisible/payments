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
	proceed_to_checkout(page)

	complete_information_step(page)
	complete_shipping_step(page)

	# Select Stripe first
	select_payment_method(page, "stripe")
	expect(page.locator(".payment-method-item[data-method-id*='stripe'].selected")).to_be_visible()

	# Switch to Wallee
	select_payment_method(page, "wallee")
	expect(page.locator(".payment-method-item[data-method-id*='wallee'].selected")).to_be_visible()
	# Stripe form should be hidden now
	stripe_form = page.locator("[id^='payment-form-stripe']")
	if stripe_form.count() > 0:
		expect(stripe_form.first).to_be_hidden()

	# Switch to TWINT
	select_payment_method(page, "twint")
	expect(page.locator(".payment-method-item[data-method-id*='twint'].selected")).to_be_visible()

	# Final selection is TWINT. Submit button enabled if terms checked (already done by select_payment_method).
	# We don't actually click pay here — just validate the state transitions cleanly.
