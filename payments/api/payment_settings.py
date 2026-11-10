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

The till side is the modes every enabled till offers, switched on or off for all of them.
"""

from __future__ import annotations

import json
from typing import Any

import frappe
from frappe import _

ROLES = ("System Manager", "Accounts Manager")
PARKED_KEY = "payment_settings_parked_rows"
METHODS = ("card", "twint")


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


@frappe.whitelist()
def get_overview() -> dict[str, Any]:
	frappe.only_for(ROLES)
	return {"providers": _providers(), "shop": _shop(), "till": _till()}


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
	name = frappe.db.get_value(
		"Payment Provider",
		{"enabled": 1, "driver_class": ["like", "payments.drivers.wallee.%"]},
		"name",
		order_by="mode desc",
	)
	return frappe.get_doc("Payment Provider", name) if name else None


def _stripe_accounts() -> list[str]:
	return frappe.get_all(
		"Payment Gateway Account", filters={"payment_gateway": ["like", "Stripe%"]}, pluck="name"
	)


def _twint_direct_account() -> str | None:
	if not frappe.db.get_value("Payment Provider", "twint_migrated", "enabled"):
		return None
	return frappe.db.get_value("Payment Gateway Account", {"payment_gateway": "Twint"}, "name")


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
			account = payrexx_setup._shop_gateway_account(gateway)
			ready = bool(online.get(method)) and bool(account)
			options[method].append(
				{
					"key": "payrexx",
					"label": "Payrexx",
					"account": account,
					"available": ready,
					"reason": None if ready else _("online sales not opened by Payrexx yet"),
				}
			)

	wallee = _wallee_provider()
	if wallee:
		account = frappe.db.get_value("Payment Gateway Account", {"payment_gateway": "Wallee"}, "name")
		live = wallee.mode == "live"
		for method in METHODS:
			options[method].append(
				{
					"key": "wallee",
					"label": "Wallee",
					"account": account,
					"available": live and bool(account),
					"reason": None if live and account else _("in test mode"),
				}
			)

	stripe = _stripe_accounts()
	if stripe:
		in_shop = {r.payment_gateway_account for r in _shop_rows()}
		account = next((a for a in stripe if a in in_shop), stripe[0])
		options["card"].append(
			{"key": "stripe", "label": "Stripe", "account": account, "available": True, "reason": None}
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
			}
		)

	for method in METHODS:
		options[method].append(
			{"key": "none", "label": _("None"), "account": None, "available": True, "reason": None}
		)
	return options


def _shop() -> dict[str, Any]:
	in_shop = {r.payment_gateway_account for r in _shop_rows()}
	out = {}
	for method, options in _shop_options().items():
		current = next((o["key"] for o in options if o["account"] and o["account"] in in_shop), "none")
		out[method] = {"options": options, "current": current}
	return out


def _till() -> dict[str, list[dict[str, Any]]]:
	tills = frappe.get_all("POS Profile", filters={"disabled": 0}, pluck="name")
	offered: dict[str, set[str]] = {}
	for till in tills:
		for row in frappe.get_doc("POS Profile", till).payments:
			offered.setdefault(row.mode_of_payment, set()).add(till)

	candidates = set(offered)
	from payments.api import payrexx_setup

	if payrexx_setup._status().get("till_mode"):
		candidates.add(payrexx_setup._status()["till_mode"])

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
	"""Make ``provider`` the one tile of ``method`` on the shop's payment step."""
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
	in_shop = {r.payment_gateway_account for r in settings.payment_methods}
	other = [m for m in METHODS if m != method]
	still_used = set()
	for m in other:
		current = next((o for o in all_options[m] if o["account"] and o["account"] in in_shop), None)
		if current:
			still_used.add(current["account"])

	leaving = {
		o["account"]
		for o in options
		if o["account"] and o["account"] != chosen["account"] and o["account"] not in still_used
	}
	parked = _parked()
	kept = []
	for row in settings.payment_methods:
		if row.payment_gateway_account in leaving:
			parked[row.payment_gateway_account] = {
				k: v
				for k, v in row.as_dict().items()
				if k
				not in (
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
				)
			}
		else:
			kept.append(row)
	settings.set("payment_methods", kept)

	if chosen["account"] and chosen["account"] not in {r.payment_gateway_account for r in kept}:
		if provider == "payrexx":
			from payments.api import payrexx_setup

			settings.flags.ignore_permissions = True
			settings.save()
			payrexx_setup._set_webshop_row(chosen["account"], payrexx_setup.payment_tiles()[method])
			settings = None
		else:
			settings.append(
				"payment_methods",
				parked.pop(chosen["account"], None) or {"payment_gateway_account": chosen["account"]},
			)
	if settings is not None:
		settings.flags.ignore_permissions = True
		settings.save()
	_save_parked(parked)
	frappe.db.commit()
	return get_overview()


# //// Neoffice — POST only (maintenance#1721): it changes what every till offers.
@frappe.whitelist(methods=["POST"])
def set_till_mode(mode: str, on: int | str) -> dict[str, Any]:
	"""Offer ``mode`` on every enabled till, or on none."""
	frappe.only_for(ROLES)
	on = frappe.utils.cint(on)
	if not frappe.db.exists("Mode of Payment", mode):
		frappe.throw(_("Mode of Payment {0} does not exist").format(mode))
	accounts = {
		row.company
		for row in frappe.get_all(
			"Mode of Payment Account", filters={"parent": mode}, fields=["company"]
		)
	}
	skipped = []
	for till in frappe.get_all("POS Profile", filters={"disabled": 0}, pluck="name"):
		profile = frappe.get_doc("POS Profile", till)
		rows = [r for r in profile.payments if r.mode_of_payment == mode]
		if on and not rows:
			if profile.company not in accounts:
				skipped.append(till)
				continue
			profile.append("payments", {"mode_of_payment": mode, "default": 0, "allow_in_returns": 0})
		elif not on and rows:
			if any(r.default for r in rows):
				frappe.throw(
					_("{0} is the default mode of till {1}: choose another default first").format(mode, till)
				)
			profile.set("payments", [r for r in profile.payments if r.mode_of_payment != mode])
		else:
			continue
		profile.flags.ignore_permissions = True
		profile.save()
	frappe.db.commit()
	result = get_overview()
	result["skipped"] = skipped
	return result


def _parked() -> dict[str, Any]:
	try:
		return frappe.parse_json(frappe.db.get_default(PARKED_KEY) or "{}") or {}
	except (ValueError, TypeError):
		return {}


def _save_parked(parked: dict[str, Any]) -> None:
	frappe.db.set_default(PARKED_KEY, json.dumps(parked, default=str))
