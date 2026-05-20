"""Shared step helpers for the webshop checkout flow.

Each function is a building block reused by multiple tests. They take a
Playwright ``Page`` (assumed authenticated) and the ``base_url`` from
conftest.
"""

from __future__ import annotations

from typing import Any

from playwright.sync_api import Page, expect


# ---------------------------------------------------------------------------
# Cart helpers
# ---------------------------------------------------------------------------


def add_to_cart(page: Page, base_url: str, item_route: str) -> None:
	"""Navigate to a product page and click Ajouter au panier."""
	page.goto(f"{base_url}/{item_route}", wait_until="domcontentloaded")
	page.wait_for_selector(".btn-add-to-cart", state="visible", timeout=15_000)
	page.click(".btn-add-to-cart")
	# A toast / redirect happens — give it 2s, then verify cart counter.
	page.wait_for_timeout(2_000)


def open_cart(page: Page, base_url: str) -> None:
	page.goto(f"{base_url}/cart", wait_until="domcontentloaded")


def assert_cart_not_empty(page: Page) -> None:
	expect(page.locator("body")).not_to_contain_text("Votre panier est vide")


def proceed_to_checkout(page: Page) -> None:
	"""Cart → /checkout. Direct navigation is more reliable than CTA click
	(some themes hide the button in a slide-out drawer)."""
	base = page.url.split("/cart")[0] if "/cart" in page.url else page.url.rstrip("/")
	page.goto(f"{base}/checkout", wait_until="domcontentloaded", timeout=60_000)


# ---------------------------------------------------------------------------
# 4-step checkout helpers
# ---------------------------------------------------------------------------


def complete_information_step(page: Page) -> None:
	"""Step 1 (address). The fixture-pre-filled address is used directly."""
	# The form should be pre-filled from the Test E2E Webshop customer's
	# linked Address. If not (drift), the test fails fast with a clear
	# selector miss rather than typing fake data.
	expect(page.locator("#contact_first_name")).to_have_value("Test", timeout=10_000)
	# Click "Suivant" — handle the theme that uses `.next-step[data-next=...]`.
	next_btn = page.locator(".next-step[data-next='step-shipping']")
	if next_btn.count() == 0:
		next_btn = page.locator("button:has-text('Suivant'), button:has-text('Next')").first
	next_btn.click()
	# Wait until shipping section becomes active.
	page.wait_for_timeout(1_500)


def complete_shipping_step(page: Page) -> None:
	"""Step 2 (shipping). Skip if no methods, otherwise pick the first."""
	# If a radio is present, pick first; otherwise no-op (gift-card / no-shipping flow).
	radios = page.locator("input[name='shipping_method']")
	if radios.count() > 0:
		radios.first.check()
	next_btn = page.locator(".next-step[data-next='step-payment']")
	if next_btn.count() == 0:
		next_btn = page.locator("button:has-text('Suivant'), button:has-text('Next')").first
	next_btn.click()
	page.wait_for_timeout(1_500)


def select_payment_method(page: Page, method_substr: str) -> None:
	"""Step 4 — pick a payment method by data-method-id substring.

	The data-method-id is a slug of the Payment Gateway name (e.g.
	'stripe_settings', 'wallee_settings', 'twint_bridge_settings').
	"""
	card = page.locator(f".payment-method-item[data-method-id*='{method_substr}']").first
	card.scroll_into_view_if_needed()
	card.click()
	# Accept the CGV checkbox in the now-selected card.
	terms = card.locator("#terms-acceptance")
	if terms.count() > 0:
		terms.check()


def click_pay(page: Page) -> None:
	page.locator(".btn-submit-payment").first.click()


# ---------------------------------------------------------------------------
# PSP-specific helpers
# ---------------------------------------------------------------------------


def fill_stripe_card(page: Page, card: str = "4242 4242 4242 4242", exp: str = "12/30", cvc: str = "123") -> None:
	"""Fill the Stripe Elements iframe with a test card."""
	# Stripe.js mounts a single iframe to #card-element. Inputs inside are
	# named `cardnumber`, `exp-date`, `cvc`, `postal`.
	frame = page.frame_locator("iframe[name^='__privateStripeFrame']").first
	frame.locator("input[name='cardnumber']").fill(card)
	frame.locator("input[name='exp-date']").fill(exp)
	frame.locator("input[name='cvc']").fill(cvc)
	# Postal field may or may not be present depending on hidePostalCode option.
	postal = frame.locator("input[name='postal']")
	if postal.count() > 0:
		postal.fill("1003")


def fill_wallee_redirect_card(page: Page, card: str = "4242 4242 4242 4242", exp: str = "12/30", cvc: str = "123") -> None:
	"""On the Wallee hosted page, fill the card form + click Payer.

	Selectors are best-effort (Wallee theme can vary by space). Tests assert
	via the redirect-back to `/thank_you` rather than UI state on the Wallee
	side.
	"""
	page.wait_for_load_state("domcontentloaded")
	page.wait_for_timeout(2_000)
	# Card number — try several common labels.
	page.locator("input[name*='cardNumber' i], input[name*='card-number' i], input[id*='cardNumber' i]").first.fill(card)
	page.locator("input[name*='expirationDate' i], input[name*='exp' i], input[id*='expiration' i]").first.fill(exp)
	page.locator("input[name*='cvv' i], input[name*='cvc' i], input[id*='cvv' i]").first.fill(cvc)
	page.locator("button:has-text('Payer'), button[type='submit']:has-text('Pay')").first.click()


def capture_twint_intent(page: Page) -> str:
	"""Install a JS hook BEFORE clicking pay to capture the Payment Intent
	name. Returns the captured intent name.
	"""
	page.evaluate(
		"""
		window.__intent = null;
		const orig = window.frappe.call;
		window.frappe.call = function(opts){
			const cb = opts.callback;
			if (opts.method === 'payments.integrations.twint.api.create_web_transaction') {
				opts.callback = function(r){
					if (r.message && r.message.payment_intent) window.__intent = r.message.payment_intent;
					if (cb) cb(r);
				};
			}
			return orig.call(this, opts);
		};
		"""
	)
	return ""  # Caller polls page.evaluate("window.__intent") after click_pay.


def trigger_twint_simulate(page: Page, intent_name: str) -> dict:
	"""Trigger `simulate_consumer_success` server-side via frappe.call.

	The SocketIO event will reach the same page session.
	"""
	resp = page.request.post(
		"/api/method/payments.api.twint.simulate_consumer_success",
		form={"intent_name": intent_name},
	)
	assert resp.ok, f"simulate failed: {resp.status} {resp.text()[:200]}"
	return resp.json().get("message", {})
