# //// Neoffice — added file (no upstream equivalent). Backend of the Payrexx setup
# //// wizard: read the current state, save the credentials, prove them against the live
# //// API, and only then create the Provider Channel Settings, the Payment Gateways, the
# //// Gateway Accounts and the Webshop Settings rows. Standing an account up by hand
# //// means six doctypes in the right order and fails silently when one is missed — a
# //// tile that never appears, or one that appears and cannot take money. Payrexx is our
# //// third provider (4c05756); upstream has no Payrexx anything.
# //// Commits: 754ddf4 2026-09-01 "feat(payrexx): a setup wizard, so standing an account up is five minutes not a runbook"
# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""One-call setup for Payrexx — credentials, channels, and the shop tiles.

Standing a Payrexx account up by hand means touching six doctypes in the right
order: a Payment Provider with its credentials, a Provider Channel Settings per
channel, then, for every tile the shop should offer, a Payment Gateway, a Payment
Gateway Account, and a row in Webshop Settings. Miss one and the failure is
silent — a tile that never appears, or one that appears and cannot take money.

That sequence is the same on every deployment, so it belongs in code rather than
in a runbook. This module is what the setup wizard calls; each function is also
usable on its own from ``bench execute``, which is how it gets tested.

The design follows the Wallee wizard next door: read the current state, save the
credentials, prove they work against the live API, and only then create anything.
Nothing is created before the connection has been verified — a half-built setup
is worse than none, because it looks finished.
"""

from __future__ import annotations

import json
from typing import Any

import frappe
from frappe import _

PROVIDER = "payrexx"

#: The tiles a shop can offer, and what each one restricts itself to.
#:
#: Keyed by the suffix appended to the gateway name, because that name is what
#: the shopper reads and what the webshop derives its template from — its first
#: word has to stay "Payrexx" for the gateway type to resolve.
#:
#: ``inline`` decides whether the payment page is shown inside the checkout. Card
#: entry is a form and stays on the shop; TWINT hands over to the phone and
#: cannot do that from inside a frame.
# A FUNCTION, not a module constant: `_()` at module level runs at IMPORT, and a
# worker imports every app before it connects to a site. `frappe.cache` is None
# there, so each of these labels logged "Unable to load translations" with a bare
# AttributeError on every restart (neoffice-maintenance#325, #326) -- and any label
# that did come back was frozen in whatever language was active at import, for the
# lifetime of the process. Called per use, both go away.
#
# Named `payment_tiles` and not `tiles`: this module already has a local `tiles`
# list, which would shadow the function and turn every call into a TypeError.
def payment_tiles() -> dict[str, dict[str, Any]]:
	return {
		"card": {
			"suffix": "Carte",
			"methods": ["visa", "mastercard"],
			"inline": True,
			"label": _("Card"),
		},
		"twint": {
			"suffix": "TWINT",
			"methods": ["twint"],
			"inline": False,
			"label": "TWINT",
		},
		"all": {
			"suffix": "",
			"methods": [],
			"inline": False,
			"label": _("All methods"),
		},
	}


# ---------------------------------------------------------------------------
# Reading the current state
# ---------------------------------------------------------------------------


@frappe.whitelist()
def get_current_setup() -> dict[str, Any]:
	"""What is configured today — so the wizard opens on reality, not a blank form."""
	frappe.only_for(("System Manager", "Accounts Manager"))

	provider = None
	if frappe.db.exists("Payment Provider", PROVIDER):
		doc = frappe.get_doc("Payment Provider", PROVIDER)
		creds = _read_credentials(doc)
		provider = {
			"enabled": bool(doc.enabled),
			"mode": doc.mode,
			"instance": creds.get("instance"),
			# Never the secrets themselves — only whether they are there. A wizard
			# that echoes a key back into a form is a wizard that leaks it into
			# every browser cache and screenshot.
			"has_api_secret": bool(creds.get("api_secret")),
			"has_pos_secret": bool(creds.get("pos_api_secret")),
			"has_webhook_key": bool(creds.get("webhook_signing_key")),
		}

	channels = frappe.get_all(
		"Provider Channel Settings",
		filters={"provider": PROVIDER},
		fields=["channel", "enabled"],
	)

	tiles = []
	for key, spec in payment_tiles().items():
		account = _account_name(spec["suffix"])
		if not account or not frappe.db.exists("Payment Gateway Account", account):
			continue
		row = _webshop_row(account)
		tiles.append(
			{
				"key": key,
				"account": account,
				"in_shop": bool(row),
				"methods": (row or {}).get("restrict_payment_methods") or "",
				"inline": bool((row or {}).get("render_inline")),
			}
		)

	# //// Neoffice — the address to declare under Webhooks in the Payrexx back office. Shown by the
	# //// wizard because nothing else tells the merchant where Payrexx must post: without a webhook
	# //// a till payment is only learnt by the five-minute sweep, so the cashier waits that long.
	return {
		"provider": provider,
		"channels": channels,
		"tiles": tiles,
		"webhook_url": frappe.utils.get_url("/api/method/payments.api.webhook_payrexx.handle"),
	}


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


# //// Neoffice — POST only (maintenance#1721): Frappe checks no CSRF token on a GET, so writing the
# //// credentials, the channels or the tiles is never opened as an address. The wizard calls them
# //// with frappe.call (POST); reading the setup and testing the connection stay open.
@frappe.whitelist(methods=["POST"])
def save_credentials(
	instance: str,
	api_secret: str | None = None,
	pos_api_secret: str | None = None,
	mode: str = "test",
	webhook_signing_key: str | None = None,
) -> dict[str, Any]:
	"""Create or update the Payment Provider.

	Every secret field left empty keeps the value already stored — the wizard
	never sends a secret back to the browser, so a field shown empty means
	"unchanged", not "erase". A wizard reopened to change the mode must not
	silently unpair the till nor stop the webhooks.

	``pos_api_secret`` is only needed for the card terminal; a shop that sells
	online alone can leave it empty. ``webhook_signing_key`` is generated under
	Webhooks in the back office: without it every delivery is refused.
	"""
	frappe.only_for(("System Manager", "Accounts Manager"))
	doc = _stage_credentials(instance, api_secret, pos_api_secret, mode, webhook_signing_key)
	frappe.db.commit()
	return {"ok": True, "provider": doc.name, "mode": doc.mode}


def _stage_credentials(
	instance: str | None,
	api_secret: str | None = None,
	pos_api_secret: str | None = None,
	mode: str = "test",
	webhook_signing_key: str | None = None,
):  # noqa: ANN202 - a Payment Provider document
	"""Write the credentials into the transaction, without committing them."""
	if mode not in ("test", "live"):
		frappe.throw(_("Mode must be 'test' or 'live'"))

	if frappe.db.exists("Payment Provider", PROVIDER):
		doc = frappe.get_doc("Payment Provider", PROVIDER)
	else:
		doc = frappe.new_doc("Payment Provider")
		doc.provider_name = PROVIDER
		doc.display_label = "Payrexx"
		doc.driver_class = "payments.drivers.payrexx.provider.PayrexxProvider"

	# //// Neoffice — the stored credentials are the starting point, and only what was typed
	# //// replaces them. Rebuilding the dict from the three form fields dropped every other key —
	# //// the webhook signing key first, set outside the wizard — so reopening the wizard to switch
	# //// to live turned the webhooks off without a word. And the API secret was required again
	# //// although the form said « already stored — leave empty to keep it ».
	creds = dict(_read_credentials(doc))
	typed = {
		"instance": instance,
		"api_secret": api_secret,
		"pos_api_secret": pos_api_secret,
		"webhook_signing_key": webhook_signing_key,
	}
	for key, value in typed.items():
		value = (value or "").strip()
		if value:
			creds[key] = value
	# //// Neoffice — the address bar gives `https://shop.payrexx.com/`, Payrexx wants `shop`: the first
	# //// live client stored the address and every call went to a host that does not exist (2026-11-09).
	from payments.drivers.payrexx._common import instance_name

	creds["instance"] = instance_name(creds.get("instance"))
	if not creds.get("instance") or not creds.get("api_secret"):
		frappe.throw(_("Instance and API secret are required"))

	doc.credentials_json = json.dumps({k: v for k, v in creds.items() if v}, indent=1)
	doc.mode = mode
	doc.enabled = 1
	doc.flags.ignore_permissions = True
	doc.save()
	return doc


@frappe.whitelist()
def test_connection() -> dict[str, Any]:
	"""Ask Payrexx whether the credentials work, and what the account can take.

	The methods it answers with are what the tiles are built from, so a wrong key
	is caught here rather than three steps later as an empty payment page.
	"""
	frappe.only_for(("System Manager", "Accounts Manager"))
	from payments.drivers.payrexx._common import build_client

	if not frappe.db.exists("Payment Provider", PROVIDER):
		return {"ok": False, "error": _("Payrexx is not configured yet")}

	provider = frappe.get_doc("Payment Provider", PROVIDER)
	try:
		with build_client(provider) as client:
			health = client.health_check()
	except Exception as exc:  # noqa: BLE001 - the message is the whole point here
		return {"ok": False, "error": str(exc)[:300]}

	# //// Neoffice — `error` is passed on: the wizard showed « Connection failed » with nothing after
	# //// it, while Payrexx had said why (a wrong instance, a refused key).
	return {
		"ok": bool(health.get("ok")),
		"instance": health.get("instance"),
		"mode": provider.mode,
		"payment_methods": health.get("active_payment_methods") or [],
		"providers": health.get("providers") or [],
		"error": None if health.get("ok") else str(health.get("error") or "")[:300] or None,
	}


# ---------------------------------------------------------------------------
# Channels and tiles
# ---------------------------------------------------------------------------


# //// Neoffice — POST only (maintenance#1721): see save_credentials.
@frappe.whitelist(methods=["POST"])
def setup_channels(web: bool = True, terminal: bool = False) -> dict[str, Any]:
	"""Bind Payrexx to the channels this deployment actually uses.

	Separate from the tiles because a channel is a capability and a tile is an
	offer: a shop may run the card terminal with no online payments at all.
	"""
	frappe.only_for(("System Manager", "Accounts Manager"))
	_require_verified_connection()
	fait = _bind_channels(web=web, terminal=terminal)
	frappe.db.commit()
	return {"ok": True, "channels": fait}


def _bind_channels(web: bool = True, terminal: bool = False) -> list[str]:
	drivers = {
		"payrexx_web": "payments.drivers.payrexx.web_driver.PayrexxWebDriver",
		"terminal": "payments.drivers.payrexx.terminal_driver.PayrexxTerminalDriver",
	}
	wanted = [c for c, on in (("payrexx_web", web), ("terminal", terminal)) if on]

	# //// Neoffice — a site installed before the channel registry (7a0f7ca, 2026-09-03) never got
	# //// the `terminal` channel: neither a patch nor `after_install` created it there, and the
	# //// wizard stopped on « Payment Channel terminal does not exist » the day a shop wanted its
	# //// card terminal. The registry is idempotent, so the wizard asks it for what it needs.
	from payments.setup.payment_channels import provision_payment_channels

	provision_payment_channels(codes=wanted)

	fait = []
	for channel in wanted:
		if not frappe.db.exists("Payment Channel", channel):
			frappe.throw(_("Payment Channel {0} does not exist").format(channel))
		name = frappe.db.exists("Provider Channel Settings", {"provider": PROVIDER, "channel": channel})
		doc = (
			frappe.get_doc("Provider Channel Settings", name)
			if name
			else frappe.new_doc("Provider Channel Settings")
		)
		doc.provider = PROVIDER
		doc.channel = channel
		doc.driver_class = drivers[channel]
		doc.enabled = 1
		doc.flags.ignore_permissions = True
		doc.save()
		fait.append(doc.name)
	return fait


# //// Neoffice — POST only (maintenance#1721): see save_credentials.
@frappe.whitelist(methods=["POST"])
def setup_tiles(tiles: list[str] | str, currency: str = "CHF") -> dict[str, Any]:
	"""Create the shop tiles: gateway, account, and the row that makes them visible.

	Three objects per tile, and all three are needed — this is exactly the
	sequence that is easy to get half-right by hand. The Payment Gateway carries
	the name the shopper reads, the Account binds it to a currency, and the
	Webshop Settings row is what actually puts it on the payment step.

	The gateway name always begins with "Payrexx" because the webshop derives the
	gateway type from its first word; "TWINT" alone would send it looking for a
	TWINT template that has nothing to do with Payrexx.
	"""
	frappe.only_for(("System Manager", "Accounts Manager"))
	_require_verified_connection()

	if isinstance(tiles, str):
		tiles = frappe.parse_json(tiles)
	inconnu = [t for t in tiles if t not in payment_tiles()]
	if inconnu:
		frappe.throw(_("Unknown tiles: {0}").format(", ".join(inconnu)))

	# //// Neoffice — the gateways and their accounts are laid down by `_prepare_erp` (clearing account
	# //// included). The tiles used to be copied from a « Payrexx » gateway account nobody created: on a
	# //// site that never had Payrexx the step died on « No Payment Gateway Account to copy from ».
	_prepare_erp()
	crees = []

	for key in tiles:
		spec = payment_tiles()[key]
		gateway = _gateway_name(spec["suffix"])
		account = _shop_gateway_account(gateway)
		if not account:
			frappe.throw(_("No Payment Gateway Account for {0}").format(gateway))
		_set_webshop_row(account, spec)
		crees.append({"tile": key, "gateway": gateway, "account": account})

	frappe.db.commit()
	return {"ok": True, "tiles": crees}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_credentials(doc) -> dict[str, Any]:  # noqa: ANN001
	try:
		return frappe.parse_json(doc.get("credentials_json") or "{}") or {}
	except (ValueError, TypeError):
		return {}


def _require_verified_connection() -> None:
	"""Refuse to build anything on credentials that have not been proven.

	A setup that looks complete but cannot reach the provider is worse than an
	empty one: nobody goes back to check a wizard that said it was done.
	"""
	resultat = test_connection()
	if not resultat.get("ok"):
		frappe.throw(
			_("Payrexx connection failed, nothing was created: {0}").format(
				resultat.get("error") or _("unknown error")
			)
		)


def _gateway_name(suffix: str) -> str:
	return f"Payrexx {suffix}".strip() if suffix else "Payrexx"


def _account_name(suffix: str) -> str | None:
	"""The account ERPNext will have named for this gateway, whatever its suffix.

	Looked up rather than guessed: ERPNext appends the currency and sometimes the
	company abbreviation, and the exact shape differs between deployments.
	"""
	return frappe.db.exists("Payment Gateway Account", {"payment_gateway": _gateway_name(suffix)})


def _webshop_row(account: str) -> dict[str, Any] | None:
	try:
		settings = frappe.get_cached_doc("Webshop Settings")
	except Exception:  # noqa: BLE001 - webshop may not be installed
		return None
	for row in settings.get("payment_methods") or []:
		if row.payment_gateway_account == account:
			return row.as_dict()
	return None


def _set_webshop_row(account: str, spec: dict[str, Any]) -> None:
	"""Put the tile on the payment step, with its restriction and its rendering."""
	if not frappe.db.exists("DocType", "Webshop Settings"):
		return
	settings = frappe.get_doc("Webshop Settings")
	row = next(
		(r for r in (settings.payment_methods or []) if r.payment_gateway_account == account),
		None,
	)
	if not row:
		row = settings.append("payment_methods", {"payment_gateway_account": account})
	apply_tile(row, spec)
	settings.flags.ignore_permissions = True
	settings.save()


def apply_tile(row, spec: dict[str, Any]) -> None:  # noqa: ANN001 - a Webshop Payment Method row
	"""What makes a shop row a Payrexx tile: the intent, the means it shows, inline or not."""
	row.use_payment_intent = 1
	row.restrict_payment_methods = ",".join(spec["methods"])
	row.render_inline = 1 if spec["inline"] else 0


# ---------------------------------------------------------------------------
# One gesture: paste the key, everything else is laid down
# ---------------------------------------------------------------------------
# //// Neoffice — the setup page asked six decisions of a merchant who wanted to take money: channels,
# //// tiles, mode, then accounts and a till mode by hand. The first live client (2026-11-09) showed
# //// every one of them failing in its own way. `connect` now does the ERP side alone, and reads the
# //// real state of the Payrexx account off a test payment page instead of trusting the API.

STATUS_KEY = "payrexx_setup_status"

#: The Payrexx account answers « active » for a method its contract does not cover
#: online; only a payment page shows what a shopper is really offered. These are the
#: identifiers that page uses for the methods the shop tiles are built from.
CARD_METHODS = ("visa", "mastercard")


def _site_lang() -> str:
	return frappe.db.get_single_value("System Settings", "language") or "en"


def _clearing_label() -> str:
	return _("Payrexx clearing account", lang=_site_lang())


def _till_mode() -> str:
	"""The till mode's name: the one created before, else the enabled mode a till already drives
	Payrexx terminals with, else the site's word for it (a mode made by hand under that name is reused)."""
	from payments.setup.provider_setup import driven_mode

	return (
		_status().get("till_mode") or driven_mode(PROVIDER) or _("Payrexx Terminal", lang=_site_lang())
	)


