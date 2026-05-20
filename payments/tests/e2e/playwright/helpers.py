"""Shared step helpers for the webshop checkout flow.

Each function is a building block reused by multiple tests. They take a
Playwright ``Page`` (assumed authenticated) and the ``base_url``.

Theme notes (osiris — "Le Tapissier" / Builder.io theme):
- The 4-step checkout is single-page; steps are ``.step-section`` divs that
  toggle an ``active`` / ``hidden`` class. ``checkout_manager`` (global JS
  object) drives the transitions.
- ``.next-step[data-next='step-X']`` buttons trigger the move; the handler is
  bound via jQuery delegation on ``document`` and can be flaky right after a
  page settle — :func:`_advance_step` retries + waits for the target section
  to gain ``.active``.
- A promo / newsletter modal sometimes overlays the page — :func:`close_modals`
  dismisses it.
"""

from __future__ import annotations

from playwright.sync_api import Page, expect


# ---------------------------------------------------------------------------
# Modal / readiness helpers
# ---------------------------------------------------------------------------


def close_modals(page: Page) -> None:
	"""Dismiss any Bootstrap-style modal blocking the page."""
	page.evaluate(
		"""
		document.querySelectorAll('.modal.show, .modal.fade.show').forEach(m => {
			const close = m.querySelector('button.close, button[data-dismiss=modal], button.btn-close, .modal-close');
			if (close) close.click();
			else { m.style.display='none'; m.classList.remove('show'); }
		});
		document.querySelectorAll('.modal-backdrop').forEach(b => b.remove());
		document.body.classList.remove('modal-open');
		document.body.style.overflow = '';
		document.body.style.paddingRight = '';
		"""
	)
	page.wait_for_timeout(300)


def wait_checkout_ready(page: Page) -> None:
	"""Wait until the checkout JS manager is initialised.

	Osiris is asset-heavy — ``checkout_manager`` (set after async init) can lag.
	Retry with a reload once before giving up.
	"""
	for attempt in range(3):
		try:
			page.wait_for_function("!!window.checkout_manager", timeout=60_000)
			page.wait_for_timeout(1_000)  # let the jQuery .next-step delegation bind
			return
		except Exception:
			if attempt == 2:
				raise
			page.reload(wait_until="domcontentloaded", timeout=90_000)


# ---------------------------------------------------------------------------
# Cart helpers
# ---------------------------------------------------------------------------


def add_to_cart(page: Page, base_url: str, item_route: str) -> None:
	"""Navigate to a product page and click Ajouter au panier."""
	page.goto(f"{base_url}/{item_route}", wait_until="domcontentloaded", timeout=60_000)
	btn = page.locator(".btn-add-to-cart").first
	expect(btn).to_be_enabled(timeout=20_000)
	btn.click()
	page.wait_for_timeout(3_000)


def open_cart(page: Page, base_url: str) -> None:
	page.goto(f"{base_url}/cart", wait_until="domcontentloaded", timeout=60_000)


def assert_cart_not_empty(page: Page) -> None:
	expect(page.locator("body")).not_to_contain_text("Votre panier est vide")


def proceed_to_checkout(page: Page, base_url: str) -> None:
	"""Cart → /checkout. Direct navigation (drawer CTA is unreliable)."""
	page.goto(f"{base_url}/checkout", wait_until="domcontentloaded", timeout=60_000)
	wait_checkout_ready(page)


# ---------------------------------------------------------------------------
# 4-step checkout — core transition helper
# ---------------------------------------------------------------------------


def _advance_step(page: Page, data_next: str, target_section: str, retries: int = 4) -> None:
	"""Click ``.next-step[data-next=...]`` and wait for ``target_section`` to gain
	``.active``. Retries because the jQuery delegation is flaky right after a
	settle (the handler may not have processed the first event).
	"""
	for attempt in range(retries):
		close_modals(page)
		btn = page.locator(f"button.next-step[data-next='{data_next}']")
		if btn.count() == 0:
			raise AssertionError(f"No .next-step[data-next='{data_next}'] button found")
		# Use force=False but the element should be visible/enabled.
		try:
			btn.first.click(timeout=8_000)
		except Exception:
			# Fall back to a JS-dispatched click if the native one is intercepted.
			page.evaluate(
				f"document.querySelector('.next-step[data-next=\"{data_next}\"]')"
				f".dispatchEvent(new MouseEvent('click', {{bubbles:true, cancelable:true}}))"
			)
		# Wait up to 8s for the transition.
		try:
			page.wait_for_function(
				f"document.getElementById({target_section!r})?.classList.contains('active')",
				timeout=8_000,
			)
			return  # success
		except Exception:
			# Surface any blocking msgprint so the caller sees why.
			alerts = page.locator(".modal.show .modal-body, .frappe-alert").all_text_contents()
			if alerts:
				close_modals(page)
			if attempt == retries - 1:
				raise AssertionError(
					f"Step did not advance to {target_section!r} after {retries} tries. "
					f"Alerts seen: {alerts}"
				)
			page.wait_for_timeout(1_000)


