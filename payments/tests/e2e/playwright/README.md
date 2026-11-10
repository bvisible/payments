<!-- //// Neoffice — added file (no upstream equivalent). How to run the Playwright suite: on osiris,
     //// against the test clone (never prod.local, #950), the pytest markers (smoke / checkout / psp_*), the
     //// API-key env vars, and the site it targets. Upstream has no browser test at all.
     //// Commits: 187b5c8 2026-05-20 "test(e2e): add Playwright Python suite with 9
     //// webshop scenarios". -->
# Webshop E2E — Playwright Python

## Quick start

The suite runs **on osiris, against the test clone `prodclone.local`**, never against `prod.local` or any live
site: it creates and cancels orders, invoices and payments, and the runs that targeted osiris's `prod.local` left
117 validated test invoices in its ledger (maintenance #950). `conftest.py` refuses any `--base-url` whose host is
`prod.local` or not a `*.local` test site, before a single test runs.

```bash
# on osiris, once per clone rebuild (see "The clone" below)
cd ~/frappe-bench/apps/payments/payments/tests/e2e/playwright
pytest -m smoke                     # smoke only
pytest                              # all tests, headless
pytest -m psp_stripe                # Stripe only
pytest -k "signup or cart"          # by name
```

## Architecture

- **`conftest.py`** : pytest fixtures
  - `backend` : HTTP client of the target site's whitelisted methods (Administrator API key)
  - `reset_state` : `reset_test_env` before each test (cancels and keeps submitted documents, deletes drafts)
  - `logged_in_page` : authenticated Playwright `Page` ready to use
  - `paying_item` : Website Item code with positive price

- **`helpers.py`** : reusable building blocks
  - `login_via_api(page, email, password)` : sets the sid cookie correctly
  - `complete_address_step(page)` : pre-filled address form → next
  - `complete_shipping_step(page)` : select first shipping method → next
  - `pay_stripe(page, card="4242…")` : full Stripe iframe interaction

## The clone

The default `--base-url` is `http://prodclone.local:8000`: the bench's own web server, which picks the site from
the host name. `prodclone.local` is not served by nginx and is rebuilt from `prod.local` without its secrets, so
after each rebuild:

- the name resolves on osiris: `bench --site prodclone.local add-to-hosts` (once per server);
- an Administrator API key of the clone in `E2E_API_KEY` / `E2E_API_SECRET` (or `~/.e2e-osiris.env`):
  `bench --site prodclone.local execute frappe.core.doctype.user.user.generate_keys --args "['Administrator']"`;
- the test keys copied into the clone's `site_config.json` (`bench --site prodclone.local set-config …`):
  `e2e_test_customer`, `e2e_test_user_email`, `e2e_test_user_password`, `enable_e2e_simulators` (TWINT test);
- the PSP tests (`psp_*`) need the PSP's sandbox settings on the clone, and the hosted-page ones (Wallee, Payrexx)
  a return URL the PSP can reach: without them, run `-m "not psp_wallee and not psp_payrexx"`.

Another test site: `pytest --base-url=http://<name>.local:8000`.

## Tests

| File | Marker | What it covers |
|---|---|---|
| `test_smoke_login.py` | smoke | Framework sanity: login → check session |
| `test_signup_flow.py` | smoke | Guest creates account from checkout |
| `test_profile_edit.py` | smoke | Login → edit address → save → reload → assert |
| `test_cart_lifecycle.py` | smoke | Add ×2 → qty change → remove 1 → assert totals |
| `test_checkout_stripe.py` | checkout, psp_stripe | Full Stripe checkout with 4242 card |
| `test_checkout_wallee.py` | checkout, psp_wallee, slow | Wallee redirect + card test on hosted page |
| `test_checkout_twint.py` | checkout, psp_twint | TWINT overlay + `simulate_consumer_success` |
| `test_re_checkout.py` | edge | Back mid-checkout → re-engage → idempotency |
| `test_change_psp_mid_flow.py` | edge | Select Stripe → switch to Wallee → assert state |
| `test_failed_payment.py` | edge | Stripe declined card → /payment-failed → retry |

## Debugging fails

- Playwright captures screenshot + trace on failure: `test-results/<test-name>/trace.zip` → open in `playwright show-trace`
- Run with `--headed` to watch the browser
- Run with `PWDEBUG=1` to step interactively via Playwright Inspector
- Per-test traces : `pytest --tracing=on`

## Backend calls from the tests

The tests call the whitelisted methods `payments.tests.e2e.fixtures.*` and `payments.tests.e2e.assertions.*` over
HTTP with the Administrator API key (`conftest.py::backend`):
- the state setup (Customer, Address) uses the fixed test customer, never a real one;
- the final assertion (`assert_payment_complete`) reads the Payment Intent / Payment Request / Sales Order triplet in
  the database.

## Automatic cleanup

`reset_test_env` runs BEFORE each test (idempotent). To clean by hand after a run, on the clone:
```bash
bench --site prodclone.local execute payments.tests.e2e.fixtures.reset_test_env
```
