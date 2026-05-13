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
