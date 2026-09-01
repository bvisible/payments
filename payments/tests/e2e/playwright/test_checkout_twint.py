"""Checkout with TWINT — login → cart → overlay dialog → simulate → SO.

TWINT is deliberately NOT on the intent engine. Flipping it there was tried and
reverted on 01.09.2026: the engine draws a tile it owns, which reduced TWINT to a
bare QR pasted into the payment card, and dropped the dialog the shopper used to
get — amount, pairing code, countdown, the three "what to do on your phone"
steps, and the poller that redirects on confirmation. That dialog lives in
``payments/public/js/twint_dialog.js`` and is reached through the app's own
``twint.html`` template, i.e. only while ``use_payment_intent`` is unchecked for
``Twint - CHF`` in Webshop Settings.

So this test asserts the overlay, and by doing so it guards the revert: put TWINT
back on the intent engine and this test fails on the missing dialog rather than
letting the regression reach a shop.
"""

from __future__ import annotations

import re
import time

import pytest
from playwright.sync_api import expect

from conftest import assert_payment_complete, list_recent_test_intents
from helpers import (
	add_to_cart,
	click_pay,
	open_cart,
	proceed_to_checkout,
	complete_information_step,
	complete_shipping_step,
	select_payment_method,
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

	# Match the record name, not the bare word: "twint" also matches the Payrexx
	# TWINT tile, which is a different provider on a different path.
	select_payment_method(page, "Twint___CHF")
	click_pay(page)

	# The dialog the shopper actually gets — an overlay, not an inline QR.
	overlay = page.locator(".twint-dialog-overlay").first
	expect(overlay).to_be_visible(timeout=25_000)

	# The QR to scan, and the numeric code for whoever types it instead.
	expect(overlay.locator("canvas, svg, img").first).to_be_visible(timeout=20_000)
	expect(overlay).to_contain_text(re.compile(r"\d{4,}"), timeout=10_000)

	# It says it is waiting rather than looking finished.
	expect(overlay).to_contain_text(
		re.compile(r"En attente|Waiting", re.I), timeout=10_000
	)

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

	# Finalise through the dialog's own dev button rather than by calling the API
	# directly: it exercises the wiring — the button, the whitelisted endpoint and
	# the poller that turns a confirmation into the redirect.
	sim = overlay.locator(".twint-simulate-success-btn").first
	expect(sim).to_be_visible(timeout=10_000)
	sim.click()

	# The status poller picks up the success and redirects.
	page.wait_for_url("**/thank_you**", timeout=30_000)

	# Backend assert
	page.wait_for_timeout(2_000)
	res = assert_payment_complete(backend, intent_name)
	assert res.get("ok"), f"TWINT payment chain not complete: {res}"
