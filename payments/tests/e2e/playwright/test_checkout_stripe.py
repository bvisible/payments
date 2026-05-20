"""Full checkout flow with Stripe — login → cart → checkout 4-step → 4242 → SO."""

from __future__ import annotations

import time

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
	fill_stripe_card,
)


@pytest.mark.checkout
@pytest.mark.psp_stripe
def test_checkout_stripe_4242(logged_in_page, paying_item, base_url, backend):
	page = logged_in_page

	# Cart
	add_to_cart(page, base_url, paying_item["route"])
	open_cart(page, base_url)
	proceed_to_checkout(page)

	# 4-step checkout
	complete_information_step(page)
	complete_shipping_step(page)
	select_payment_method(page, "stripe")
	fill_stripe_card(page)
	click_pay(page)

	# Stripe needs ~8s to tokenize + 3DS + redirect.
	page.wait_for_url("**/thank_you**", timeout=30_000)
	assert "/thank_you" in page.url
	assert "sales_order=" in page.url or "payment_intent=" in page.url

	# Backend assert via Frappe API
	# Wait briefly for the poll job to finalize, then look up the most recent PI
	page.wait_for_timeout(2_000)
	intents = list_recent_test_intents(backend, minutes=5)
	stripe_intents = [i for i in intents if i.get("channel") == "terminal" or "stripe" in (i.get("channel") or "")]
	assert stripe_intents, f"No Stripe PI created in last 5 min. intents={intents}"
	latest = stripe_intents[0]["name"]
	res = assert_payment_complete(backend, latest)
	assert res.get("ok"), f"Stripe payment chain not complete: {res}"
