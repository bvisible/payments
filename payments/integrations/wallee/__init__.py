# //// Neoffice — added file (no upstream equivalent). Wizard- and webshop-facing Wallee
# //// helpers, kept apart from `payments.drivers.wallee.*` which is the runtime path
# //// during a payment. Symmetric to `payments.api.terminal` for Stripe. Arrived with
# //// the ADR-005 fusion of the retired `wallee_integration` app.
# //// Commits: 99e929c 2026-05-19 "feat(payments): merge wallee_integration into payments — ADR-005"
# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""Wallee-specific helpers exposed for the unified ``payment_terminal_wizard``.

The driver classes (``payments.drivers.wallee.*``) are the runtime entry point
during payment flows. This package exposes a separate, **wizard-facing** API:
``test_connection``, ``sync_locations_from_wallee``, ``create_terminal``, etc.

Symmetric to ``payments.api.terminal`` (which holds Stripe-specific wizard
helpers like ``create_stripe_location``, ``register_stripe_reader``).
"""
