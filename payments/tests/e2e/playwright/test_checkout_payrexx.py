"""Webshop checkout with Payrexx — login → cart → checkout → redirect.

Payrexx was the only gateway on the shop that nothing watched: the tile, the
terms box and the button had been eyeballed, but no test would have caught them
breaking. This closes that, and deliberately covers the half a browser is needed
for — everything from picking the card to landing on the hosted payment page.

It stops at the redirect. What happens afterwards is already asserted end to end,
without a browser, by :mod:`payments.tests.payrexx_webshop_smoke`: the webhook
drives the intent to ``succeeded``, the Payment Request to ``Paid``, and creates
the Sales Order even when the shopper closed the tab. Paying on the hosted page
here would add a slow, flaky duplicate of a check that already exists — and would
move real money on a live account, since Payrexx documents no card sandbox for
this flow.
"""

from __future__ import annotations

import pytest

from conftest import list_recent_test_intents
from helpers import (
	add_to_cart,
	open_cart,
	proceed_to_checkout,
	complete_information_step,
	complete_shipping_step,
	select_payment_method,
	click_pay,
)


@pytest.mark.checkout
@pytest.mark.psp_payrexx
@pytest.mark.slow
def test_checkout_payrexx_redirects_to_hosted_page(logged_in_page, paying_item, base_url):
	page = logged_in_page

	# Cart → checkout
	add_to_cart(page, base_url, paying_item["route"])
	open_cart(page, base_url)
	proceed_to_checkout(page, base_url)

	# 4-step
	complete_information_step(page)
	complete_shipping_step(page)
	select_payment_method(page, "payrexx")
	click_pay(page)

	# The checkout created the gateway transaction and handed the shopper over.
	page.wait_for_url("**payrexx.com**", timeout=45_000)
	assert "payrexx.com" in page.url, f"Did not redirect to Payrexx: {page.url}"

	# On the instance's own subdomain, not some other merchant's page — this is
	# what catches a mis-resolved provider, which is the failure that would
	# otherwise reach a customer.
	assert "neoservice.payrexx.com" in page.url, (
		f"Redirected to Payrexx but not to our instance: {page.url}"
	)

	# And it is a real hosted payment, not an error page.
	assert "payment=" in page.url, f"No payment token in the hosted page URL: {page.url}"


@pytest.mark.checkout
@pytest.mark.psp_payrexx
@pytest.mark.slow
@pytest.mark.parametrize(
	("tuile", "attendu"),
	[("payrexx_twint", ["twint"]), ("payrexx_carte", ["visa", "mastercard"])],
)
def test_checkout_payrexx_restricted_tiles(
	logged_in_page, paying_item, base_url, backend, tuile, attendu
):
	"""One gateway, several tiles — each restricted to its own payment methods.

	A single "Payrexx" tile hands the shopper to a page where they still have to
	choose, which is a second decision after they thought they had decided. Naming
	a Payment Gateway Account after the same provider and restricting it in
	``Webshop Settings`` gives them a TWINT tile and a card tile that both settle
	through Payrexx.

	The restriction is asserted on the intent rather than on the hosted page: the
	page's URL is a token and reveals nothing, and reading Payrexx's own rendering
	would be testing their markup. What matters here is that the tile carried the
	restriction all the way into the payment request — the driver already logs
	loudly if Payrexx then drops it.
	"""
	import json

	page = logged_in_page

	add_to_cart(page, base_url, paying_item["route"])
	open_cart(page, base_url)
	proceed_to_checkout(page, base_url)

	complete_information_step(page)
	complete_shipping_step(page)
	select_payment_method(page, tuile)
	click_pay(page)

	page.wait_for_url("**payrexx.com**", timeout=45_000)

	# Restricting to a single method makes Payrexx skip its own chooser and
	# dispatch straight into that method — a TWINT tile lands on
	# dispatcher.payrexx.com/twint/, not on the instance's page. That is the point
	# of the feature: the shopper decides once, on our tile, instead of twice. So
	# the host is not pinned here; the contract asserted below is that the tile
	# carried its restriction, which is what produced the direct dispatch.
	assert "payrexx.com" in page.url, f"Did not reach Payrexx: {page.url}"

	intents = [
		i for i in list_recent_test_intents(backend, minutes=5)
		if i.get("channel") == "payrexx_web"
	]
	assert intents, "No payrexx_web intent recorded for this checkout"

	metadata = json.loads(intents[0].get("metadata_json") or "{}")
	assert metadata.get("payment_methods") == attendu, (
		f"Tile {tuile} did not carry its restriction: {metadata.get('payment_methods')}"
	)
