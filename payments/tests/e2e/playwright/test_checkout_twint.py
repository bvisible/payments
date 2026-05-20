"""Full checkout flow with TWINT — login → cart → checkout → overlay → simulate → SO."""

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
	capture_twint_intent,
	trigger_twint_simulate,
)


@pytest.mark.checkout
@pytest.mark.psp_twint
def test_checkout_twint_with_simulate(logged_in_page, paying_item, base_url, backend, site_config):
	assert site_config.get("enable_e2e_simulators"), "enable_e2e_simulators must be True in site_config"

	page = logged_in_page

	# Cart
	add_to_cart(page, base_url, paying_item["route"])
	open_cart(page, base_url)
	proceed_to_checkout(page)

	# 4-step
	complete_information_step(page)
	complete_shipping_step(page)

	# Install the JS hook BEFORE clicking pay so we can capture intent_name.
	capture_twint_intent(page)

	select_payment_method(page, "twint")
	click_pay(page)

	# Wait for the TWINT overlay to render with a captured intent.
	page.wait_for_function("window.__intent !== null", timeout=15_000)
	intent_name = page.evaluate("window.__intent")
	assert intent_name and intent_name.startswith("PI-"), f"intent_name not captured: {intent_name}"

	# Simulate the consumer success via server API.
	res = trigger_twint_simulate(page, intent_name)
	assert res.get("ok"), f"simulate failed: {res}"

	# JS receives SocketIO event → redirects /thank_you.
	page.wait_for_url("**/thank_you**", timeout=15_000)

	# Backend assert
	page.wait_for_timeout(2_000)
	res = assert_payment_complete(backend, intent_name)
	assert res.get("ok"), f"TWINT payment chain not complete: {res}"
