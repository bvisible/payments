# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""Payrexx drivers — Swiss PSP covering card, TWINT and web in one contract.

HTTP lives in the standalone ``payrexx`` library
(https://github.com/neoserviceai/payrexx-python), which encodes the API behaviours
that fail silently. These modules only bridge it to the Provider × Channel × Driver
ontology.

Channels:

- ``payrexx_web`` — hosted payment page, for the webshop
- ``terminal`` — card present on a NexGo terminal (ECR), shared with Stripe/Wallee

Tap to Pay is intentionally absent: it is an Android app-to-app integration over
Intents, not a REST channel, so no server-side driver can initiate it. Its
transactions still arrive through the shared webhook and are recorded.
"""

from payments.drivers.payrexx.provider import PayrexxProvider
from payments.drivers.payrexx.terminal_driver import PayrexxTerminalChannel, PayrexxTerminalDriver
from payments.drivers.payrexx.web_driver import PayrexxWebChannel, PayrexxWebDriver

__all__ = [
	"PayrexxProvider",
	"PayrexxTerminalChannel",
	"PayrexxTerminalDriver",
	"PayrexxWebChannel",
	"PayrexxWebDriver",
]
