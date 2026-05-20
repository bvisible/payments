"""Smoke test — validates the Playwright + Frappe login chain works end-to-end."""

import pytest
from playwright.sync_api import expect


@pytest.mark.smoke
def test_login_test_user(logged_in_page, site_config, base_url):
	"""Login as Test E2E Webshop and assert session is alive."""
	page = logged_in_page

	# Navigate to a page that requires authentication; if guest, the page
	# would redirect to /login.
	page.goto(f"{base_url}/me", wait_until="domcontentloaded")

	# /me should display the user's full name
	expect(page.locator("body")).to_contain_text("Test E2E", timeout=10_000)


@pytest.mark.smoke
def test_paying_item_resolvable(paying_item):
	"""The fixtures helper returns a paying Website Item."""
	assert paying_item.get("item_code")
	assert paying_item.get("route")
	assert float(paying_item.get("price", 0)) > 0


@pytest.mark.smoke
def test_test_customer_created(ensure_customer):
	"""Backend fixture runs without error."""
	# ensure_customer fixture is session-scoped and runs at session start.
	# This test is mostly a noop but documents the dependency.
	pass
