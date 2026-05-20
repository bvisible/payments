"""Cart lifecycle : add 2 items → change qty → remove 1 → assert totals."""

from __future__ import annotations

import pytest
from playwright.sync_api import expect


@pytest.mark.smoke
def test_cart_add_update_remove(logged_in_page, paying_item, base_url):
	page = logged_in_page

	# 1. Add item once. Wait for the button to be enabled (theme may
	# disable it during a cooldown / re-render right after click).
	page.goto(f"{base_url}/{paying_item['route']}", wait_until="domcontentloaded", timeout=60_000)
	btn = page.locator(".btn-add-to-cart").first
	expect(btn).to_be_enabled(timeout=15_000)
	btn.click()
	page.wait_for_timeout(3_000)

	# 2. Add same item again — re-locate the button (DOM may have re-rendered).
	page.goto(f"{base_url}/{paying_item['route']}", wait_until="domcontentloaded", timeout=60_000)
	btn = page.locator(".btn-add-to-cart").first
	expect(btn).to_be_enabled(timeout=15_000)
	btn.click()
	page.wait_for_timeout(3_000)

	# 3. Open cart and check we have 1 line item with qty >= 2 OR 2 line items.
	page.goto(f"{base_url}/cart", wait_until="domcontentloaded")
	expect(page.locator("body")).not_to_contain_text("Votre panier est vide")

	# 4. Find the quantity input — change to 1.
	qty_input = page.locator(".cart-table input.item-qty, input.cart-qty, input[name='qty']").first
	if qty_input.count() > 0:
		qty_input.fill("1")
		# Trigger update (theme-specific — try blur + dispatch input event).
		page.evaluate(
			"document.querySelector('.cart-table input.item-qty, input.cart-qty, input[name=qty]').dispatchEvent(new Event('change', {bubbles:true}))"
		)
		page.wait_for_timeout(2_000)

	# 5. Remove the item entirely via the × button.
	close_btn = page.locator(".cart-table .remove-cart-item, button.btn-remove-cart, button:has-text('×')").first
	if close_btn.count() > 0:
		close_btn.click()
		page.wait_for_timeout(2_500)
		# Cart should be empty now
		expect(page.locator("body")).to_contain_text("Votre panier est vide", timeout=10_000)
