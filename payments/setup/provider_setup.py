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

import json
from typing import Any

import frappe


#: Names of the clearing accounts the chart already uses for other payment means,
#: tried in order to find where a new one belongs (its parent and its number).
_ANCHOR_HINTS = ("Twint", "carte de crédit", "Postcard", "Paypal")


# ---------------------------------------------------------------------------
# Where a setup page keeps what it knows
# ---------------------------------------------------------------------------
# A DefaultValue under « __default » is sent to every desk user in the boot (`sysdefaults`): the setup
# state (who connected, the account's methods, the parked shop rows) does not belong there. It is kept
# under « __global », which the boot does not send, and moved there the first time it is read.


def read_state(key: str) -> dict:
	raw = frappe.db.get_global(key)
	if raw is None:
		legacy = frappe.db.get_default(key)
		if legacy is not None:
			frappe.db.set_global(key, legacy)
			frappe.defaults.clear_default(key, parent="__default")
			raw = legacy
	try:
		value = frappe.parse_json(raw or "{}")
	except (ValueError, TypeError):
		return {}
	return value if isinstance(value, dict) else {}


def write_state(key: str, value: dict) -> None:
	frappe.db.set_global(key, json.dumps(value, default=str))


# ---------------------------------------------------------------------------
# Companies
# ---------------------------------------------------------------------------


def shop_company() -> str | None:
	"""The company the shop sells for (Webshop Settings), else the site's default company."""
	company = None
	if frappe.db.exists("DocType", "Webshop Settings"):
		company = frappe.db.get_single_value("Webshop Settings", "company")
	return company or frappe.defaults.get_global_default("company")


def shop_gateway_account(gateway: str) -> str | None:
	"""The gateway's account for the shop's company.

	Never another company's account: a payment booked to the wrong company's clearing
	account is worse than a tile that cannot be offered. A single-company site whose
	account was made without a company still finds it.
	"""
	company = shop_company()
	found = frappe.db.get_value(
		"Payment Gateway Account", {"payment_gateway": gateway, "company": company}, "name"
	)
	if found:
		return found
	return frappe.db.get_value(
		"Payment Gateway Account", {"payment_gateway": gateway, "company": ["in", ["", None]]}, "name"
	)


def companies_in_use() -> list[str]:
	"""The shop's company, the default company and every company that has an enabled till."""
	companies = []
	for company in (shop_company(), frappe.defaults.get_global_default("company")):
		if company and company not in companies:
			companies.append(company)
	for row in frappe.get_all("POS Profile", filters={"disabled": 0}, fields=["company"]):
		if row.company and row.company not in companies:
			companies.append(row.company)
	return companies


