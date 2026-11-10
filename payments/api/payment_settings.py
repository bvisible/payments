# //// Neoffice — added file (no upstream equivalent). Backend of the « Payment settings » page: who
# //// takes what, on the shop and at the till, in a few clicks. Jérémy (2026-11-10): nothing may switch
# //// on its own — Payrexx opening online sales or not — the merchant chooses here, per payment means,
# //// and TWINT can stay direct. The shop is the Webshop Settings payment rows; the till is the modes
# //// each POS Profile offers.
# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""Who takes what: the shop's payment tiles and the till's modes, chosen per payment means.

The shop offers one tile per payment means. Choosing a provider for « card » swaps the
card tile: the other providers' card rows leave Webshop Settings, parked so that going
back restores them exactly (customer group, settlement, template). A provider that cannot
take the means yet — Payrexx before it opens online sales, Wallee in test — is listed
with the reason and cannot be chosen.

The till side is the payment-provider modes every enabled till offers, switched on or
off for all of them; a row taken off a till is parked too, and comes back as it was.
"""

from __future__ import annotations

import re
from typing import Any

import frappe
from frappe import _

from payments.setup.provider_setup import (
	read_state,
	shop_company,
	shop_gateway_account,
	write_state,
)

ROLES = ("System Manager", "Accounts Manager")
PARKED_KEY = "payment_settings_parked_rows"
PARKED_TILL_KEY = "payment_settings_parked_till_rows"
METHODS = ("card", "twint")
#: The fields of a parked row that are its own, not the bookkeeping of the child table.
_ROW_META = frozenset(
	{
		"name",
		"parent",
		"parentfield",
		"parenttype",
		"idx",
		"creation",
		"modified",
		"owner",
		"modified_by",
		"docstatus",
		"doctype",
	}
)
#: A till mode is a payment provider's when its name says so, or a driver drives it.
_PSP_MODE = re.compile(r"twint|terminal|carte|card|kart|payrexx|wallee|stripe|sumup", re.I)
_NOT_PSP = re.compile(r"cadeau|gift|gutschein|voucher|bon d'achat|avoir", re.I)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


@frappe.whitelist()
def get_overview() -> dict[str, Any]:
	frappe.only_for(ROLES)
	from payments.api import terminals

	return {
		"providers": _providers(),
		"shop": _shop(),
		"till": _till(),
		"terminals": terminals.overview(),
	}


def _providers() -> list[dict[str, Any]]:
	out = []
	if frappe.db.exists("Payment Provider", "payrexx"):
		from payments.api import payrexx_setup

		status = payrexx_setup.get_status()
		shop = next((r for r in status["rows"] if r["key"] == "shop"), {})
		out.append(
			{
				"key": "payrexx",
				"label": "Payrexx",
				"mode": status.get("mode"),
				"note": _("Shop: ready to choose")
				if shop.get("state") == "ok"
				else _("Shop: waiting for Payrexx")
				if shop.get("state") == "waiting"
				else _("Shop: not checked yet"),
				"route": "/app/payrexx-setup-wizard",
			}
		)
	wallee = _wallee_provider()
	if wallee:
		out.append(
			{
				"key": "wallee",
				"label": "Wallee",
				"mode": "live" if wallee.mode == "live" else "test",
				"note": _("Card and TWINT through your Wallee space"),
				"route": "/app/wallee-setup-wizard",
			}
		)
	if _stripe_accounts():
		out.append(
			{
				"key": "stripe",
				"label": "Stripe",
				"mode": "live",
				"note": _("Card on the shop"),
				"route": "/app/payment-gateway-account",
			}
		)
	if _twint_direct_account():
		out.append(
			{
				"key": "twint_direct",
				"label": _("TWINT direct"),
				"mode": "live",
				"note": _("Your TWINT contract, without an intermediary"),
				"route": "/app/twint-bridge-settings",
			}
		)
	return out


def _wallee_provider():  # noqa: ANN202
	"""The Wallee provider the setup page works on (the live one first), if enabled."""
	from payments.api.wallee_setup import _provider_name

	name = _provider_name()
	if not frappe.db.get_value("Payment Provider", name, "enabled"):
		return None
	return frappe.get_doc("Payment Provider", name)


def _stripe_accounts() -> list[str]:
	"""The shop company's Stripe gateway accounts."""
	company = shop_company()
	return frappe.get_all(
		"Payment Gateway Account",
		filters={"payment_gateway": ["like", "Stripe%"], "company": ["in", [company, "", None]]},
		pluck="name",
		order_by="creation asc",
	)


def _twint_direct_account() -> str | None:
	if not frappe.db.get_value("Payment Provider", "twint_migrated", "enabled"):
		return None
	return shop_gateway_account("Twint")


def _shop_rows() -> list[Any]:
	if not frappe.db.exists("DocType", "Webshop Settings"):
		return []
	return list(frappe.get_doc("Webshop Settings").get("payment_methods") or [])