def _status() -> dict[str, Any]:
	from payments.setup.provider_setup import read_state

	return read_state(STATUS_KEY)


def _save_status(status: dict[str, Any]) -> None:
	from payments.setup.provider_setup import write_state

	write_state(STATUS_KEY, status)


def _prepare_erp() -> dict[str, Any]:
	"""Clearing account, gateways and their accounts, channels, the till mode. Idempotent."""
	from payments.setup.provider_setup import (
		companies_in_use,
		ensure_clearing_account,
		ensure_gateway_account,
		ensure_till_mode,
	)

	_bind_channels(web=True, terminal=True)
	# A company with no bank account gets no clearing account, and so no gateway nor till mode:
	# it is skipped, the others are still laid down.
	accounts = {}
	for company in companies_in_use():
		account = ensure_clearing_account(company, _clearing_label(), gateway=_gateway_name(""))
		if account:
			accounts[company] = account
	for company, account in accounts.items():
		for spec in payment_tiles().values():
			ensure_gateway_account(_gateway_name(spec["suffix"]), company, account)
	mode = _till_mode()
	till = ensure_till_mode(mode, accounts)
	status = _status()
	status["till_mode"] = mode
	status["accounts"] = accounts
	status["till_errors"] = till["errors"]
	_save_status(status)
	return {"accounts": accounts, "tills": till["tills"], "till_mode": mode, "errors": till["errors"]}


