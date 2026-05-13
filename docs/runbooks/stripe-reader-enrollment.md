# Runbook — Enroll a Stripe Terminal Reader

Applies to: BBPOS WisePOS E, Stripe Reader S700, Stripe Reader S710 (server-driven mode).

## Pre-requisites

- Stripe Terminal **server-driven** activated on the target account (`stripe-terminal-betas@stripe.com` if not yet). Verify in Dashboard → Terminal → API tab.
- A `Payment Provider` record with provider_name=`stripe` (or `stripe_test`/`stripe_live`) and credentials_json containing a valid `secret_key`.
- A `Provider Channel Settings` record bound to `(stripe*, terminal)` with `enabled=1`.
- One `Stripe Terminal Location` already created in the same country as the reader (cf. step 1 below).

## Step 1 — Create a Stripe Terminal Location (one-time per branch)

```bash
bench --site <site> execute payments.api.terminal.create_stripe_location \
  --kwargs '{
    "display_name": "Neoffice Lausanne",
    "country": "CH",
    "line1": "Rue X 1",
    "city": "Lausanne",
    "postal_code": "1003"
  }'
```

Returns: `{stripe_location_id: "tml_xxx", display_name: "...", address: {...}}`. The country is immutable; if it's wrong, delete in the Dashboard and re-create.

## Step 2 — Pair the physical reader

1. On the WisePOS E / S700 / S710: **swipe from the left edge** of the screen.
2. Tap **Settings**.
3. Enter admin code **`07139`** (not `0-0-0-0-0-1` as noted in a legacy spec).
4. Tap **Generate pairing code**. Three dash-separated words appear, e.g. `cool-cyan-fox`.

## Step 3 — Register the reader in Frappe

```bash
bench --site <site> execute payments.api.terminal.register_stripe_reader \
  --kwargs '{
    "registration_code": "cool-cyan-fox",
    "location": "tml_xxx",
    "label": "Comptoir 1",
    "device_label": "Caisse principale"
  }'
```

Returns: `{payment_device: "DEV-YYYY-XXXXXX", stripe_reader_id: "tmr_xxx", device_type: "bbpos_wisepos_e", location: "tml_xxx", status: "online"}`.

A `Payment Device` record is now persisted; the cron `*/5 * * * *` keeps its `status` and `last_seen_at` in sync.

## Step 4 — Map a Mode of Payment to this device

In Frappe Desk, create a **POS Payment Driver Mapping** record (or via API):

```bash
bench --site <site> execute frappe.client.insert --kwargs '{
  "doc": {
    "doctype": "POS Payment Driver Mapping",
    "pos_profile": "Caisse",
    "mode_of_payment": "Carte de crédit",
    "provider": "stripe",
    "channel": "terminal",
    "default_device": "DEV-YYYY-XXXXXX",
    "auto_attach_device": 1,
    "enabled": 1
  }
}'
```

## Step 5 — Verify

Smoke test against simulator (replace `simulated-wpe` with a real reader for full E2E):

```bash
bench --site <site> execute payments.tests.phase2_smoke.run_all
```

Should print **9/9 ALL GREEN**.

## Edge cases

| Symptom | Cause | Action |
|---|---|---|
| Reader stuck on offline 10 min+ | WiFi captive portal, DNS, NTP drift | Reboot the reader (it auto-reboots nightly at midnight Location time). Confirm reader can reach `api.stripe.com`, `armada.stripe.com`, `gator.stripe.com`, `*.terminal-events.stripe.com`. |
| Pairing code expired | Codes expire ~60s | Generate a new one and re-run `register_stripe_reader`. |
| `terminal_reader_timeout` in logs | Network blip between Stripe and reader | DO NOT re-create the PaymentIntent. Re-fetch via `stripe.PaymentIntent.retrieve` and decide. |
| Firmware update overdue | Reader not plugged in overnight | Leave the reader plugged in 24h. Updates land at midnight Location time. |
| WiFi 6 (802.11ax) | Not supported on smart readers | Reconfigure router to expose a WPA2/WPA3 SSID on 802.11n/ac. |

## References

- ADR-003 (`docs/adr/ADR-003-stripe-terminal-server-driven.md`)
- Stripe docs https://docs.stripe.com/terminal/payments/setup-integration?terminal-sdk-platform=server-driven
- Phase 2 smoke `payments/tests/phase2_smoke.py`
