#//// Neoffice — added file (no upstream equivalent). Wallee credentials (user_id,
#//// authentication_key, space_id) for the Wallee driver. Ported from the retired
#//// `wallee_integration` app, folded into `payments` by 99e929c (ADR-005), and
#//// turned from a Single into a regular DocType autonamed on `provider`, so a test
#//// and a live Wallee account can cohabit on one site. Upstream has no Wallee.
#//// Commits: 99e929c 2026-05-19 "feat(payments): merge wallee_integration into payments — ADR-005"
# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""Wallee Settings — per-provider configuration for the Wallee driver.

Previously a Single DocType in the legacy ``wallee_integration`` app. After
ADR-005 (merger into ``payments``), this is a regular DocType with one record
per ``Payment Provider`` of class :class:`payments.drivers.wallee.WalleeProvider`.

The provider field is the autoname + unique key, so two enabled providers
(e.g. ``wallee_test`` and ``wallee_live``) can cohabit on the same site.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document


class WalleeSettings(Document):
	def validate(self):
		if not self.provider:
			frappe.throw(_("Payment Provider is required on Wallee Settings."))
		if self.enabled and not (self.user_id and self.authentication_key and self.space_id):
			frappe.throw(
				_("user_id, authentication_key and space_id are mandatory when Wallee Settings is enabled.")
			)