def _shop_gateway_account(gateway: str) -> str | None:
	"""The gateway's account for the shop's company, never another company's."""
	from payments.setup.provider_setup import shop_gateway_account

	return shop_gateway_account(gateway)


# //// Neoffice — POST only (maintenance#1721): see save_credentials.
@frappe.whitelist(methods=["POST"])
def connect(
	instance: str, api_secret: str | None = None, webhook_signing_key: str | None = None
) -> dict[str, Any]:
	"""Save the key, prove it, lay everything down, then read what the account really offers.

	Nothing reaches the shop: the tiles are prepared, the merchant chooses them in
	Payment settings. The till gets its mode for a terminal used on its own.
	"""
	frappe.only_for(("System Manager", "Accounts Manager"))
	existing_mode = frappe.db.get_value("Payment Provider", PROVIDER, "mode")
	# //// Neoffice — the typed key is proven BEFORE it replaces the stored one. Saving first and testing
	# //// after meant a mistyped key overwrote a working one: the shop and the till stopped taking money
	# //// until someone found the right key again. A refused key now leaves everything as it was.
	_stage_credentials(
		instance=instance,
		api_secret=api_secret,
		mode=existing_mode or "live",
		webhook_signing_key=webhook_signing_key,
	)
	connection = test_connection()
	if not connection.get("ok"):
		frappe.db.rollback()
		frappe.clear_document_cache("Payment Provider", PROVIDER)
		return {
			"ok": False,
			"error": _("{0} — nothing was changed.").format(
				connection.get("error") or _("Connection failed")
			),
			**get_status(),
		}

	_prepare_erp()
	status = _status()
	status["connected_by"] = frappe.session.user
	status["connection"] = {
		"instance": connection.get("instance"),
		"providers": connection.get("providers") or [],
		"methods": connection.get("payment_methods") or [],
	}
	status["online"] = _merge_probe(status.get("online"), _probe_online())
	_save_status(status)
	frappe.db.commit()
	return {"ok": True, **get_status()}


