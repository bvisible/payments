# //// Neoffice — added file (no upstream equivalent). Local cache of a Wallee Terminal
# //// Location — the address/branch its terminals belong to. The canonical record lives
# //// on api.wallee.com; this keeps id + version_id for the terminal onboarding wizard
# //// and local lookups. Ported from the retired `wallee_integration` app and folded
# //// into `payments` (module=Payments) by 99e929c (ADR-005). Upstream has no Wallee.
# //// Commits: 99e929c 2026-05-19 "feat(payments): merge wallee_integration into payments — ADR-005"
# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""Wallee Location — local cache of a Wallee Terminal Location.

A Wallee Location groups one or more physical terminals (per address/branch).
The canonical record lives on api.wallee.com — this DocType keeps a local
reference (id + version_id) for the terminal onboarding wizard and any local
lookups.
"""

from __future__ import annotations

from frappe.model.document import Document


class WalleeLocation(Document):
	pass
