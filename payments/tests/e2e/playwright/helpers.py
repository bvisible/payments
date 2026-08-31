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


def accept_terms(page: Page, card) -> None:  # noqa: ANN001
	"""Tick the CGV box inside one payment method card.

	Two things make this less obvious than it looks.

	The checkbox used to be ``#terms-acceptance`` on every gateway template —
	a duplicated id, so ``document`` lookups and ``label[for]`` both resolved to
	the *first* card in the page rather than this one. That was fixed in the
	webshop by giving each one ``id="terms-<submit_id>"`` and a shared
	``.terms-acceptance`` class; targeting the class, scoped to the card, is what
	survives both spellings.

	And the label is not safe to click blindly: it wraps an ``<a class="terms-link">``
	that jumps to the terms section. A click at the label's centre lands on that
	link, scrolls the page and leaves the box untouched — a silent no-op that
	looks exactly like a flaky test.

	So: ``check()`` the input directly, which Playwright routes through the
	proper label/checkbox association, and fall back to the DOM only if the
	element is not actionable (some themes hide it under custom-control styling).
	Idempotent — a box already ticked is left alone, since clicking a label is a
	toggle and a second click would untick it.
	"""
	box = card.locator("input.terms-acceptance, input[id^='terms-'], input[type=checkbox]").first
	if box.count() == 0:
		return
	try:
		if box.is_checked():
			return
		box.check(timeout=3_000)
		return
	except Exception:
		pass

	# Not actionable (hidden by CSS). Tick it in the DOM and fire the events the
	# gateway templates listen on, so the submit button unlocks.
	box.evaluate(
		"""
		(cb) => {
			if (cb.checked) return;
			cb.checked = true;
			cb.dispatchEvent(new Event('change', {bubbles: true}));
			cb.dispatchEvent(new Event('click', {bubbles: true}));
		}
		"""
	)


def select_payment_method(page: Page, method_substr: str) -> None:
	"""Step 4 — pick a payment method card by data-method-id substring.

	Case-insensitive (``i`` flag) — the data-method-id is a slug of the
	Payment Gateway Account name, e.g. ``Stripe___CHF`` / ``Wallee___CHF___pri``
	/ ``Twint___CHF`` / ``Payrexx___CHF``.
	"""
	close_modals(page)
	# Payment method cards load async after step-payment becomes active.
	card = page.locator(f".payment-method-item[data-method-id*='{method_substr}' i]").first
	card.wait_for(state="visible", timeout=20_000)
	card.scroll_into_view_if_needed()
	card.click()
	page.wait_for_timeout(1_000)

	# The submit button stays disabled until the terms are accepted. Ticking is
	# idempotent (see accept_terms), so the loop only waits — it never re-clicks
	# a box that is already ticked, which is what used to flip it back off and
	# leave the button disabled on a coin toss.
	submit = card.locator(".btn-submit-payment")
	form = card.locator(".payment-method-form").first
	for _ in range(8):
		try:
			if submit.count() > 0 and submit.first.is_enabled():
				return
			# A method switched to the intent engine draws itself — TWINT shows its
			# QR and pairing token as soon as it is picked, with no button and
			# nothing to accept. Once its form has content and still no submit
			# exists, there is nothing left to wait for.
			if submit.count() == 0 and form.count() > 0:
				if page.evaluate(
					"(el) => el.innerHTML.trim().length > 0", form.element_handle()
				):
					return
		except Exception:
			pass
		accept_terms(page, card)
		page.wait_for_timeout(800)
	# Final state — caller's click_pay surfaces a clear error if still disabled.


def selected_card(page: Page):
	"""Return the currently-selected payment method card locator."""
	return page.locator(".payment-method-item.selected").first


def click_pay(page: Page) -> None:
	"""Go ahead with the payment, from inside the selected card.

	Two shapes, because a method switched to the intent engine is drawn by the
	engine rather than by its own template:

	- legacy — a ``<button class="btn-submit-payment">``, disabled until the terms
	  are accepted;
	- intent engine — an ``<a class="btn btn-primary">`` reading "Continue to
	  payment", already carrying the link the driver produced.

	Waiting on ``.btn-submit-payment`` alone is why the intent-engine tiles timed
	out on an element that was never going to appear.
	"""
	card = selected_card(page)

	submit = card.locator(".btn-submit-payment")
	if submit.count() > 0:
		btn = submit.first
		btn.scroll_into_view_if_needed()
		expect(btn).to_be_enabled(timeout=15_000)
		btn.click()
		return

	link = card.locator(".payment-method-form a.btn, .payment-method-form button.btn").first
	expect(link).to_be_visible(timeout=15_000)
	link.scroll_into_view_if_needed()
	link.click()


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


# ---------------------------------------------------------------------------
# Signup
# ---------------------------------------------------------------------------


def signup_ephemeral_user(page: Page, base_url: str, email: str, fullname: str) -> None:
	"""Drive the webshop signup form to create a fresh Website User.

	Lands on /login, opens the signup pane, submits fullname + email, and
	waits for Frappe's "account created" feedback. The caller still has to
	set a password via ``frappe.client.set_value`` (Frappe sends a welcome
	email instead of asking for one inline).
	"""
	if "/login" not in page.url:
		page.goto(f"{base_url}/login", wait_until="domcontentloaded", timeout=60_000)

	# Open the signup pane — Frappe's login page toggles to it via this link.
	signup_link = page.locator(
		"a:has-text(\"S'inscrire\"), a:has-text('Sign up')"
	).first
	signup_link.wait_for(state="visible", timeout=15_000)
	signup_link.click()
	page.wait_for_timeout(1_000)

	fullname_input = page.locator("#signup_fullname")
	fullname_input.wait_for(state="visible", timeout=15_000)
	fullname_input.fill(fullname)
	page.locator("#signup_email").fill(email)

	page.click(
		"button:has-text('Inscription'), button:has-text('Sign up'), "
		"button:has-text(\"S'inscrire\")"
	)
	# Frappe shows a flash + the page reloads / redirects — give it a beat.
	page.wait_for_timeout(3_000)