@frappe.whitelist()
def get_status() -> dict[str, Any]:
	"""The checklist the setup page shows: one row per part, each with its state."""
	frappe.only_for(("System Manager", "Accounts Manager"))
	from payments.setup.provider_setup import till_mode_tills

	status = _status()
	provider = None
	if frappe.db.exists("Payment Provider", PROVIDER):
		doc = frappe.get_doc("Payment Provider", PROVIDER)
		creds = _read_credentials(doc)
		provider = {
			"enabled": bool(doc.enabled),
			"mode": doc.mode,
			"instance": creds.get("instance"),
			"has_api_secret": bool(creds.get("api_secret")),
			"has_webhook_key": bool(creds.get("webhook_signing_key")),
		}
	connection = status.get("connection") or {}
	online = status.get("online") or {}
	till_mode = status.get("till_mode")
	tills = till_mode_tills(till_mode) if till_mode else []
	accounts = status.get("accounts") or {}
	clearing = ", ".join(a for a in accounts.values() if frappe.db.exists("Account", a)) or None
	terminals = (
		frappe.get_all(
			"POS Payment Driver Mapping",
			filters={"provider": PROVIDER, "channel": "terminal", "enabled": 1},
			fields=["pos_profile", "default_device"],
		)
		if frappe.db.exists("DocType", "POS Payment Driver Mapping")
		else []
	)

	rows = [
		{
			"key": "account",
			"state": "ok" if provider and connection else "todo",
			"detail": connection.get("instance")
			and " · ".join([connection["instance"], ", ".join(connection.get("methods") or [])]),
		},
		{"key": "accounting", "state": "ok" if clearing else "todo", "detail": clearing},
		{
			"key": "till",
			"state": "ok" if tills else "todo",
			"detail": ", ".join(tills),
			"till_mode": till_mode,
			"linked": terminals,
			"errors": status.get("till_errors") or {},
		},
		{
			"key": "shop",
			"state": _shop_state(online),
			"detail": ", ".join(online.get("methods") or []),
			"checked_at": online.get("checked_at"),
			"error": online.get("error"),
			"test_banner": bool(online.get("test_banner")),
		},
		{
			"key": "webhook",
			"state": "ok" if provider and provider["has_webhook_key"] else "optional",
		},
	]
	return {
		"provider": provider,
		"rows": rows,
		"mode": _mode_state(connection, online),
		"online": online,
		"webhook_url": frappe.utils.get_url("/api/method/payments.api.webhook_payrexx.handle"),
		"back_office": f"https://{provider['instance']}.payrexx.com/cadmin/"
		if provider and provider.get("instance")
		else None,
	}


