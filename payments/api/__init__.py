#//// Neoffice — added file (no upstream equivalent). Package marker and index of the
#//// whitelisted API surface of the unified payment hub: Payment Intent lifecycle, the
#//// per-PSP webhook receivers, Stripe Terminal management, the TWINT poller. Upstream
#//// has no `payments/api/` at all — its only callable surface is the per-gateway
#//// checkout pages under `payment_gateways/`.
#//// Commits: e32ecf5 2026-05-13 "feat(payments): Phase 1 — unified payment driver layer (Provider × Channel × Driver)"
# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""Public whitelisted API surface for the unified payments app.

//// Neoffice — this index is ours; upstream has no `payments/api/` at all. It
//// named 4 of the 11 modules until 2026-09-04. Add a module here when you add
//// one: an index nobody completes is worse than no index.
Modules:

- ``intent``: create, read, cancel and refund Payment Intents
- ``reconciliation``: match settled payments against their accounting entries
- ``mobile_payments``: the phone's surface — collect without a document, poll,
  and the Tap to Pay session
- ``card_receipt`` / ``terminal_receipt``: the card receipt a card-present
  payment must be able to produce
- ``terminal``: Stripe Terminal management (readers, locations)
- ``twint``: TWINT scheduler + helpers (no webhook exists, so state is polled)
- ``payrexx_setup``: one-call provisioning of the Payrexx provider and bindings
- ``webhook_stripe`` / ``webhook_wallee`` / ``webhook_payrexx``: the per-PSP
  receivers (raw body + signature + dedup + RQ)
"""
