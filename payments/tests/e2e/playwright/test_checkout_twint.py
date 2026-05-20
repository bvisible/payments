"""Full checkout flow with TWINT — login → cart → checkout → overlay → simulate → SO."""

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
	proceed_to_checkout(page, base_url)

	# 4-step
	complete_information_step(page)
	complete_shipping_step(page)
	select_payment_method(page, "twint")
	click_pay(page)

	# Wait for the TWINT overlay to render (QR + pairing token).
	overlay = page.locator(".twint-dialog, .twint-modal-dialog")
	overlay.first.wait_for(state="visible", timeout=25_000)

	# Visual assert : QR + pairing token present.
	expect(page.locator(".qr-code svg, .qr-code img").first).to_be_visible(timeout=10_000)
	expect(page.locator(".pairing-token")).not_to_be_empty()

	# Recover the Payment Intent from the backend (most recent twint_web PI for
	# the test customer). More robust than hooking frappe.call client-side.
	intent_name = None
	for _ in range(10):
		intents = list_recent_test_intents(backend, minutes=5)
		twint = [i for i in intents if i.get("channel") == "twint_web"]
		if twint:
			intent_name = twint[0]["name"]
			break
		time.sleep(1)
	assert intent_name and intent_name.startswith("PI-"), f"No twint_web PI found: {intents if 'intents' in dir() else '?'}"

	# Simulate the consumer success via server API.
	res = trigger_twint_simulate(page, base_url, intent_name)
	assert res.get("ok"), f"simulate failed: {res}"

	# JS receives SocketIO event → redirects /thank_you.
	page.wait_for_url("**/thank_you**", timeout=20_000)

	# Backend assert
	page.wait_for_timeout(2_000)
	res = assert_payment_complete(backend, intent_name)
	assert res.get("ok"), f"TWINT payment chain not complete: {res}"
