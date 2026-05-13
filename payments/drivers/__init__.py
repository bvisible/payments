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
