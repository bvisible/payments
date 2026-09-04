#//// Neoffice — added file (no upstream equivalent). Local cache of a Wallee Terminal
#//// Configuration — the hardware profile (PIN pad, printer, connectors) a Wallee
#//// terminal is created against. The canonical record lives on api.wallee.com; this
#//// keeps configuration_id + version_id for the onboarding wizard. Ported from the
#//// retired `wallee_integration` app by 99e929c (ADR-005). Upstream has no Wallee.
#//// Commits: 99e929c 2026-05-19 "feat(payments): merge wallee_integration into payments — ADR-005"
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
