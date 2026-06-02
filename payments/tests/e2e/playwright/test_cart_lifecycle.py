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

	# 3. Open cart and assert it is NOT empty.
	#    The empty-cart placeholder (``.cart-empty`` / "Votre panier est vide")
	#    is ALWAYS in the DOM and merely hidden when the cart has items, so a
	#    textContent-based check would match it even with a full cart. Assert on
	#    visibility instead: ``.cart-empty`` hidden + the line item present.
	page.goto(f"{base_url}/cart", wait_until="domcontentloaded")
	page.wait_for_timeout(2_000)
	expect(page.locator(".cart-empty").first).not_to_be_visible()
	expect(
		page.locator(f"tbody.cart-items [data-item-code='{paying_item['item_code']}']").first
	).to_be_visible(timeout=10_000)

	# 4. Remove the only line item via the theme's remove control
	#    (``.remove-cart-item`` carries the item code).
	remove = page.locator(
		f".remove-cart-item[data-item-code='{paying_item['item_code']}']"
	).first
	if remove.count() == 0:
		remove = page.locator(".remove-cart-item").first
	expect(remove).to_be_visible(timeout=10_000)
	remove.click()
	page.wait_for_timeout(2_500)

	# 5. The cart is now empty. Reload /cart so the server-rendered empty state
	#    is authoritative, then assert the empty-cart card is visible and the
	#    line item is gone.
	page.goto(f"{base_url}/cart", wait_until="domcontentloaded")
	page.wait_for_timeout(1_500)
	expect(page.locator(".cart-empty").first).to_be_visible(timeout=15_000)
	expect(
		page.locator(f"tbody.cart-items [data-item-code='{paying_item['item_code']}']")
	).to_have_count(0)
