# //// Neoffice — added file (no upstream equivalent). The post-merge guard for the retired
# //// `webshopsi_integration` app, now folded into `webshop`: picking "Facture" must
# //// still render the installment selector, which proves template discovery at the
# //// new webshop path and that the relocated whitelisted method resolves over HTTP.
# //// This file is the documentation of where that feature went.
# //// Commits: b002600 2026-05-26 "Facture checkout renders + deterministic test item".
"""Facture (pay-by-invoice / installments) renders at checkout.

This is the post-merge guard for the former ``webshopsi_integration`` app,
now folded into ``webshop``. Selecting the "Facture" payment method triggers
``get_render_for_payment_method`` which renders
``webshop/templates/payments/webshopsi.html``; that template calls
``webshop.webshop.doctype.webshopsi_settings.webshopsi_settings.get_payment_context``
to populate the installment plan selector.

We assert the installment selector renders (proving template discovery at the
new webshop path + the relocated whitelisted method resolve over HTTP). We do
NOT place the order — that would submit a real Sales Invoice.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

from helpers import (
	add_to_cart,
	open_cart,
	proceed_to_checkout,
	complete_information_step,
	complete_shipping_step,
	close_modals,
)


@pytest.mark.checkout
def test_checkout_facture_renders_installments(logged_in_page, paying_item, base_url):
	page = logged_in_page

	add_to_cart(page, base_url, paying_item["route"])
	open_cart(page, base_url)
	proceed_to_checkout(page, base_url)
	complete_information_step(page)
	complete_shipping_step(page)

	# Select the Facture payment method card (data-method-id slug of
	# "Facture - CHF" → contains "facture").
	close_modals(page)
	card = page.locator(".payment-method-item[data-method-id*='facture' i]").first
	card.wait_for(state="visible", timeout=20_000)
	card.scroll_into_view_if_needed()
	card.click()

	# The webshopsi.html template renders async into the card; the installment
	# radios identify it. (proves the merged template + get_payment_context.)
	installments = page.locator("input[name='payment_installment']")
	expect(installments.first).to_be_visible(timeout=20_000)
	count = installments.count()
	assert count >= 2, f"Expected the 2 configured installment plans, got {count}"

	# The configured plan titles should appear in the rendered selector.
	body_text = page.locator(".installments-container, .payment-method-item[data-method-id*='facture' i]").first.inner_text()
	assert "Facture" in body_text, f"Installment plan titles not rendered: {body_text[:300]!r}"
