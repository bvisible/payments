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
