# Webshop E2E — Playwright Python

## Quick start

```bash
cd /Users/jeremy/GitHub/payments
source .venv-e2e/bin/activate
cd payments/tests/e2e/playwright
pytest                              # all tests, headless
pytest -m smoke                     # smoke only
pytest -m psp_stripe                # Stripe-only
pytest --headed                     # see the browser
pytest -k "signup or cart"          # by name
pytest --slowmo=300                 # slowmo for debugging
```

## Architecture

- **`conftest.py`** : pytest fixtures
  - `backend` : SSH helper to run `bench execute` commands on Osiris
  - `test_customer` : ensure_test_customer + cleanup before each test
  - `logged_in_page` : authenticated Playwright `Page` ready to use
  - `paying_item` : Website Item code with positive price

- **`helpers.py`** : reusable building blocks
  - `login_via_api(page, email, password)` : sets the sid cookie correctly
  - `complete_address_step(page)` : pre-filled address form → next
  - `complete_shipping_step(page)` : select first shipping method → next
  - `pay_stripe(page, card="4242…")` : full Stripe iframe interaction

## Configuration

The tests target `osiris.neoffice.me` by default. Override:
```bash
pytest --base-url=https://myothersite.example
```

The test customer + password come from osiris site_config keys:
- `e2e_test_customer`
- `e2e_test_user_email`
- `e2e_test_user_password`
- `enable_e2e_simulators` (required for TWINT test)

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

## Pattern : appel backend depuis le test

Les tests appellent `bench execute payments.tests.e2e.fixtures.X` sur Osiris via SSH (configuré dans `conftest.py::backend` fixture). Avantages :
- Le setup state (Customer, Address) ne pollue pas la DB de l'utilisateur réel
- L'assertion finale (`assert_payment_complete`) interroge directement la DB Frappe pour vérifier le triplet PI/PR/SO

## Cleanup automatique

`conftest.py::test_customer` lance `reset_test_env` AVANT chaque test (idempotent). À la fin du run, on peut optionnellement nettoyer manuellement :
```bash
ssh osiris "bench --site prod.local execute payments.tests.e2e.fixtures.reset_test_env"
```
