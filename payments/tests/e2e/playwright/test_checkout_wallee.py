"""Full checkout flow with Wallee — login → cart → checkout → redirect Wallee → 4242 → SO."""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

from conftest import assert_payment_complete, list_recent_test_intents
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
def test_checkout_wallee_redirect_4242(logged_in_page, paying_item, base_url, backend):
	page = logged_in_page

	# Cart
	add_to_cart(page, base_url, paying_item["route"])
	open_cart(page, base_url)
	proceed_to_checkout(page)

	# 4-step
	complete_information_step(page)
	complete_shipping_step(page)
	select_payment_method(page, "wallee")
	click_pay(page)

	# Wait for redirect to Wallee
	page.wait_for_url("**app-wallee.com**", timeout=20_000)

	# Pay on the hosted page
	fill_wallee_redirect_card(page)

	# Wait for redirect back to /thank_you
	page.wait_for_url("**/thank_you**", timeout=45_000)
	assert "/thank_you" in page.url

	# Backend assert
	page.wait_for_timeout(2_000)
	intents = list_recent_test_intents(backend, minutes=5)
	wallee_intents = [i for i in intents if i.get("channel") == "wallee_web"]
	assert wallee_intents, f"No Wallee PI created. intents={intents}"
	latest = wallee_intents[0]["name"]
	res = assert_payment_complete(backend, latest)
	assert res.get("ok"), f"Wallee payment chain not complete: {res}"