def _shop_options() -> dict[str, list[dict[str, Any]]]:
	options: dict[str, list[dict[str, Any]]] = {m: [] for m in METHODS}

	if frappe.db.exists("Payment Provider", "payrexx"):
		from payments.api import payrexx_setup

		online = payrexx_setup._status().get("online") or {}
		for method, gateway in (("card", "Payrexx Carte"), ("twint", "Payrexx TWINT")):
			account = shop_gateway_account(gateway)
			ready = payrexx_setup.shop_can_take(online, method) and bool(account)
			if ready:
				reason = None
			elif online.get(method) and online.get("test_banner"):
				reason = _("Payrexx still shows a test-mode notice on its payment page")
			elif not account:
				reason = _("not prepared: open the Payrexx page and Connect")
			else:
				reason = _("online sales not opened by Payrexx yet")
			options[method].append(
				{
					"key": "payrexx",
					"label": "Payrexx",
					"account": account,
					"available": ready,
					"reason": reason,
					"dedicated": True,
				}
			)

	wallee = _wallee_provider()
	if wallee:
		from payments.api.wallee_setup import GATEWAY

		account = shop_gateway_account(GATEWAY)
		live = wallee.mode == "live"
		for method in METHODS:
			options[method].append(
				{
					"key": "wallee",
					"label": "Wallee",
					"account": account,
					"available": live and bool(account),
					"reason": None if live and account else _("in test mode"),
					# One Wallee tile shows every means of the space: it serves card and TWINT at once.
					"dedicated": False,
					"hint": _("Wallee's page offers every means of your Wallee space"),
				}
			)

	stripe = _stripe_accounts()
	if stripe:
		in_shop = {r.payment_gateway_account for r in _shop_rows()}
		account = next((a for a in stripe if a in in_shop), stripe[0])
		options["card"].append(
			{
				"key": "stripe",
				"label": "Stripe",
				"account": account,
				"available": True,
				"reason": None,
				"dedicated": True,
			}
		)

	twint = _twint_direct_account()
	if twint:
		options["twint"].append(
			{
				"key": "twint_direct",
				"label": _("TWINT direct"),
				"account": twint,
				"available": True,
				"reason": None,
				"dedicated": True,
			}
		)

	for method in METHODS:
		options[method].append(
			{
				"key": "none",
				"label": _("None"),
				"account": None,
				"available": True,
				"reason": None,
				"dedicated": True,
			}
		)
	return options


def _current(options: list[dict[str, Any]], in_shop: set[str]) -> dict[str, Any] | None:
	"""The option the shop shows for a means: its own tile first, else a tile shared with another."""
	offered = [o for o in options if o["account"] and o["account"] in in_shop]
	return next((o for o in offered if o.get("dedicated")), None) or next(iter(offered), None)


def _shop() -> dict[str, Any]:
	in_shop = {r.payment_gateway_account for r in _shop_rows()}
	out = {}
	for method, options in _shop_options().items():
		current = _current(options, in_shop)
		out[method] = {"options": options, "current": current["key"] if current else "none"}
	return out


def _psp_modes() -> set[str]:
	"""The modes that belong to a payment provider: the setup pages' till modes, the modes a
	driver drives, and the ones whose name says so. Cash, cheque, invoice and voucher are not."""
	from payments.api import payrexx_setup, wallee_setup

	modes = {
		m
		for m in (payrexx_setup._status().get("till_mode"), wallee_setup._status().get("till_mode"))
		if m
	}
	if frappe.db.exists("DocType", "POS Payment Driver Mapping"):
		modes.update(
			frappe.get_all("POS Payment Driver Mapping", pluck="mode_of_payment", distinct=True)
		)
	return modes


def _till() -> dict[str, list[dict[str, Any]]]:
	tills = frappe.get_all("POS Profile", filters={"disabled": 0}, pluck="name")
	offered: dict[str, set[str]] = {}
	for till in tills:
		for row in frappe.get_doc("POS Profile", till).payments:
			offered.setdefault(row.mode_of_payment, set()).add(till)

	psp = _psp_modes()
	parked = {key.split("::", 1)[1] for key in _parked_till()}
	candidates = {
		m
		for m in set(offered) | parked | psp
		if m in psp or (_PSP_MODE.search(m or "") and not _NOT_PSP.search(m or ""))
	}

	out: dict[str, list[dict[str, Any]]] = {"card": [], "twint": []}
	for mode in sorted(candidates):
		mode_type = frappe.db.get_value("Mode of Payment", mode, "type")
		if not mode_type or mode_type == "Cash":
			continue
		method = "twint" if "twint" in mode.lower() else "card"
		on = offered.get(mode, set())
		out[method].append(
			{
				"mode": mode,
				"state": "all" if tills and len(on) == len(tills) else "some" if on else "off",
				"tills": sorted(on),
			}
		)
	return out


# ---------------------------------------------------------------------------
# Choosing
# ---------------------------------------------------------------------------