def _shop_state(online: dict[str, Any]) -> str:
	if not online.get("checked_at"):
		return "error" if online.get("error") else "todo"
	if shop_can_take(online, "card") or shop_can_take(online, "twint"):
		return "ok"
	return "waiting"


def shop_can_take(online: dict[str, Any], method: str) -> bool:
	"""Whether a shopper on Payrexx's page is really offered ``method``, in production.

	The page carries a « test mode » notice while any provider of the account is in test:
	a method shown next to it may be the test one, so it is not offered to real buyers.
	"""
	return bool(online.get(method)) and not online.get("test_banner")


def _mode_state(connection: dict[str, Any], online: dict[str, Any]) -> str:
	"""live · test · mixed · unknown, as the merchant must read it.

	« mixed » is the common first state: Payrexx Pay already takes money on the
	terminals while the shop is not open yet, or one provider is still in test.
	"""
	if not online.get("checked_at"):
		return "unknown"
	takes_online = online.get("card") or online.get("twint")
	if online.get("test_banner"):
		return (
			"mixed" if takes_online or "Payrexx Pay" in (connection.get("providers") or []) else "test"
		)
	if takes_online:
		return "live"
	return "mixed" if "Payrexx Pay" in (connection.get("providers") or []) else "unknown"


# //// Neoffice — POST only (maintenance#1721): see save_credentials.
@frappe.whitelist(methods=["POST"])
def check_online() -> dict[str, Any]:
	"""« Check now »: read a test payment page again."""
	frappe.only_for(("System Manager", "Accounts Manager"))
	status = _status()
	status["online"] = _merge_probe(status.get("online"), _probe_online())
	_save_status(status)
	frappe.db.commit()
	return get_status()


