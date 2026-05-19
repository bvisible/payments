# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""Wallee Terminal Configuration — local cache of a Wallee Terminal Configuration.

A Wallee Terminal Configuration is a hardware profile (PIN pad, printer
settings, connectors, …) referenced by Wallee Payment Terminals when created.
The canonical record lives on api.wallee.com — this DocType keeps a local
reference (configuration_id + version_id) for the onboarding wizard.
"""

from __future__ import annotations

from frappe.model.document import Document


class WalleeTerminalConfiguration(Document):
	pass
