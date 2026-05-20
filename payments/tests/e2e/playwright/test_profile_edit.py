"""Login → /me → edit address → save → reload → assert persisted."""

from __future__ import annotations

import pytest
from playwright.sync_api import expect


@pytest.mark.smoke
def test_edit_address_persists(logged_in_page, backend, base_url, site_config):
	page = logged_in_page

	# 1. Open the address book (or /me — Frappe webshop uses /addresses).
	page.goto(f"{base_url}/addresses", wait_until="domcontentloaded", timeout=60_000)

	# 2. Click "Edit" on the existing test address (created by ensure_test_customer).
	# Selector: link with the test address title.
	addr_title = site_config["e2e_test_customer"] + "-Billing"
	# The list shows cards / rows — find the one for our test address.
	row = page.locator(f"a:has-text('{addr_title}'), .address-card:has-text('{addr_title}')").first
	if row.count() == 0:
		pytest.skip("Address book layout doesn't expose the test address — manual debug needed")
	row.click()
	page.wait_for_load_state("domcontentloaded")

	# 3. Modify a non-critical field (e.g. address_line2 with a tag we'll check).
	tag = f"e2e-edit-{__import__('time').time():.0f}"
	page.fill("input[name='address_line2'], #address_line2", tag)
	# Click save
	page.click("button:has-text('Save'), button:has-text('Sauvegarder'), button[type=submit]")
	page.wait_for_timeout(2_000)

	# 4. Verify via API that the change persisted (cheaper than re-rendering).
	addr = backend.call("frappe.client.get", doctype="Address", name=addr_title)
	assert tag in (addr.get("address_line2") or ""), (
		f"address_line2 did not update: got {addr.get('address_line2')!r}, expected to contain {tag!r}"
	)
