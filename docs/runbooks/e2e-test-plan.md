<!-- //// Neoffice — added file (no upstream equivalent). The acceptance checklist for go-live with
     //// real hardware and sandbox credentials (WisePOS E / S700 / S710, TWINT sandbox
     //// P12). Upstream ships nothing of the kind because it has no hardware to accept.
     //// Commits: 7cfe7fa 2026-05-13 "Phase 7 runbooks + Phase 8 PSP template". -->
# Test plan — End-to-end with real hardware + sandbox credentials

Use this as the acceptance checklist when the Stripe Terminal hardware arrives
and the TWINT sandbox P12 is provisioned (Phase 7 go-live).

## Pre-conditions

- [ ] BBPOS WisePOS E (or S700 / S710) unboxed and connected to power + WiFi
- [ ] Stripe sandbox account active for Terminal server-driven in CH
- [ ] TWINT sandbox P12 deposited on `neoservice:/home/frappe/twint-certs/<merchant_uuid>.p12`
- [ ] `payments` app on `feat/unified-payments` deployed to `osiris.local`
- [ ] `neoffice-devops` app on `feat/twint-php-bridge` deployed to `neoservice.neoffice.me`

## Stripe Terminal (real reader)

### 1. Enrollment
- [ ] Generate pairing code on reader (admin `07139`)
- [ ] `register_stripe_reader` returns a Payment Device record
- [ ] `Payment Device.status` flips to `online` within 5 min (or after `sync_stripe_readers_status` cron)

### 2. Card present, full flow
- [ ] `pos_start_payment(amount=1500, currency=CHF)` returns `requires_action` + Stripe `pi_*`
- [ ] Reader displays the amount + prompt
- [ ] Tap a sandbox test card (no actual card needed — Stripe demo mode)
- [ ] Webhook `terminal.reader.action_succeeded` received and logged in `Webhook Event Log`
- [ ] Webhook `payment_intent.succeeded` received and processed
- [ ] Frappe `Payment Intent.status = succeeded`
- [ ] Reconciliation hook appended a Sales Invoice Payment row (if reference is a POS Invoice)
- [ ] POSNext UI updates via SocketIO push (cashier sees "Payment successful")

### 3. Card declined
- [ ] Trigger declined card on reader (Stripe demo mode `4000000000000002`)
- [ ] FSM transitions to `failed` with `error_code` propagated
- [ ] Webhook Event Log shows the failed event

### 4. Cancel mid-flow
- [ ] Start a payment, then immediately click "Cancel" in the dialog
- [ ] FSM transitions to `canceled`
- [ ] `terminal.reader.action_failed` webhook received and logged

### 5. Network resilience
- [ ] Unplug the reader from WiFi mid-flow
- [ ] Confirm the FSM stays in `processing` (not falsely `failed`)
- [ ] Once reconnected, reader resumes the action
- [ ] Final state arrives via webhook

### 6. Refund
- [ ] On a `succeeded` intent, call `pos_refund_payment(intent_name, amount=500)` for a partial refund
- [ ] Stripe Refund created, `charge.refunded` webhook received
- [ ] FSM transitions to `refunded`

## TWINT QR (real sandbox merchant)

### 1. P12 + Twint Bridge Settings ready
- [ ] `Twint Bridge Settings` record exists on the client site with `merchant_uuid`, `store_uuid`, `p12_password`
- [ ] `neoffice_devops.api.twint.execute(command=health_check)` returns `{success: true}`

### 2. QR scan flow
- [ ] `pos_start_payment(provider=twint, channel=qr_bridge, amount=2500)` returns `pairing_token`
- [ ] POSNext UI displays the QR (via `TwintQRDialog.vue`)
- [ ] Scan with TWINT sandbox app (test merchant)
- [ ] `poll_pending_twint_transactions` advances FSM to `succeeded` within 30s
- [ ] SocketIO push received by POSNext
- [ ] Reconciliation hook appended a Sales Invoice Payment row

### 3. Customer aborts in the TWINT app
- [ ] Start a QR payment, scan, then cancel inside the TWINT app
- [ ] Bridge returns `CLIENT_ABORTED` on the next poll
- [ ] FSM transitions to `canceled`

### 4. Timeout
- [ ] Start a QR payment, scan nothing
- [ ] After 10 minutes, scheduler cancels the intent (`twint_timeout` error_code)

### 5. Refund (TWINT reversal)
- [ ] On a `succeeded` TWINT intent, call refund with amount=1000 (1/3 of the total)
- [ ] Bridge `refund_payment` returns success
- [ ] FSM transitions to `refunded`

## Cross-cutting

### Webhook idempotency
- [ ] Replay the same Stripe webhook event (manual `stripe trigger`)
- [ ] `Webhook Event Log` rejects the duplicate (`DuplicateEntryError`)
- [ ] No double Payment Entry created

### Multi-merchant TWINT (data isolation)
- [ ] Create 2 `Twint Bridge Settings` records with different `merchant_uuid` (assuming 2 sandbox P12s)
- [ ] Run a transaction for merchant A
- [ ] Run a transaction for merchant B
- [ ] Verify both succeed independently without cross-contamination of certificates

### Concurrency
- [ ] Trigger 5 simultaneous `pos_start_payment` calls
- [ ] All return distinct `Payment Intent` records
- [ ] Stripe gets 5 distinct PaymentIntents (no idempotency collision)

## Sign-off

Once all the above are ✅ the unified payments app is **production-ready for the
covered scope** (Stripe Terminal + TWINT QR Bridge + POSNext + reconciliation).
Add an entry to `wiki/log.md` and mark Phase 7 completed.
