# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""TWINT drivers — calls the PHP bridge hosted on neoservice.

- :class:`payments.drivers.twint.provider.TwintProvider` — credentials + bridge health check
- :class:`payments.drivers.twint.php_bridge_driver.TwintPHPBridgeDriver` — driver
  for the ``qr_bridge`` channel, dispatches via ``neoffice_devops.api.twint.execute``
"""

from payments.drivers.twint.php_bridge_driver import TwintPHPBridgeDriver  # noqa: F401
from payments.drivers.twint.provider import TwintProvider  # noqa: F401
