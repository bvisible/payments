#//// Neoffice — added file (no upstream equivalent). Package marker and index of the
#//// whitelisted API surface of the unified payment hub: Payment Intent lifecycle, the
#//// per-PSP webhook receivers, Stripe Terminal management, the TWINT poller. Upstream
#//// has no `payments/api/` at all — its only callable surface is the per-gateway
#//// checkout pages under `payment_gateways/`.
#//// Commits: e32ecf5 2026-05-13 "feat(payments): Phase 1 — unified payment driver layer (Provider × Channel × Driver)"
# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""Public whitelisted API surface for the unified payments app.

Modules:
- ``intent``: create/get/cancel/refund Payment Intents
- ``webhook_stripe``: Stripe webhook endpoint (raw body + signature + dedup + RQ)
- ``terminal``: Stripe Terminal management (Phase 2)
- ``twint``: TWINT scheduler + helpers (Phase 4)
"""
