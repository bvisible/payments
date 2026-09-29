# //// Neoffice — added file (no upstream equivalent). The one place that says who may call the
# //// end-to-end helpers (#952).
# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""Who may call the end-to-end helpers.

Every function of `fixtures.py` and `assertions.py` is a `@frappe.whitelist()` endpoint that ships with
the app to every instance, and none of them checked its caller: any signed-in account, a shop signup
included, could delete a user and their customer, move a customer to the B2B group, create the discount
rule that goes with it, read any Payment Intent or the test password. They are test tooling, so they answer
a System Manager only, and only on a site that switched the end-to-end simulators on
(`enable_e2e_simulators`, the flag the mobile payment simulators already use).
"""

from __future__ import annotations

import frappe
from frappe import _


def e2e_only(*, needs_simulators: bool = True) -> None:
	"""Refuse anyone but a System Manager, and any site that has not switched the simulators on.

	`get_e2e_site_config` is the one helper that must answer on a site with the simulators off: it is how the
	suite learns that they are, so it passes `needs_simulators=False` and keeps the role check.
	"""
	frappe.only_for("System Manager")
	if needs_simulators and not frappe.conf.get("enable_e2e_simulators"):
		frappe.throw(_("The end-to-end helpers are off on this site."), frappe.PermissionError)
