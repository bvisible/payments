# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""E2E payment simulators (dev-only).

The Stripe and Wallee web flows can be driven by the standard sandbox test
card ``4242 4242 4242 4242``, so they don't need a simulator — the runbook
just types the card into Chrome.

TWINT consumer flow has no equivalent — it would normally require a real
TWINT app to scan the QR. To keep the E2E loop self-contained, the live
simulator lives in :func:`payments.api.twint.simulate_consumer_success`, gated
by ``frappe.conf.enable_e2e_simulators=True`` for safety.

This module is intentionally thin and reserved for future PSPs that need an
in-process success/failure simulator (Worldline, Saferpay, etc.).
"""

from __future__ import annotations
