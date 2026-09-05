# //// Neoffice — added file (no upstream equivalent). Checkout on Payrexx up to the hosted page.
# //// Payrexx was the only gateway on the shop nothing watched — the tile, the terms
# //// box and the button had only ever been eyeballed. Stops at the redirect on
# //// purpose: what follows is asserted without a browser by
# //// `payments.tests.payrexx_webshop_smoke`.
# //// Commits: 5babaea 2026-08-30 "cover Payrexx, and stop the checkout helpers
# //// testing the wrong things"; aba52be 2026-08-31 (one gateway serving several
# //// restricted tiles); f59d601 2026-08-31 (the card tile keeps the shopper on the
# //// shop); c2fb4c4 + 32b1a0e 2026-09-01 (the action is veiled, not locked, before
# //// the terms are accepted).
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
from playwright.sync_api import expect

from conftest import list_recent_test_intents
from helpers import (
	add_to_cart,
	open_cart,
	proceed_to_checkout,
	complete_information_step,
	complete_shipping_step,
	select_payment_method,
	selected_card,
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
	("tile", "expected"),
	[("payrexx_twint", ["twint"])],
)
def test_checkout_payrexx_restricted_tiles(
	logged_in_page, paying_item, base_url, backend, tile, expected
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
	select_payment_method(page, tile)
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
	assert metadata.get("payment_methods") == expected, (
		f"Tile {tile} did not carry its restriction: {metadata.get('payment_methods')}"
	)


@pytest.mark.checkout
@pytest.mark.psp_payrexx
@pytest.mark.parametrize("tile", ["payrexx_twint"])
def test_intent_engine_hides_the_action_until_terms_accepted(
	logged_in_page, paying_item, base_url, tile
):
	"""No terms, no payment — on the intent engine as everywhere else.

	A method switched to the intent engine is drawn by the engine, not by its
	template, and the template was the only thing that carried the terms checkbox.
	So on that path the shopper could pay having accepted nothing, while every
	other tile made it impossible.

	Covers the tiles whose action WE own — a link or a button. The card tile is
	deliberately absent: its payment happens inside Payrexx's frame, so there is
	nothing of ours to gate and it carries a mention instead of a checkbox
	(test_card_tile_states_the_terms_instead_of_gating_them). `Twint - CHF` is
	absent too — it was reverted to its own template on 01.09.2026 (see
	test_checkout_twint), so it carries its own checkbox and never reaches the
	engine.

	The action is shown but veiled, not hidden. Hiding it meant clicking a tile
	and seeing nothing but a checkbox, with no idea what was coming — where every
	other tile shows its form and greys only the button. A payment frame cannot be
	merely greyed, since it would still work, so it sits under a veil that says
	why: visible, explained, and inert.
	"""
	page = logged_in_page

	add_to_cart(page, base_url, paying_item["route"])
	open_cart(page, base_url)
	proceed_to_checkout(page, base_url)

	complete_information_step(page)
	complete_shipping_step(page)
	select_payment_method(page, tile, accept=False)

	card = selected_card(page)
	terms = card.locator("input.terms-acceptance").first
	veil = card.locator(".intent-veil").first

	assert terms.count() > 0, "The intent engine drew no terms checkbox"
	assert not terms.is_checked(), "The terms started out already accepted"
	assert veil.is_visible(), "The payment action was reachable without accepting"

	# And ticking lifts it — otherwise the assertion above would pass on a card
	# that simply never finished loading.
	terms.check()
	expect(veil).to_be_hidden(timeout=10_000)


@pytest.mark.checkout
@pytest.mark.psp_payrexx
def test_card_tile_renders_the_payment_page_inside_the_checkout(
	logged_in_page, paying_item, base_url, backend
):
	"""Card entry is a form, so it stays on the shop.

	Sending someone away to fill in a card number costs the thread of what they
	were doing: they land on a foreign page, come back to a return screen, and
	wonder whether their order still exists. Payrexx's hosted page frames without
	complaint — no X-Frame-Options, no frame-ancestors — and its card fields
	render from our origin, so the tile shows it in place.

	Not for every method: TWINT hands over to the phone and cannot do that from
	inside a frame, which is why its tile keeps the link and this is a per-tile
	setting rather than a rule.
	"""
	import json

	page = logged_in_page

	add_to_cart(page, base_url, paying_item["route"])
	open_cart(page, base_url)
	proceed_to_checkout(page, base_url)

	complete_information_step(page)
	complete_shipping_step(page)
	select_payment_method(page, "payrexx_carte")

	card = selected_card(page)
	frame_el = card.locator(".payment-method-form iframe").first
	expect(frame_el).to_be_visible(timeout=20_000)

	src = frame_el.get_attribute("src") or ""
	assert "payrexx.com" in src, f"The frame does not point at Payrexx: {src}"

	# The shopper stays on the shop — no navigation happened.
	assert "/checkout" in page.url, f"The shopper was sent away after all: {page.url}"

	# And the card fields are actually reachable inside it, which is the whole
	# point: a frame that renders an error page would satisfy everything above.
	#
	# The card widget is a frame *inside* Payrexx's own page, so it appears a
	# beat after the outer one becomes visible. Waiting for it explicitly beats
	# reading too early and calling it a failure.
	champs = None
	for _ in range(20):
		for frame in page.frames:
			if "checkout.payrexx.com" in frame.url:
				try:
					champs = frame.evaluate(
						"""() => [...document.querySelectorAll('input')]
						     .map(e => e.placeholder || e.name).filter(Boolean)"""
					)
				except Exception:
					champs = None
		if champs:
			break
		page.wait_for_timeout(1_000)
	assert champs, "The Payrexx card frame never loaded"
	assert any("CVC" in c for c in champs), f"No card fields in the frame: {champs}"

	# The restriction still travelled, as for any other tile.
	intents = [
		i for i in list_recent_test_intents(backend, minutes=5)
		if i.get("channel") == "payrexx_web"
	]
	assert intents, "No payrexx_web intent recorded"
	metadata = json.loads(intents[0].get("metadata_json") or "{}")
	assert metadata.get("payment_methods") == ["visa", "mastercard"], (
		f"Restriction lost: {metadata.get('payment_methods')}"
	)


@pytest.mark.checkout
@pytest.mark.psp_payrexx
def test_card_tile_states_the_terms_instead_of_gating_them(
	logged_in_page, paying_item, base_url
):
	"""A payment frame gets a mention, not a checkbox.

	A tick gates something we own — our button, our link. Inside the frame the
	shopper pays at Payrexx, on their fields, with their button, so there is
	nothing of ours left to gate; what a checkbox bought here was a veil hiding
	the card fields behind a request to accept before seeing what for.

	Two things must hold, and they fail differently: the frame is immediately
	usable (no checkbox, no veil), and the terms are still SAID — dropping the
	gate must not drop the sentence.
	"""
	page = logged_in_page

	add_to_cart(page, base_url, paying_item["route"])
	open_cart(page, base_url)
	proceed_to_checkout(page, base_url)

	complete_information_step(page)
	complete_shipping_step(page)
	select_payment_method(page, "payrexx_carte", accept=False)

	card = selected_card(page)
	expect(card.locator(".payment-method-form iframe").first).to_be_visible(timeout=25_000)

	assert card.locator("input.terms-acceptance").count() == 0, (
		"The card tile still draws a checkbox"
	)
	assert card.locator(".intent-veil").count() == 0, (
		"The card tile still veils its payment frame"
	)

	# The sentence stays, and it names the merchant's own terms record rather
	# than a wording invented in JS — the two used to disagree on one page.
	mention = card.locator(".intent-mention").first
	expect(mention).to_be_visible(timeout=10_000)
	expected_label = page.evaluate("window.webshop_terms_label")
	assert expected_label, "The shop never seeded a terms label"
	expect(mention).to_contain_text(expected_label, timeout=5_000)
