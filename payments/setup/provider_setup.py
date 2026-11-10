# //// Neoffice — added file (no upstream equivalent). What every provider's setup page needs on the
# //// ERP side once the merchant has pasted a key: a clearing account in the chart, the payment
# //// gateways and their accounts, and a till mode for a terminal used on its own. Payrexx and
# //// Wallee call the same functions, so the two pages lay the same things down the same way.
# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""ERP-side groundwork shared by the provider setup pages (Payrexx, Wallee).

A merchant pastes a key and expects to take money. Before that is possible the ERP
needs, per company: an account the provider's payments are booked to while they wait
for the payout, a ``Payment Gateway`` with a ``Payment Gateway Account`` on it (the
shop's tiles hang off those), and at the till a ``Mode of Payment`` the cashier picks
when the terminal is used on its own. Each function here is idempotent: it finds what
is already there and only creates what is missing, so a setup page can run them on
every « Connect » without duplicating anything.
"""

from __future__ import annotations

import frappe


#: Names of the clearing accounts the chart already uses for other payment means,
#: tried in order to find where a new one belongs (its parent and its number).
_ANCHOR_HINTS = ("Twint", "carte de crédit", "Postcard", "Paypal")


def companies_in_use() -> list[str]:
	"""The default company and every company that has an enabled till."""
	companies = []
	default = frappe.defaults.get_global_default("company")
	if default:
		companies.append(default)
	for row in frappe.get_all("POS Profile", filters={"disabled": 0}, fields=["company"]):
		if row.company and row.company not in companies:
			companies.append(row.company)
	return companies


def ensure_clearing_account(company: str, label: str) -> str | None:
	"""The company's clearing account named ``label``; created beside the others if missing.

	It goes under the parent of an existing clearing account (TWINT first), with the
	first free number after it — on a Swiss chart that is 103x under « Trésorerie », next
	to the accounts the bookkeeper already knows. Without such an anchor it goes under the
	parent of the company's first bank account. A company with no bank account at all
	gets none (``None``): a setup page lays down the others rather than stop on it.
	"""
	existing = frappe.db.get_value(
		"Account", {"company": company, "account_name": label, "is_group": 0}, "name"
	)
	if existing:
		return existing

	anchor = None
	for hint in _ANCHOR_HINTS:
		anchor = frappe.db.get_value(
			"Account",
			{
				"company": company,
				"is_group": 0,
				"account_type": "Bank",
				"account_name": ["like", f"%{hint}%"],
			},
			["parent_account", "account_number", "account_currency"],
			as_dict=True,
		)
		if anchor:
			break
	if not anchor:
		anchor = frappe.db.get_value(
			"Account",
			{"company": company, "is_group": 0, "account_type": "Bank"},
			["parent_account", "account_number", "account_currency"],
			as_dict=True,
		)
	if not anchor:
		return None

	number = _free_number(company, anchor.account_number)
	doc = frappe.get_doc(
		{
			"doctype": "Account",
			"account_name": label,
			"account_number": number,
			"parent_account": anchor.parent_account,
			"company": company,
			"account_type": "Bank",
			"account_currency": anchor.account_currency
			or frappe.get_cached_value("Company", company, "default_currency"),
			"is_group": 0,
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


def _free_number(company: str, after: str | None) -> str | None:
	"""The first account number after ``after`` that the company does not use, else none."""
	if not after or not str(after).isdigit():
		return None
	start = int(after)
	for candidate in range(start + 1, start + 60):
		if not frappe.db.exists("Account", {"company": company, "account_number": str(candidate)}):
			return str(candidate)
	return None


def ensure_gateway(gateway: str) -> str:
	if not frappe.db.exists("Payment Gateway", gateway):
		frappe.get_doc({"doctype": "Payment Gateway", "gateway": gateway}).insert(
			ignore_permissions=True
		)
	return gateway


def ensure_gateway_account(gateway: str, company: str, payment_account: str) -> str:
	"""The gateway's account for this company; created on ``payment_account`` if missing."""
	ensure_gateway(gateway)
	existing = frappe.db.get_value(
		"Payment Gateway Account", {"payment_gateway": gateway, "company": company}, "name"
	)
	if existing:
		return existing
	doc = frappe.get_doc(
		{
			"doctype": "Payment Gateway Account",
			"payment_gateway": gateway,
			"company": company,
			"payment_account": payment_account,
			"is_default": 0,
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


def ensure_till_mode(mode: str, accounts: dict[str, str]) -> list[str]:
	"""A Bank ``Mode of Payment`` booked to ``accounts`` (company → account), on every till.

	Returns the tills (POS Profiles) that offer it. A till of a company with no account
	in ``accounts`` is left alone: ERPNext refuses a mode without a default account for
	the till's company.
	"""
	if frappe.db.exists("Mode of Payment", mode):
		doc = frappe.get_doc("Mode of Payment", mode)
	else:
		doc = frappe.new_doc("Mode of Payment")
		doc.mode_of_payment = mode
		doc.type = "Bank"
	known = {row.company for row in doc.get("accounts") or []}
	for company, account in accounts.items():
		if company not in known:
			doc.append("accounts", {"company": company, "default_account": account})
	doc.enabled = 1
	doc.flags.ignore_permissions = True
	doc.save()
	# //// Neoffice — the theme's `handle_mode_of_payment` (on_update) disabled every mode with no
	# //// bank account linked, a rule meant for QR-bill modes (neoffice-maintenance#2422, fixed in
	# //// neoffice-theme 78d40a52). Until every site runs that fix, put back what was just asked for.
	if not frappe.db.get_value("Mode of Payment", mode, "enabled"):
		frappe.db.set_value("Mode of Payment", mode, "enabled", 1, update_modified=False)

	tills = []
	for name in frappe.get_all("POS Profile", filters={"disabled": 0}, pluck="name"):
		profile = frappe.get_doc("POS Profile", name)
		if profile.company not in accounts:
			continue
		if not any(row.mode_of_payment == mode for row in profile.payments):
			profile.append("payments", {"mode_of_payment": mode, "default": 0, "allow_in_returns": 0})
			profile.flags.ignore_permissions = True
			profile.save()
		tills.append(name)
	return tills


def till_mode_tills(mode: str) -> list[str]:
	"""The enabled tills that offer ``mode``."""
	parents = frappe.get_all(
		"POS Payment Method",
		filters={"mode_of_payment": mode, "parenttype": "POS Profile"},
		pluck="parent",
		distinct=True,
	)
	return [p for p in parents if not frappe.db.get_value("POS Profile", p, "disabled")]
