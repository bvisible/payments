# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""Wallee drivers.

Public entry points (resolved via ``Payment Provider.driver_class`` /
``Provider Channel Settings.driver_class``):

- :class:`payments.drivers.wallee.provider.WalleeProvider` — credentials lookup
  (reads the per-provider ``Wallee Settings`` DocType — post-ADR-005 merger).
- :class:`payments.drivers.wallee.terminal_driver.WalleeTerminalDriver` — POS
  Terminal (server-driven, async via webhook).
- :class:`payments.drivers.wallee.web_driver.WalleeWebDriver` — Hosted payment
  page (Redirect / Lightbox / iFrame), customer redirect flow for webshop.

The Wallee SDK (``wallee>=6.4.0``) is loaded lazily inside each method, so this
module is cheap to import on sites that don't use Wallee.
"""

from payments.drivers.wallee.provider import WalleeProvider  # noqa: F401
from payments.drivers.wallee.terminal_driver import WalleeTerminalDriver  # noqa: F401
from payments.drivers.wallee.web_driver import WalleeWebDriver  # noqa: F401