# //// Neoffice — POST only (maintenance#1721): it changes what the shop offers.
@frappe.whitelist(methods=["POST"])
def set_shop_method(method: str, provider: str) -> dict[str, Any]:
	"""Make ``provider`` the one tile of ``method`` on the shop's payment step.

	One save: the rows are rebuilt in memory and written once. A choice that would leave
	the shop with no way to pay at all is refused — checkout would stop for every buyer.
	"""
	frappe.only_for(ROLES)
	if method not in METHODS:
		frappe.throw(_("Unknown payment means: {0}").format(method))
	all_options = _shop_options()
	options = all_options[method]
	chosen = next((o for o in options if o["key"] == provider), None)
	if not chosen:
		frappe.throw(_("Unknown provider: {0}").format(provider))
	if not chosen["available"]:
		frappe.throw(_("{0} cannot be chosen: {1}").format(chosen["label"], chosen["reason"]))

	settings = frappe.get_doc("Webshop Settings")
	before = list(settings.payment_methods)
	in_shop = {r.payment_gateway_account for r in before}
	# A tile another means still uses stays (Wallee's single tile serves card and TWINT).
	still_used = set()
	for other in (m for m in METHODS if m != method):
		current = _current(all_options[other], in_shop)
		if current:
			still_used.add(current["account"])

	leaving = {
		o["account"]
		for o in options
		if o["account"] and o["account"] != chosen["account"] and o["account"] not in still_used
	}
	parked = _parked()
	kept = []
	for row in before:
		if row.payment_gateway_account in leaving:
			parked.setdefault(row.payment_gateway_account, []).append(
				{k: v for k, v in row.as_dict().items() if k not in _ROW_META}
			)
		else:
			kept.append(row)
	settings.set("payment_methods", kept)

	account = chosen["account"]
	if account and account not in {r.payment_gateway_account for r in kept}:
		rows = parked.pop(account, None) or [{"payment_gateway_account": account}]
		for values in rows:
			row = settings.append("payment_methods", values)
			if provider == "payrexx":
				from payments.api import payrexx_setup

				payrexx_setup.apply_tile(row, payrexx_setup.payment_tiles()[method])

	if before and not settings.payment_methods:
		frappe.throw(
			_("The shop would be left with no way to pay: choose another provider for one means first.")
		)
	settings.flags.ignore_permissions = True
	settings.save()
	_save_parked(parked)
	frappe.db.commit()
	return get_overview()


# //// Neoffice — POST only (maintenance#1721): it changes what every till offers.
@frappe.whitelist(methods=["POST"])
def set_till_mode(mode: str, on: int | str) -> dict[str, Any]:
	"""Offer ``mode`` on every enabled till, or on none.

	A row taken off a till is parked with its settings (returns allowed, default) and put
	back as it was when the mode is offered again.
	"""
	frappe.only_for(ROLES)
	on = frappe.utils.cint(on)
	if not frappe.db.exists("Mode of Payment", mode):
		frappe.throw(_("Mode of Payment {0} does not exist").format(mode))
	if mode not in {m["mode"] for ms in _till().values() for m in ms}:
		frappe.throw(
			_("{0} is not a payment provider's mode: change it on the till itself.").format(mode)
		)
	accounts = {
		row.company
		for row in frappe.get_all(
			"Mode of Payment Account", filters={"parent": mode}, fields=["company"]
		)
	}
	parked = _parked_till()
	skipped = []
	for till in frappe.get_all("POS Profile", filters={"disabled": 0}, pluck="name"):
		profile = frappe.get_doc("POS Profile", till)
		rows = [r for r in profile.payments if r.mode_of_payment == mode]
		key = f"{till}::{mode}"
		if on and not rows:
			if profile.company not in accounts:
				skipped.append(till)
				continue
			values = parked.pop(key, None) or {"mode_of_payment": mode, "allow_in_returns": 0}
			values["default"] = 0  # the till's default stays the one it has
			profile.append("payments", values)
		elif not on and rows:
			if any(r.default for r in rows):
				frappe.throw(
					_("{0} is the default mode of till {1}: choose another default first").format(mode, till)
				)
			parked[key] = {k: v for k, v in rows[0].as_dict().items() if k not in _ROW_META}
			profile.set("payments", [r for r in profile.payments if r.mode_of_payment != mode])
		else:
			continue
		profile.flags.ignore_permissions = True
		profile.save()
	_save_parked_till(parked)
	frappe.db.commit()
	result = get_overview()
	result["skipped"] = skipped
	return result


def _parked() -> dict[str, list[dict[str, Any]]]:
	"""Parked shop rows, by gateway account; several rows per account (one per customer group)."""
	parked = read_state(PARKED_KEY)
	# The first version kept one row per account, as a dict.
	return {k: (v if isinstance(v, list) else [v]) for k, v in parked.items() if v}


def _save_parked(parked: dict[str, Any]) -> None:
	write_state(PARKED_KEY, parked)


def _parked_till() -> dict[str, dict[str, Any]]:
	return read_state(PARKED_TILL_KEY)


def _save_parked_till(parked: dict[str, Any]) -> None:
	write_state(PARKED_TILL_KEY, parked)
