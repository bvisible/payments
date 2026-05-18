# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""Wallee drivers.

Public entry points (resolved via ``Payment Provider.driver_class`` /
``Provider Channel Settings.driver_class``):

- :class:`payments.drivers.wallee.provider.WalleeProvider` — credentials lookup
  (reads ``Wallee Settings`` single DocType from the legacy ``wallee_integration``
  app, which we keep installed for its config DocTypes).
- :class:`payments.drivers.wallee.terminal_driver.WalleeTerminalDriver` — POS
  Terminal (server-driven, async via webhook).

The Wallee SDK (``wallee>=6.4.0``) is loaded lazily inside each method, so this
module is cheap to import on sites that don't use Wallee.
"""

from payments.drivers.wallee.provider import WalleeProvider  # noqa: F401
from payments.drivers.wallee.terminal_driver import WalleeTerminalDriver  # noqa: F401
