# //// Neoffice — added file (no upstream equivalent). Edit an address through the Web Form and
# //// assert it persisted. Commits: 187b5c8 2026-05-20; ff99e4b 2026-06-02 (it was
# //// not driving the Web Form at all).
"""Login → /addresses → edit address via Web Form → assert persisted."""

from __future__ import annotations

import urllib.parse as _url

import pytest
from playwright.sync_api import expect


@pytest.mark.smoke
def test_edit_address_persists(logged_in_page, backend, base_url, site_config):
	page = logged_in_page

	# 1. The buyer's address book lists addresses owned by the session user
	#    (frappe.contacts get_address_list filters owner == user). Assert our
	#    fixture address shows up — this also guards the fixture step that
	#    reassigns the Address owner to the test user.
	page.goto(f"{base_url}/addresses", wait_until="domcontentloaded", timeout=60_000)
	page.wait_for_timeout(2_000)
	addr_title = site_config["e2e_test_customer"] + "-Billing"
	expect(page.locator(f"a:has-text('{addr_title}')").first).to_be_visible(timeout=15_000)

	# 2. Resolve the real Address docname (autoname differs from the title, e.g.
	#    "...-Billing-Facturation"). The list link carries ?name=<docname>.
	href = page.locator(f"a:has-text('{addr_title}')").first.get_attribute("href") or ""
	addr_name = _url.unquote(href.split("name=", 1)[1]) if "name=" in href else None
	if not addr_name:
		addr_name = backend.call(
			"frappe.client.get_value", doctype="Address",
			filters={"address_title": addr_title}, fieldname="name",
		)
		addr_name = addr_name.get("name") if isinstance(addr_name, dict) else addr_name
	assert addr_name, f"Could not resolve Address docname for title {addr_title!r}"

	# 3. The bvisible theme's /addresses list is read-only (its link merely
	#    re-renders the list). Drive the standard Frappe Web Form (route
	#    `address`) directly. Its inputs carry data-fieldname.
	page.goto(
		f"{base_url}/address/{_url.quote(addr_name)}",
		wait_until="domcontentloaded",
		timeout=60_000,
	)
	page.wait_for_timeout(2_000)
	# The Web Form opens in read mode; click "modifier" (edit) to reveal inputs.
	page.click(".edit-button, button:has-text('modifier'), a:has-text('Edit')")
	page.wait_for_timeout(2_000)

	tag = f"e2e-edit-{int(__import__('time').time())}"
	line2 = page.locator(
		"[data-fieldname='address_line2'] input, input[data-fieldname='address_line2']"
	).first
	expect(line2).to_be_visible(timeout=15_000)
	line2.fill(tag)

	# 4. Submit the Web Form ("Valider" → .submit-btn). Avoid generic
	#    button[type=submit] so we don't hit the page's newsletter form.
	page.click(".submit-btn, button:has-text('Valider'), .web-form-actions button.btn-primary")
	page.wait_for_timeout(3_000)

	# 5. Verify via API that the change persisted (cheaper than re-rendering).
	addr = backend.call("frappe.client.get", doctype="Address", name=addr_name)
	assert tag in (addr.get("address_line2") or ""), (
		f"address_line2 did not update: got {addr.get('address_line2')!r}, expected to contain {tag!r}"
	)
