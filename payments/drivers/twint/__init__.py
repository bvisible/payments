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
