#//// Neoffice — added file (no upstream equivalent). Public entry points of the
#//// Payrexx drivers — a Swiss PSP (Thun) covering card-present on a NexGo terminal,
#//// TWINT and web checkout under one contract. Added as a **third** provider
#//// alongside Stripe Terminal and the TWINT bridge, never as a replacement: clients
#//// already in production on those must not regress; the choice is per client,
#//// through `Payment Provider` + `POS Payment Driver Mapping`.
#//// Commits: 4c05756 2026-08-11 "feat(payrexx): add Payrexx as a third payment provider"
#////          a8087dc 2026-08-11 "feat(payrexx): Tap to Pay server lot — the phone initiates, the server records"
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
- ``payrexx_tap_to_pay`` — NFC on the operator's own Android phone

Tap to Pay is the odd one out: it is an Android app-to-app integration over Intents,
so **no server-side call can initiate it**. Its driver holds the intent, hands the
phone what it needs, then reads the transaction back and refunds it; the payment
itself is started by the Payrexx app and reported by the shared webhook.
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
