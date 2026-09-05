# //// Neoffice — added file (no upstream equivalent). Public entry points of the
# //// TWINT drivers: the POS `qr_bridge` channel and the webshop `twint_web` one,
# //// both routed through the central PHP bridge on neoservice (ADR-002). Upstream
# //// has no TWINT integration of any kind.
# //// Commits: e32ecf5 2026-05-13 "feat(payments): Phase 1 — unified payment driver layer (Provider × Channel × Driver)"
# ////          258f8cf 2026-05-13 "feat(payments): Phase 4 — TWINT PHP bridge driver + scheduler poll"
# ////          ec69d96 2026-05-19 "feat(twint): Phase 11 fusion twint_integration → payments (webshop QR consumer + dialog)"
# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""TWINT drivers — both flows use the centralized PHP bridge on neoservice.

- :class:`payments.drivers.twint.provider.TwintProvider` — credentials + bridge health check
- :class:`payments.drivers.twint.php_bridge_driver.TwintPHPBridgeDriver` —
  channel ``qr_bridge`` (POS terminal, merchant-initiated; QR on cash register)
- :class:`payments.drivers.twint.web_driver.TwintWebDriver` — channel
  ``twint_web`` (webshop, consumer-initiated; QR rendered for the buyer's browser)
"""

from payments.drivers.twint.php_bridge_driver import TwintPHPBridgeDriver  # noqa: F401
from payments.drivers.twint.provider import TwintProvider  # noqa: F401
from payments.drivers.twint.web_driver import TwintWebDriver  # noqa: F401
