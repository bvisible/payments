# //// Neoffice — added file (no upstream equivalent). One clearing account per payment provider
# //// (decided 2026-11-13: accounts 1032, 1033 beside the others): what a bookkeeper reconciles is a
# //// payout, one provider at a time, so the gateways and the till modes of a provider book to an account of
# //// its own, never to another provider's. A site whose Payrexx and Wallee gateways booked to the Stripe
# //// account could not tell three payouts apart.
# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""Each payment provider books to a clearing account of its own.

A provider here is whoever pays the money out: Payrexx for its cards and its TWINT alike, Wallee,
Stripe, TWINT taken directly, PayPal. What books to a clearing account, per company:

- the provider's **gateways** (``Payment Gateway Account``: the shop, the payment links), named after
  it (« Payrexx TWINT » is Payrexx's: the provider a name names first);
- its **till modes** (``Mode of Payment Account``): a mode named after the provider that drives its
  terminals (``POS Payment Driver Mapping``) or that its setup page made (« Terminal Payrexx »). A mode
  named after no provider (« Carte de crédit ») may be a bank's terminal: it is never moved, but the
  account it books to is shared all the same.

An account is the provider's **own** when its name says so (« 1031 Stripe »), or when it names no
provider and nothing but this provider books to it. A gateway or a mode on an account that is not its
provider's own moves to the provider's own account: one it already books to, else one named after it
alone (« Compte d'attente Twint »), else « Compte d'attente <provider> », made beside the others with
the first free number (``provider_setup.ensure_clearing_account``). Whatever is on an own account stays
where the merchant or the bookkeeper put it. A company with no bank account gets no account, and
nothing of it moves.

Nothing already booked moves: the entries stay on the account they were posted to, for the bookkeeper
to reclass if they wish. Each change is said on the document it changed.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

import frappe
from frappe import _

#: The providers that pay money out, as a gateway, a till mode or an account names them.
PROVIDERS = ("Payrexx", "Wallee", "Stripe", "TWINT", "PayPal")
_NAMED = {p: re.compile(rf"(?<![a-z]){p.lower()}(?![a-z])") for p in PROVIDERS}
#: Where a provider's setup page keeps the till mode it made (payrexx_setup, wallee_setup).
SETUP_STATES = {"Payrexx": "payrexx_setup_status", "Wallee": "wallee_setup_status"}
GATEWAY = "Payment Gateway Account"
MODE = "Mode of Payment Account"


def provider_named_first(text: str | None) -> str | None:
	"""The provider a name names first, or None: « Payrexx TWINT » is Payrexx's."""
	text = (text or "").lower()
	found = [(match.start(), p) for p, pattern in _NAMED.items() if (match := pattern.search(text))]
	return min(found)[1] if found else None


def providers_named(text: str | None) -> set[str]:
	"""Every provider a name names."""
	text = (text or "").lower()
	return {p for p, pattern in _NAMED.items() if pattern.search(text)}


def label(provider: str, lang: str | None = None) -> str:
	"""The name of the account made for a provider that has none of its own, in the site's language."""
	lang = lang or frappe.db.get_single_value("System Settings", "language") or "en"
	# Literal calls, so that the extractor sees each one
	labels = {
		"Payrexx": _("Payrexx clearing account", lang=lang),
		"Wallee": _("Wallee clearing account", lang=lang),
		"Stripe": _("Stripe clearing account", lang=lang),
		"TWINT": _("TWINT clearing account", lang=lang),
		"PayPal": _("PayPal clearing account", lang=lang),
	}
	return labels[provider]


# ---------------------------------------------------------------------------
# What books to an account
# ---------------------------------------------------------------------------


@dataclass
class Booker:
	"""A gateway or a till mode of a company, and the account it books to."""

	doctype: str  # GATEWAY or MODE
	row: str  # the Payment Gateway Account, or the Mode of Payment Account row
	name: str  # the gateway or the mode, as people read it
	provider: str | None  # None: not a provider's (« Carte de crédit »), never moved
	account: str


def _state(key: str) -> dict:
	"""A setup page's state, read without moving it (provider_setup.read_state writes when it moves one)."""
	raw = frappe.db.get_global(key)
	if raw is None:
		raw = frappe.db.get_default(key)
	try:
		value = json.loads(raw or "{}")
	except (ValueError, TypeError):
		return {}
	return value if isinstance(value, dict) else {}


def _driver_provider(row) -> str | None:  # noqa: ANN001 - a Payment Provider row
	"""The provider a Payment Provider row drives (its driver class, else its name): « payments.drivers.wallee… »."""
	for text in (row.get("driver_class"), row.get("provider_name"), row.get("name")):
		text = (text or "").lower()
		for provider in PROVIDERS:
			if provider.lower() in text:
				return provider
	return None


def till_modes() -> dict[str, str]:
	"""{mode: provider} for the till modes that are a provider's: named after it, and driven by it or made by its setup page."""
	claims: dict[str, set[str]] = {}
	if frappe.db.exists("DocType", "POS Payment Driver Mapping") and frappe.db.exists(
		"DocType", "Payment Provider"
	):
		drivers = {
			row.name: _driver_provider(row)
			for row in frappe.get_all("Payment Provider", fields=["name", "provider_name", "driver_class"])
		}
		for row in frappe.get_all(
			"POS Payment Driver Mapping", filters={"enabled": 1}, fields=["mode_of_payment", "provider"]
		):
			if row.mode_of_payment and drivers.get(row.provider):
				claims.setdefault(row.mode_of_payment, set()).add(drivers[row.provider])
	for provider, key in SETUP_STATES.items():
		mode = _state(key).get("till_mode")
		if mode:
			claims.setdefault(mode, set()).add(provider)
	return {
		mode: provider_named_first(mode)
		for mode, providers in claims.items()
		if provider_named_first(mode) in providers
	}


def bookers(company: str, modes: dict[str, str] | None = None) -> list[Booker]:
	"""Every gateway and every till mode of the company, with the account it books to."""
	modes = till_modes() if modes is None else modes
	out = []
	if frappe.db.exists("DocType", GATEWAY):
		for row in frappe.get_all(
			GATEWAY,
			filters={"company": company},
			fields=["name", "payment_gateway", "payment_account"],
			order_by="payment_gateway",
		):
			if row.payment_account:
				out.append(
					Booker(
						GATEWAY,
						row.name,
						row.payment_gateway,
						provider_named_first(row.payment_gateway),
						row.payment_account,
					)
				)
	for row in frappe.get_all(
		MODE,
		filters={"company": company, "parenttype": "Mode of Payment"},
		fields=["name", "parent", "default_account"],
		order_by="parent",
	):
		if row.default_account:
			out.append(Booker(MODE, row.name, row.parent, modes.get(row.parent), row.default_account))
	return out


def _account_names(account: str) -> set[str]:
	return providers_named(
		"{} {}".format(account, frappe.db.get_value("Account", account, "account_name") or "")
	)


def is_own(account: str, provider: str | None, users: list[Booker]) -> bool:
	"""Whether ``account`` is ``provider``'s own, given everything (``users``) that books to it."""
	if not provider:
		return True
	named = _account_names(account)
	if named:
		return provider in named
	return all(user.provider == provider for user in users)


def belongs_to(company: str, account: str, provider: str | None) -> bool:
	"""Whether ``account`` is ``provider``'s own clearing account in ``company``, as things stand."""
	return is_own(account, provider, [b for b in bookers(company) if b.account == account])


# ---------------------------------------------------------------------------
# The plan, then the change
# ---------------------------------------------------------------------------


def _usable(account: str, company: str) -> bool:
	row = frappe.db.get_value("Account", account, ["company", "is_group", "disabled"], as_dict=True)
	return bool(row) and row.company == company and not row.is_group and not row.disabled


def _named_alone(company: str, provider: str) -> str | None:
	"""A usable account of the company that names this provider and no other (« Compte d'attente Twint »)."""
	for row in frappe.get_all(
		"Account",
		filters={"company": company, "is_group": 0, "disabled": 0},
		fields=["name", "account_name"],
		order_by="account_number, name",
	):
		if providers_named(f"{row.name} {row.account_name}") == {provider}:
			return row.name
	return None


def plan(company: str, modes: dict[str, str] | None = None) -> dict:
	"""What giving each provider its own account would change in ``company``. Reads, never writes.

	``{"company", "providers": {provider: {"target", "create", "moves", "kept"}}, "blocked": [provider]}``:
	``target`` is an existing account or None, ``create`` the account to make (its name, parent and
	number) when there is none, each move ``{doctype, row, name, from}``.
	"""
	from payments.setup import provider_setup

	everything = bookers(company, modes)
	by_account: dict[str, list[Booker]] = {}
	for booker in everything:
		by_account.setdefault(booker.account, []).append(booker)
	report = {"company": company, "providers": {}, "blocked": []}
	planned_numbers: set[str] = set()
	for provider in PROVIDERS:
		mine = [b for b in everything if b.provider == provider]
		if not mine:
			continue
		own = [b for b in mine if is_own(b.account, provider, by_account[b.account])]
		moving = [b for b in mine if b not in own]
		entry = {
			"target": None,
			"create": None,
			"moves": [
				{"doctype": b.doctype, "row": b.row, "name": b.name, "from": b.account} for b in moving
			],
			"kept": [{"doctype": b.doctype, "name": b.name, "account": b.account} for b in own],
		}
		report["providers"][provider] = entry
		if not moving:
			continue
		named_own = [b.account for b in own if provider in _account_names(b.account)]
		target = next(
			(a for a in named_own + [b.account for b in own] if _usable(a, company)), None
		) or _named_alone(company, provider)
		if not target:
			name = label(provider)
			target = frappe.db.get_value(
				"Account", {"company": company, "account_name": name, "is_group": 0, "disabled": 0}, "name"
			)
			if not target:
				slot = provider_setup.clearing_slot(company, taken=planned_numbers)
				if not slot:
					report["blocked"].append(provider)
					entry["moves"] = []
					continue
				if slot["account_number"]:
					planned_numbers.add(slot["account_number"])
				entry["create"] = {"account_name": name, **slot}
		entry["target"] = target
	return report


def companies() -> list[str]:
	"""The companies with a gateway or a till mode booked to an account."""
	names = set()
	if frappe.db.exists("DocType", GATEWAY):
		names |= set(frappe.get_all(GATEWAY, filters={"company": ["is", "set"]}, pluck="company"))
	names |= set(
		frappe.get_all(
			MODE, filters={"company": ["is", "set"], "parenttype": "Mode of Payment"}, pluck="company"
		)
	)
	return sorted(n for n in names if frappe.db.exists("Company", n))


def separate(dry_run: bool = False, only: list[str] | None = None) -> list[dict]:
	"""Give each provider its own clearing account, in every company (or ``only`` these); ``dry_run`` only says what it would do.

	Returns one report per company where something moves (or would), with the account made for each
	provider and what was moved onto it.
	"""
	from payments.setup import provider_setup

	modes = till_modes()
	reports = []
	for company in only or companies():
		report = plan(company, modes)
		if not any(entry["moves"] for entry in report["providers"].values()) and not report["blocked"]:
			continue
		reports.append(report)
		if dry_run:
			continue
		for provider, entry in report["providers"].items():
			if not entry["moves"]:
				continue
			target = entry["target"] or provider_setup.ensure_clearing_account(
				company, entry["create"]["account_name"]
			)
			if not target:
				continue
			entry["target"] = target
			currency = frappe.db.get_value("Account", target, "account_currency")
			for move in entry["moves"]:
				_move(move, target, currency)
			_follow_setup_state(provider, company, target)
	return reports


def _move(move: dict, target: str, currency: str | None) -> None:
	# In the site's language: bench migrate runs in English, the bookkeeper reads the note in theirs
	lang = frappe.db.get_single_value("System Settings", "language") or "en"
	note = _(
		"Clearing account changed from {0} to {1}: each payment provider books to an account of its own.",
		lang=lang,
	).format(move["from"], target)
	if move["doctype"] == GATEWAY:
		values = {"payment_account": target}
		if currency:
			values["currency"] = currency
		frappe.db.set_value(GATEWAY, move["row"], values, update_modified=False)
		frappe.get_doc(GATEWAY, move["row"]).add_comment("Info", note)
	else:
		frappe.db.set_value(MODE, move["row"], "default_account", target, update_modified=False)
		frappe.get_doc("Mode of Payment", move["name"]).add_comment("Info", note)


def _follow_setup_state(provider: str, company: str, target: str) -> None:
	"""The setup page shows the account it laid down: it now shows the provider's own."""
	key = SETUP_STATES.get(provider)
	if not key:
		return
	state = _state(key)
	accounts = state.get("accounts")
	if isinstance(accounts, dict) and company in accounts and accounts[company] != target:
		accounts[company] = target
		frappe.db.set_global(key, json.dumps(state, default=str))


def said(report: dict) -> str:
	"""One line per provider, for the migration's log."""
	lines = []
	for provider, entry in report["providers"].items():
		if not entry["moves"]:
			continue
		where = entry["target"] or "{} {} (new)".format(
			entry["create"].get("account_number") or "", entry["create"]["account_name"]
		)
		moved = ", ".join("{} ({})".format(m["name"], m["from"]) for m in entry["moves"])
		lines.append(f"{report['company']}: {provider} -> {where.strip()}: {moved}")
	for provider in report["blocked"]:
		lines.append(
			f"{report['company']}: {provider}: no bank account to put a clearing account beside, nothing moved"
		)
	return "\n".join(lines)
