"""Checkout with TWINT — login → cart → QR shown on selection → simulate → SO.

TWINT has no pay button, and that is by design rather than an omission: it is
one of the methods switched to the intent engine, so picking the card *is* the
request. The QR and the pairing token are drawn into the card straight away and
the shop then waits for the consumer to confirm in their app.

This test used to click a submit button and wait for a modal overlay
(``.twint-dialog`` / ``.qr-code`` / ``.pairing-token``). None of those exist on
this flow — the markup is inline in the payment method card — so it timed out on
an element that was never coming.
"""

from __future__ import annotations

import re
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
	selected_card,
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

	# 4-step — selecting TWINT is what starts the payment.
	complete_information_step(page)
	complete_shipping_step(page)
	select_payment_method(page, "twint")

	card = selected_card(page)
	form = card.locator(".payment-method-form").first

	# The QR the customer scans, drawn inline as an SVG.
	expect(form.locator("svg").first).to_be_visible(timeout=25_000)

	# The pairing token, for a customer who types it instead of scanning.
	expect(form).to_contain_text(re.compile(r"\d{4,}"), timeout=10_000)

	# And the shop says it is waiting, rather than looking finished.
	expect(form.locator(".intent-attente").first).to_be_visible(timeout=10_000)

	# Recover the Payment Intent from the backend (most recent twint_web PI for
	# the test customer). More robust than hooking frappe.call client-side.
	intent_name = None
	intents = []
	for _ in range(10):
		intents = list_recent_test_intents(backend, minutes=5)
		twint = [i for i in intents if i.get("channel") == "twint_web"]
		if twint:
			intent_name = twint[0]["name"]
			break
		time.sleep(1)
	assert intent_name and intent_name.startswith("PI-"), f"No twint_web PI found: {intents}"

	# Simulate the consumer success via server API.
	res = trigger_twint_simulate(page, base_url, intent_name)
	assert res.get("ok"), f"simulate failed: {res}"

	# JS receives SocketIO event → redirects /thank_you.
	page.wait_for_url("**/thank_you**", timeout=20_000)

	# Backend assert
	page.wait_for_timeout(2_000)
	res = assert_payment_complete(backend, intent_name)
	assert res.get("ok"), f"TWINT payment chain not complete: {res}"