def _merge_probe(before: dict[str, Any] | None, now: dict[str, Any]) -> dict[str, Any]:
	"""A failed reading keeps the last good one and says it failed; a good one replaces it."""
	if not now.get("error"):
		return now
	kept = {k: v for k, v in (before or {}).items() if k not in ("error", "error_at")}
	kept["error"] = now["error"]
	kept["error_at"] = now.get("error_at")
	return kept


def _probe_online() -> dict[str, Any]:
	"""Create a CHF 1.00 Payrexx payment page, read what it offers, delete it.

	Nothing can be paid: the page is deleted before this returns. Its HTML names each
	offered method in a lower-case ``alt`` (``visa``, ``twint``, ``pre_payment`` …) and
	carries a « test mode » banner while a provider of the account is in test.
	"""
	import re

	import requests

	from payments.drivers.payrexx._common import build_client

	checked_at = frappe.utils.now()

	def failed(reason: str) -> dict[str, Any]:
		return {"error": reason[:200], "error_at": checked_at}

	try:
		provider = frappe.get_doc("Payment Provider", PROVIDER)
		with build_client(provider) as client:
			gateway = client.gateway.create(
				amount=100, currency="CHF", purpose=_("Neoffice check, nothing is charged")
			)
			try:
				# English, so the test-mode notice is read in the language this code looks for.
				response = requests.get(
					gateway.link,
					headers={"User-Agent": "Mozilla/5.0 Neoffice", "Accept-Language": "en"},
					timeout=20,
				)
				status_code, html = response.status_code, response.text
			finally:
				try:
					client.gateway.delete(gateway.id)
				except Exception:  # noqa: BLE001 - the page cannot be paid anyway; never hide the reading
					frappe.log_error("Payrexx online check: page not deleted", frappe.get_traceback())
	except Exception as exc:  # noqa: BLE001 - reported on the page, never raised into it
		frappe.log_error("Payrexx online check failed", frappe.get_traceback())
		return failed(str(exc))

	if status_code != 200:
		return failed(f"Payrexx page answered HTTP {status_code}")
	methods = sorted(set(re.findall(r'alt="([a-z][a-z0-9_-]{1,40})"', html)))
	if not methods:
		# An unreadable page (a new layout, a block) must not read as « Payrexx offers nothing ».
		return failed("no payment method found on the Payrexx page")
	return {
		"checked_at": checked_at,
		"methods": methods,
		"card": all(m in methods for m in CARD_METHODS),
		"twint": "twint" in methods,
		"test_banner": "test mode" in html.lower(),
	}


