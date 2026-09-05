# //// Neoffice — added file (no upstream equivalent). Package root of the driver
# //// layer. Upstream `payments` has no notion of a driver at all: it ships one
# //// `payment_gateways/doctype/<psp>_settings/` per PSP, fusing credentials,
# //// channel config and business logic in a single DocType. This package holds
# //// the Provider × Channel × Driver abstraction (ADR-001 / ADR-004) behind the
# //// Payment Intent flow, and coexists with the legacy web-only controllers.
# //// Commits: e32ecf5 2026-05-13 "feat(payments): Phase 1 — unified payment driver layer (Provider × Channel × Driver)"
# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""Unified payment driver layer for the `payments` Frappe app.

This package provides the abstraction layer (Provider × Channel × Driver) used by
the new Payment Intent flow. It coexists with the legacy `payment_gateways/*`
controllers, which remain available for web-only checkout flows.

Key modules:
- :mod:`payments.drivers.base` — abstract base classes.
- :mod:`payments.drivers.registry` — driver registry singleton.
- :mod:`payments.drivers.mock_driver` — in-memory driver used by tests.
- :mod:`payments.drivers.stripe` — Stripe drivers (Phase 2).
- :mod:`payments.drivers.twint` — TWINT drivers via PHP bridge (Phase 4).
"""
