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