def complete_information_step(page: Page) -> None:
	"""Step 1 (address). The fixture-pre-filled address is used directly."""
	expect(page.locator("#contact_first_name")).to_have_value("Test", timeout=15_000)
	_advance_step(page, "step-shipping", "step-shipping")


def complete_shipping_step(page: Page) -> None:
	"""Step 2 (shipping). Select the first shipping method then advance.

	If the checkout skipped straight to step-payment (gift-card-only path),
	this is a no-op.
	"""
	if page.evaluate("document.getElementById('step-payment')?.classList.contains('active')"):
		return  # already on payment (gift-card-only flow)

	# Select the first shipping method radio — required by the JS validation.
	# The radio is CSS-hidden (custom-control styling), so .check() refuses.
	# Set it checked via JS + dispatch the change event the handler listens to.
	radios = page.locator("input[name='shipping_method']")
	radios.first.wait_for(state="attached", timeout=10_000)
	if radios.count() > 0:
		page.evaluate(
			"""
			const r = document.querySelector("input[name='shipping_method']");
			if (r) {
				r.checked = true;
				r.dispatchEvent(new Event('change', {bubbles:true}));
				r.dispatchEvent(new Event('click', {bubbles:true}));
			}
			"""
		)
		page.wait_for_timeout(1_000)
	_advance_step(page, "step-payment", "step-payment")


def select_payment_method(page: Page, method_substr: str) -> None:
	"""Step 4 — pick a payment method card by data-method-id substring.

	Case-insensitive (``i`` flag) — the data-method-id is a slug of the
	Payment Gateway Account name, e.g. ``Stripe___CHF`` / ``Wallee___CHF___pri``
	/ ``Twint___CHF``.
	"""
	close_modals(page)
	# Payment method cards load async after step-payment becomes active.
	card = page.locator(f".payment-method-item[data-method-id*='{method_substr}' i]").first
	card.wait_for(state="visible", timeout=20_000)
	card.scroll_into_view_if_needed()
	card.click()
	page.wait_for_timeout(1_000)

	# Accept the CGV terms. The checkbox (#terms-acceptance) is CSS-hidden
	# (custom-control styling) — the webshop handler listens on the
	# .form-check-label click and toggles the checkbox itself. So we click
	# the LABEL inside the selected card, then wait for the submit button to
	# become enabled. Retry because the handler binds late on async cards.
	submit = card.locator(".btn-submit-payment").first
	for attempt in range(5):
		try:
			if submit.is_enabled():
				return
		except Exception:
			pass
		label = card.locator(".form-check-label, label[for='terms-acceptance']").first
		if label.count() > 0:
			try:
				label.scroll_into_view_if_needed()
				label.click(force=True)
			except Exception:
				pass
		else:
			# Fallback : toggle the checkbox directly via JS within this card.
			page.evaluate(
				"""
				(methodSubstr) => {
					const card = [...document.querySelectorAll('.payment-method-item')]
						.find(c => (c.dataset.methodId||'').toLowerCase().includes(methodSubstr.toLowerCase()));
					if (!card) return;
					const cb = card.querySelector('#terms-acceptance, input[id*=terms]');
					if (cb) { cb.checked = true; cb.dispatchEvent(new Event('change', {bubbles:true})); }
				}
				""",
				method_substr,
			)
		page.wait_for_timeout(900)
	# Final state — caller's click_pay surfaces a clear error if still disabled.


def selected_card(page: Page):
	"""Return the currently-selected payment method card locator."""
	return page.locator(".payment-method-item.selected").first


