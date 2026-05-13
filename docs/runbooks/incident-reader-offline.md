# Runbook — Stripe Reader offline incident

## Trigger

- Cashier reports "terminal not responding" / "card payment fails"
- Frappe Desk → Payment Device list shows status=offline for ≥ 5 minutes
- Stripe Dashboard → Terminal → Readers shows offline

## Quick triage (2 minutes)

1. **Power cycle the reader**: long-press the power button (5s), wait, power on.
2. **Check WiFi signal**: on the reader, swipe down from top to see WiFi indicator. If poor, move closer to the AP.
3. **Network connectivity**: from any laptop on the same SSID, `ping api.stripe.com` (or open https://status.stripe.com).
4. **Stripe status**: https://status.stripe.com — confirm Stripe Terminal is operational.

## Deep dive (if quick triage failed)

### A. Verify reader can reach Stripe endpoints

The reader needs **outbound TCP/443** to:
- `api.stripe.com`
- `armada.stripe.com`
- `gator.stripe.com`
- `*.terminal-events.stripe.com`
- `api.emms.bbpos.com` (BBPOS reader telemetry)
- NTP (UDP/123) for clock sync

From the WiFi router admin or firewall, confirm no egress filtering blocks these.

**WiFi 6 (802.11ax) is NOT supported on smart readers.** Force the AP to advertise WPA2/WPA3 on 802.11n/ac.

### B. Force a status refresh

```bash
bench --site <site> execute payments.api.terminal.sync_stripe_readers_status
```

Returns stats: `{providers, devices_checked, devices_updated, errors}`. If `devices_updated=0` and the reader is online physically, the Payment Device record may be stale — look at `last_seen_at` in the Frappe Desk.

### C. Re-register the reader

If the reader is bricked or the pairing was lost:

```bash
# 1. Generate a new pairing code (see stripe-reader-enrollment runbook).
# 2. Re-register:
bench --site <site> execute payments.api.terminal.register_stripe_reader \
  --kwargs '{"registration_code": "new-pair-code", "location": "tml_xxx", "label": "..."}'
```

The previous `Payment Device` record stays (don't delete it — historic Payment Intents reference it). The new one supersedes it for new transactions.

### D. Firmware update stuck

Stripe pushes firmware automatically; the reader reboots **nightly at midnight Location time**. If a reader misses several nights (unplugged), it may attempt a slow update on power-on. Plug it in and wait 30 minutes before testing.

## Workaround for cashiers during incident

- Switch the Mode of Payment in POSNext to **Cash** temporarily.
- Or accept the card manually via Stripe Dashboard → Payments → Create → enter card details (test mode of operation, not recommended for prod compliance reasons).

## Escalation

- Stripe support: https://support.stripe.com (24/7 for Terminal issues)
- BBPOS hardware support (WisePOS E): via Stripe support escalation

## Post-incident

- Update `Neoffice/Payments/Runbooks/incident-reader-offline.md` with what you learned (new symptoms, new fixes).
- If the incident lasted > 15 minutes, file an entry in `wiki/log.md` with the resolution.