def ensure_clearing_account(company: str, label: str, gateway: str | None = None) -> str | None:
	"""The company's clearing account for a provider; created beside the others if missing.

	Found first through what is already wired: the account of ``gateway``'s Payment
	Gateway Account for this company, then an account named ``label``. The label is in
	the site's language, so a site whose language changed would otherwise get a second
	account under another name.

	A new one goes under the parent of an existing clearing account in the company's
	currency (TWINT first), with the first free number after it — on a Swiss chart that is
	103x under « Trésorerie », next to the accounts the bookkeeper already knows. Without
	such an anchor it goes under the parent of the company's first bank account. A
	company with no bank account at all gets none (``None``): a setup page lays down the
	others rather than stop on it.
	"""
	if gateway:
		wired = frappe.db.get_value(
			"Payment Gateway Account", {"payment_gateway": gateway, "company": company}, "payment_account"
		)
		if wired:
			return wired
	existing = frappe.db.get_value(
		"Account", {"company": company, "account_name": label, "is_group": 0}, "name"
	)
	if existing:
		return existing

	currency = frappe.get_cached_value("Company", company, "default_currency")
	base = {"company": company, "is_group": 0, "account_type": "Bank"}
	if currency:
		base["account_currency"] = ["in", [currency, "", None]]
	anchor = None
	for hint in _ANCHOR_HINTS:
		anchor = frappe.db.get_value(
			"Account",
			{**base, "account_name": ["like", f"%{hint}%"]},
			["parent_account", "account_number", "account_currency"],
			as_dict=True,
		)
		if anchor:
			break
	if not anchor:
		anchor = frappe.db.get_value(
			"Account", base, ["parent_account", "account_number", "account_currency"], as_dict=True
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
			"account_currency": currency or anchor.account_currency,
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


def ensure_till_mode(mode: str, accounts: dict[str, str]) -> dict[str, Any]:
	"""A Bank ``Mode of Payment`` booked to ``accounts`` (company → account).

	A mode created here is put on every enabled till of a company in ``accounts``. A mode
	that already existed is not put back on a till: the merchant may have taken it off
	in Payment settings, and « Connect » again must not undo that. Its enabled state is
	kept as it was. Returns ``{"created", "tills", "errors"}`` — a till that refuses the
	mode is named in ``errors`` and the others still get it.
	"""
	created = not frappe.db.exists("Mode of Payment", mode)
	if created:
		doc = frappe.new_doc("Mode of Payment")
		doc.mode_of_payment = mode
		doc.type = "Bank"
		doc.enabled = 1
		wanted_enabled = 1
	else:
		doc = frappe.get_doc("Mode of Payment", mode)
		wanted_enabled = doc.enabled
	known = {row.company for row in doc.get("accounts") or []}
	missing = {c: a for c, a in accounts.items() if c not in known}
	for company, account in missing.items():
		doc.append("accounts", {"company": company, "default_account": account})
	if created or missing:
		doc.flags.ignore_permissions = True
		doc.save()
	# //// Neoffice — the theme's `handle_mode_of_payment` (on_update) disabled every mode with no
	# //// bank account linked, a rule meant for QR-bill modes (neoffice-maintenance#2422, fixed in
	# //// neoffice-theme 78d40a52). Until every site runs that fix, put back the state it had.
	if frappe.db.get_value("Mode of Payment", mode, "enabled") != wanted_enabled:
		frappe.db.set_value("Mode of Payment", mode, "enabled", wanted_enabled, update_modified=False)

	errors = {}
	if created:
		for name in frappe.get_all("POS Profile", filters={"disabled": 0}, pluck="name"):
			profile = frappe.get_doc("POS Profile", name)
			if profile.company not in accounts:
				continue
			if any(row.mode_of_payment == mode for row in profile.payments):
				continue
			profile.append("payments", {"mode_of_payment": mode, "default": 0, "allow_in_returns": 0})
			profile.flags.ignore_permissions = True
			point = f"till_{frappe.generate_hash(length=8)}"
			frappe.db.savepoint(point)
			try:
				profile.save()
			except Exception as exc:  # noqa: BLE001 - one till's refusal must not stop the others
				frappe.db.rollback(save_point=point)
				errors[name] = str(exc)[:200]
			else:
				frappe.db.release_savepoint(point)
	return {"created": created, "tills": till_mode_tills(mode), "errors": errors}


def till_mode_tills(mode: str) -> list[str]:
	"""The enabled tills that offer ``mode``."""
	parents = frappe.get_all(
		"POS Payment Method",
		filters={"mode_of_payment": mode, "parenttype": "POS Profile"},
		pluck="parent",
		distinct=True,
	)
	return [p for p in parents if not frappe.db.get_value("POS Profile", p, "disabled")]


# ---------------------------------------------------------------------------
# Terminals at a till
# ---------------------------------------------------------------------------


def offer_mode_on(pos_profile: str, mode: str) -> bool:
	"""Put ``mode`` on one till if it does not offer it yet; False when its company has no account."""
	profile = frappe.get_doc("POS Profile", pos_profile)
	if any(row.mode_of_payment == mode for row in profile.payments):
		return True
	if not frappe.db.exists("Mode of Payment Account", {"parent": mode, "company": profile.company}):
		return False
	profile.append("payments", {"mode_of_payment": mode, "default": 0, "allow_in_returns": 0})
	profile.flags.ignore_permissions = True
	profile.save()
	return True


def wire_terminal(pos_profile: str, mode: str, device: str, provider: str) -> None:
	"""The till's ``mode`` drives ``device``, its default device for that mode."""
	mapping_name = frappe.db.get_value(
		"POS Payment Driver Mapping", {"pos_profile": pos_profile, "mode_of_payment": mode}, "name"
	)
	mapping = (
		frappe.get_doc("POS Payment Driver Mapping", mapping_name)
		if mapping_name
		else frappe.new_doc("POS Payment Driver Mapping")
	)
	mapping.update(
		{
			"pos_profile": pos_profile,
			"mode_of_payment": mode,
			"enabled": 1,
			"provider": provider,
			"channel": "terminal",
			"default_device": device,
			"auto_attach_device": 1,
		}
	)
	mapping.flags.ignore_permissions = True
	mapping.save()

	profile = frappe.get_doc("POS Profile", pos_profile)
	if not profile.meta.get_field("custom_active_payment_devices"):
		return
	rows = profile.get("custom_active_payment_devices") or []
	mine = next((r for r in rows if r.payment_device == device), None)
	for row in rows:
		# One default per mode: the till would otherwise pick whichever came first.
		if row is not mine and row.mode_of_payment == mode and row.is_default:
			row.is_default = 0
	if mine:
		mine.mode_of_payment = mode
		mine.is_default = 1
	else:
		profile.append(
			"custom_active_payment_devices",
			{"payment_device": device, "mode_of_payment": mode, "is_default": 1},
		)
	profile.flags.ignore_permissions = True
	profile.save()


def unwire_terminal(device: str, only: str | None = None) -> list[str]:
	"""Take ``device`` off the till ``only``, or off every till; returns the tills it left.

	A mapping whose default device it was keeps its mode but no device: the till then
	asks which terminal, or takes the mode as a terminal used on its own.
	"""
	left = []
	if frappe.db.exists("DocType", "POS Payment Driver Mapping"):
		for name, till in frappe.get_all(
			"POS Payment Driver Mapping",
			filters={"default_device": device},
			fields=["name", "pos_profile"],
			as_list=True,
		):
			if only and till != only:
				continue
			frappe.db.set_value("POS Payment Driver Mapping", name, "default_device", None)
			left.append(till)
	field = frappe.get_meta("POS Profile").get_field("custom_active_payment_devices")
	if field:
		for till in frappe.get_all(
			field.options, filters={"payment_device": device}, pluck="parent", distinct=True
		):
			if only and till != only:
				continue
			profile = frappe.get_doc("POS Profile", till)
			profile.set(
				"custom_active_payment_devices",
				[r for r in profile.custom_active_payment_devices if r.payment_device != device],
			)
			profile.flags.ignore_permissions = True
			profile.save()
			left.append(till)
	return sorted(set(left))


def _enabled(modes) -> str | None:  # noqa: ANN001
	"""The first of ``modes`` that is an enabled Mode of Payment: a till cannot offer a disabled one."""
	for mode in modes:
		if mode and frappe.db.get_value("Mode of Payment", mode, "enabled"):
			return mode
	return None


def terminal_mode(device: str) -> str | None:
	"""The enabled till mode a terminal is already driven by, at any till."""
	modes = []
	if frappe.db.exists("DocType", "POS Payment Driver Mapping"):
		modes += frappe.get_all(
			"POS Payment Driver Mapping", filters={"default_device": device}, pluck="mode_of_payment"
		)
	field = frappe.get_meta("POS Profile").get_field("custom_active_payment_devices")
	if field:
		modes += frappe.get_all(
			field.options, filters={"payment_device": device}, pluck="mode_of_payment"
		)
	return _enabled(modes)


def driven_mode(provider: str) -> str | None:
	"""The enabled till mode that already drives ``provider``'s terminals at a till, if any."""
	if not frappe.db.exists("DocType", "POS Payment Driver Mapping"):
		return None
	return _enabled(
		frappe.get_all(
			"POS Payment Driver Mapping",
			filters={"provider": provider, "channel": "terminal", "enabled": 1},
			pluck="mode_of_payment",
			order_by="creation asc",
		)
	)


def terminal_tills(device: str) -> list[str]:
	"""The tills ``device`` is wired to."""
	tills = set()
	if frappe.db.exists("DocType", "POS Payment Driver Mapping"):
		tills.update(
			frappe.get_all(
				"POS Payment Driver Mapping",
				filters={"default_device": device, "enabled": 1},
				pluck="pos_profile",
			)
		)
	field = frappe.get_meta("POS Profile").get_field("custom_active_payment_devices")
	if field:
		tills.update(frappe.get_all(field.options, filters={"payment_device": device}, pluck="parent"))
	return sorted(tills)