def click_pay(page: Page) -> None:
	"""Click the submit button INSIDE the selected payment method card."""
	card = selected_card(page)
	btn = card.locator(".btn-submit-payment").first
	btn.scroll_into_view_if_needed()
	expect(btn).to_be_enabled(timeout=15_000)
	btn.click()


# ---------------------------------------------------------------------------
# PSP-specific helpers
# ---------------------------------------------------------------------------


def fill_stripe_card(page: Page, card: str = "4242 4242 4242 4242", exp: str = "12 / 30", cvc: str = "123") -> None:
	"""Fill the Stripe Elements iframe with a test card."""
	frame = page.frame_locator("iframe[name^='__privateStripeFrame']").first
	frame.locator("input[name='cardnumber']").fill(card)
	frame.locator("input[name='exp-date']").fill(exp)
	frame.locator("input[name='cvc']").fill(cvc)
	postal = frame.locator("input[name='postal']")
	if postal.count() > 0:
		postal.fill("1003")


def stripe_iframe_visible(page: Page) -> bool:
	"""True if the Stripe Elements iframe is rendered + visible."""
	frames = page.locator("iframe[name^='__privateStripeFrame']")
	return frames.count() > 0 and frames.first.is_visible()


def fill_wallee_redirect_card(
	page: Page,
	card: str = "4242424242424242",
	exp_month: str = "12",
	exp_year: str = "30",
	cvc: str = "123",
) -> None:
	"""On the Wallee hosted (sandbox) page, pay with a test card.

	Wallee's sandbox page shows a "Simulation" panel with ready-to-use test
	cards behind ``Use`` buttons — clicking the first one auto-fills a valid
	card. This is far more robust than typing into the split MM/YY fields
	(which auto-tab and re-render). Falls back to manual entry if no Use
	button is present (production page).
	"""
	page.wait_for_load_state("domcontentloaded")
	page.wait_for_timeout(3_000)

	# 1. Expand the "Simulation" panel (collapsed by default). Its header
	#    button text contains "Simulation … you can use the test information".
	sim = page.locator("button", has_text="Simulation").first
	if sim.count() > 0:
		try:
			sim.click()
			page.wait_for_timeout(1_500)
		except Exception:
			pass

	# 2. Click an EXACT "Use" button (get_by_role exact avoids matching the
	#    Simulation header which also contains the substring "use").
	use_btn = page.get_by_role("button", name="Use", exact=True).first
	if use_btn.count() > 0:
		use_btn.click()
		page.wait_for_timeout(2_500)
	else:
		# Production fallback — type into the card fields directly.
		page.locator("input[name='ccnumber']").first.fill(card)
		page.locator("input[id$='expiryDate-month']").first.fill(exp_month)
		page.locator("input[id$='expiryDate-year']").first.fill(exp_year)
		page.locator("input[id$='cardVerificationCode-input']").first.fill(cvc)

	# 3. Submit.
	pay = page.locator("button[type='submit']", has_text="Pay").first
	pay.scroll_into_view_if_needed()
	pay.click()


def read_twint_intent_from_overlay(page: Page) -> str | None:
	"""Try to read the Payment Intent name from the rendered TWINT overlay.

	The overlay JS (`payments/public/js/twint_dialog.js`) subscribes to
	``payment.intent.<X>.updated`` — the event name leaks the intent id. We
	scan the page for it. Returns None if not found (caller falls back to the
	backend ``list_recent_test_intents``).
	"""
	return page.evaluate(
		"""
		() => {
			// The overlay stores the intent on a data attribute or in the
			// pairing-token container's closest dialog.
			const dlg = document.querySelector('.twint-dialog, .twint-modal-dialog');
			if (dlg && dlg.dataset && dlg.dataset.paymentIntent) return dlg.dataset.paymentIntent;
			// Fallback: scan realtime subscriptions if frappe exposes them.
			try {
				const subs = window.frappe?.socketio?.open_tasks || {};
			} catch (e) {}
			return null;
		}
		"""
	)


def trigger_twint_simulate(page: Page, base_url: str, intent_name: str) -> dict:
	"""Trigger ``simulate_consumer_success`` via the page session (authenticated)."""
	resp = page.context.request.post(
		f"{base_url}/api/method/payments.api.twint.simulate_consumer_success",
		form={"intent_name": intent_name},
	)
	assert resp.ok, f"simulate failed: {resp.status} {resp.text()[:200]}"
	return resp.json().get("message", {})
