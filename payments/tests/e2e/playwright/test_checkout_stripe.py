#//// Neoffice — added file (no upstream equivalent). Full checkout on Stripe: login → cart →
#//// the 4-step checkout → card 4242 → Sales Order. Commits: 187b5c8 2026-05-20.
"""Full checkout flow with Stripe — login → cart → checkout 4-step → 4242 → SO."""

from __future__ import annotations

import time

import pytest
from playwright.sync_api import expect

import time

from conftest import assert_recent_sales_order
from helpers import (
	add_to_cart,
	open_cart,
	proceed_to_checkout,
	complete_information_step,
	complete_shipping_step,
	select_payment_method,
	click_pay,
	fill_stripe_card,
	stripe_iframe_visible,
)


@pytest.mark.checkout
@pytest.mark.psp_stripe
def test_checkout_stripe_4242(logged_in_page, paying_item, base_url, backend):
	page = logged_in_page

	# Cart
	add_to_cart(page, base_url, paying_item["route"])
	open_cart(page, base_url)
	proceed_to_checkout(page, base_url)

	# 4-step checkout
	complete_information_step(page)
	complete_shipping_step(page)
	select_payment_method(page, "stripe")

	# Visual assert : the Stripe Elements iframe must be rendered + visible.
	assert stripe_iframe_visible(page), "Stripe Elements iframe not visible after selecting Stripe"

	fill_stripe_card(page)
	click_pay(page)

	# Stripe needs ~8s to tokenize + 3DS + redirect.
	page.wait_for_url("**/thank_you**", timeout=45_000)
	assert "/thank_you" in page.url

	# Backend assert — Stripe webshop uses the upstream make_payment which does
	# NOT create a unified Payment Intent, so we assert on the Sales Order.
	page.wait_for_timeout(2_000)
	res = assert_recent_sales_order(backend, minutes=5)
	assert res.get("ok"), f"Stripe checkout did not produce a submitted Sales Order: {res}"