def daily_online_check() -> None:
	"""Scheduler, daily: once Payrexx opens the shop, say so by e-mail. Nothing else changes."""
	if not frappe.db.get_value("Payment Provider", PROVIDER, "enabled"):
		return
	status = _status()
	before = status.get("online") or {}
	if shop_can_take(before, "card") and shop_can_take(before, "twint"):
		return
	now = _merge_probe(before, _probe_online())
	status["online"] = now
	opened = shop_can_take(now, "card") or shop_can_take(now, "twint")
	recipient = status.get("connected_by")
	# Said once: a later failed reading followed by a good one must not send it again.
	notify = opened and not status.get("opened_notified")
	if opened:
		status["opened_notified"] = 1
	_save_status(status)
	frappe.db.commit()
	if notify and recipient and frappe.db.get_value("User", recipient, "enabled"):
		frappe.sendmail(
			recipients=[recipient],
			subject=_("Payrexx: your shop can now take payments"),
			message=_(
				"Payrexx now offers {0} on your shop's payment page. Nothing has changed in your shop yet: "
				"choose in Payment settings what Payrexx takes."
			).format(", ".join(now.get("methods") or []))
			+ f'<p><a href="{frappe.utils.get_url("/app/payment-settings")}">{_("Open Payment settings")}</a></p>',
		)


