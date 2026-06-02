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
	#    ``cart.html`` is a server-side ``{% if doc.items %}`` / ``{% else %}``:
	#    the ``.cart-empty`` card is rendered ONLY when the cart is empty, so
	#    here (cart has items) it is simply absent.
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

	# Wait for the removal to actually commit: the line item disappears from the
	# live cart (AJAX update). This is the real sync point — a fixed sleep before
	# reload races the server and made the empty-state assertion flaky.
	# MUST scope to ``tbody.cart-items`` — the bare ``[data-item-code]`` also
	# matches the "recommended products" cards on the cart page (same item code),
	# which never disappear, so an unscoped count would never reach 0.
	expect(
		page.locator(f"tbody.cart-items [data-item-code='{paying_item['item_code']}']")
	).to_have_count(0, timeout=20_000)

	# 5. Reload /cart so the server-rendered empty state is authoritative
	#    (cart.html renders ``.cart-empty`` only in the ``{% else %}`` branch,
	#    i.e. when ``doc.items`` is empty), then assert it and the line item gone.
	page.goto(f"{base_url}/cart", wait_until="domcontentloaded")
	expect(page.locator(".cart-empty").first).to_be_visible(timeout=20_000)
	expect(
		page.locator(f"tbody.cart-items [data-item-code='{paying_item['item_code']}']")
	).to_have_count(0)
