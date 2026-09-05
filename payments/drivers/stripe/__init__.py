# //// Neoffice — added file (no upstream equivalent). Public entry points of the
# //// Stripe drivers, the names an operator puts in `Payment Provider.driver_class`
# //// / `Provider Channel Settings.driver_class`. Upstream only has the
# //// `Stripe Settings` web-checkout controller.
# //// Commits: e32ecf5 2026-05-13 "feat(payments): Phase 1 — unified payment driver layer (Provider × Channel × Driver)"
# ////          0efe5ef 2026-05-13 "feat(payments): Phase 2 — Stripe Terminal server-driven driver"
# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""Stripe drivers.

Public entry points (resolved via ``Payment Provider.driver_class`` /
``Provider Channel Settings.driver_class``):

- :class:`payments.drivers.stripe.provider.StripeProvider` — credentials + lifecycle
- :class:`payments.drivers.stripe.terminal_driver.StripeTerminalDriver` — POS Terminal (server-driven)
- :class:`payments.drivers.stripe.web_driver.StripeWebDriver` — Web checkout (Phase 6, placeholder)
"""

from payments.drivers.stripe.provider import StripeProvider  # noqa: F401
from payments.drivers.stripe.terminal_driver import StripeTerminalDriver  # noqa: F401
from payments.drivers.stripe.web_driver import StripeWebDriver  # noqa: F401
