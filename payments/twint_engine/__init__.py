# //// Neoffice — added file (no upstream equivalent). The instance speaks TWINT itself, in Python, instead of
# //// sending each call (and the merchant's certificate password) to the hub's PHP bridge (maintenance#1721). Same
# //// commands, same answers as twint_bridge.php + twint-ag/sdk; the SOAP messages are the SDK's byte for byte.
# //// Pure Python: no `import frappe` here, so the bench can run it next to the PHP bridge outside any site.
# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""TWINT engine: the hub's PHP bridge, on the instance."""

from payments.twint_engine.commands import COMMANDS, ENGINE_ONLY_COMMANDS, execute  # noqa: F401
from payments.twint_engine.messages import DEFAULT_VERSION, VERSIONS  # noqa: F401


def supports(command: str) -> bool:
	return command in COMMANDS or command in ENGINE_ONLY_COMMANDS