# //// Neoffice — POST only (maintenance#1721): see save_credentials.
@frappe.whitelist(methods=["POST"])
def link_terminal(serial_number: str, pairing_code: str, pos_profile: str) -> dict[str, Any]:
	"""Pair a terminal and wire it to a till: device, mapping, active device.

	Refused without the webhook signing key: a linked till learns the end of a
	payment from the webhook; without it the cashier waits for the five-minute sweep.
	"""
	frappe.only_for(("System Manager", "Accounts Manager"))
	serial = (serial_number or "").strip().upper()
	code = (pairing_code or "").strip().upper()
	if not serial or not code:
		frappe.throw(_("Serial number and pairing code are required"))
	if not frappe.db.exists("POS Profile", pos_profile):
		frappe.throw(_("Till {0} does not exist").format(pos_profile))
	creds = _read_credentials(frappe.get_doc("Payment Provider", PROVIDER))
	if not creds.get("webhook_signing_key"):
		frappe.throw(
			_(
				"Set up instant confirmations (the webhook) first: a linked till needs them to see a payment end."
			)
		)

	# //// Neoffice — the ERP side is written first and the terminal paired last: a pairing done at
	# //// Payrexx before a refused database write left a terminal paired to nothing in the till. Any
	# //// failure below, the pairing included, rolls the whole request back.
	prepared = _prepare_erp()
	mode = prepared["till_mode"]
	if pos_profile not in prepared["tills"] and not _offer_mode_on(pos_profile, mode):
		frappe.throw(
			_("Till {0} cannot offer {1}: {2}").format(
				pos_profile, mode, prepared["errors"].get(pos_profile) or _("no account for its company")
			)
		)
	device = _ensure_device(serial)
	from payments.setup.provider_setup import wire_terminal

	wire_terminal(pos_profile, mode, device, PROVIDER)

	from payments.drivers.registry import resolve_driver

	driver = resolve_driver(PROVIDER, "terminal")
	pairing = driver.pair_terminal(serial, code, cashier_name=pos_profile)
	if not (pairing or {}).get("paired"):
		frappe.throw(
			_(
				"Payrexx did not pair terminal {0}: check the code on its screen (it changes when you leave that screen)."
			).format(serial)
		)
	frappe.db.set_value("Payment Device", device, "status", "online", update_modified=False)
	frappe.db.commit()
	return {"ok": True, "device": device, "pos_profile": pos_profile, "pairing": pairing}


def _offer_mode_on(pos_profile: str, mode: str) -> bool:
	from payments.setup.provider_setup import offer_mode_on

	return offer_mode_on(pos_profile, mode)


def _ensure_device(serial: str) -> str:
	pcs = frappe.db.get_value(
		"Provider Channel Settings", {"provider": PROVIDER, "channel": "terminal"}, "name"
	)
	device = frappe.db.get_value("Payment Device", {"serial_number": serial}, "name")
	if device:
		frappe.db.set_value(
			"Payment Device",
			device,
			{"provider_channel_settings": pcs, "enabled": 1},
			update_modified=False,
		)
		return device
	doc = frappe.get_doc(
		{
			"doctype": "Payment Device",
			"device_label": f"Payrexx {serial}",
			"serial_number": serial,
			"provider_channel_settings": pcs,
			"enabled": 1,
			"status": "unknown",
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


# //// Neoffice — POST only (maintenance#1721): see save_credentials.
@frappe.whitelist(methods=["POST"])
def save_webhook_key(webhook_signing_key: str) -> dict[str, Any]:
	"""Store the webhook signing key alone, the rest of the credentials untouched."""
	frappe.only_for(("System Manager", "Accounts Manager"))
	key = (webhook_signing_key or "").strip()
	if not key:
		frappe.throw(_("The signing key is empty"))
	if not frappe.db.exists("Payment Provider", PROVIDER):
		frappe.throw(_("Payrexx is not configured yet"))
	doc = frappe.get_doc("Payment Provider", PROVIDER)
	creds = dict(_read_credentials(doc))
	creds["webhook_signing_key"] = key
	doc.credentials_json = json.dumps(creds, indent=1)
	doc.flags.ignore_permissions = True
	doc.save()
	frappe.db.commit()
	return get_status()
