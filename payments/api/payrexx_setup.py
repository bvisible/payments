#//// Neoffice — added file (no upstream equivalent). Backend of the Payrexx setup
#//// wizard: read the current state, save the credentials, prove them against the live
#//// API, and only then create the Provider Channel Settings, the Payment Gateways, the
#//// Gateway Accounts and the Webshop Settings rows. Standing an account up by hand
#//// means six doctypes in the right order and fails silently when one is missed — a
#//// tile that never appears, or one that appears and cannot take money. Payrexx is our
#//// third provider (4c05756); upstream has no Payrexx anything.
#//// Commits: 754ddf4 2026-09-01 "feat(payrexx): a setup wizard, so standing an account up is five minutes not a runbook"
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
TILES: dict[str, dict[str, Any]] = {
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
		}

	channels = frappe.get_all(
		"Provider Channel Settings",
		filters={"provider": PROVIDER},
		fields=["channel", "enabled"],
	)

	tiles = []
	for key, spec in TILES.items():
		account = _account_name(spec["suffix"])
		if not account or not frappe.db.exists("Payment Gateway Account", account):
			continue
		row = _webshop_row(account)
		tiles.append({
			"key": key,
			"account": account,
			"in_shop": bool(row),
			"methods": (row or {}).get("restrict_payment_methods") or "",
			"inline": bool((row or {}).get("render_inline")),
		})

	return {"provider": provider, "channels": channels, "tiles": tiles}


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


@frappe.whitelist()
def save_credentials(
	instance: str, api_secret: str, pos_api_secret: str | None = None, mode: str = "test"
) -> dict[str, Any]:
	"""Create or update the Payment Provider.

	``pos_api_secret`` is only needed for the card terminal; a shop that sells
	online alone can leave it empty, and an empty value must not wipe one that is
	already stored — a wizard reopened to change the mode should not silently
	unpair the till.
	"""
	frappe.only_for(("System Manager", "Accounts Manager"))
	if mode not in ("test", "live"):
		frappe.throw(_("Mode must be 'test' or 'live'"))
	if not instance or not api_secret:
		frappe.throw(_("Instance and API secret are required"))

	if frappe.db.exists("Payment Provider", PROVIDER):
		doc = frappe.get_doc("Payment Provider", PROVIDER)
	else:
		doc = frappe.new_doc("Payment Provider")
		doc.provider_name = PROVIDER
		doc.display_label = "Payrexx"
		doc.driver_class = "payments.drivers.payrexx.provider.PayrexxProvider"

	existing = _read_credentials(doc)
	creds = {
		"instance": instance.strip(),
		"api_secret": api_secret.strip(),
		"pos_api_secret": (pos_api_secret or "").strip() or existing.get("pos_api_secret"),
	}
	doc.credentials_json = json.dumps({k: v for k, v in creds.items() if v}, indent=1)
	doc.mode = mode
	doc.enabled = 1
	doc.flags.ignore_permissions = True
	doc.save()
	frappe.db.commit()

	return {"ok": True, "provider": doc.name, "mode": doc.mode}


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

	return {
		"ok": bool(health.get("ok")),
		"instance": health.get("instance"),
		"mode": provider.mode,
		"payment_methods": health.get("active_payment_methods") or [],
		"providers": health.get("providers") or [],
	}


# ---------------------------------------------------------------------------
# Channels and tiles
# ---------------------------------------------------------------------------


@frappe.whitelist()
def setup_channels(web: bool = True, terminal: bool = False) -> dict[str, Any]:
	"""Bind Payrexx to the channels this deployment actually uses.

	Separate from the tiles because a channel is a capability and a tile is an
	offer: a shop may run the card terminal with no online payments at all.
	"""
	frappe.only_for(("System Manager", "Accounts Manager"))
	_require_verified_connection()

	drivers = {
		"payrexx_web": "payments.drivers.payrexx.web_driver.PayrexxWebDriver",
		"terminal": "payments.drivers.payrexx.terminal_driver.PayrexxTerminalDriver",
	}
	wanted = [c for c, on in (("payrexx_web", web), ("terminal", terminal)) if on]

	fait = []
	for channel in wanted:
		if not frappe.db.exists("Payment Channel", channel):
			frappe.throw(_("Payment Channel {0} does not exist").format(channel))
		name = frappe.db.exists(
			"Provider Channel Settings", {"provider": PROVIDER, "channel": channel}
		)
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

	frappe.db.commit()
	return {"ok": True, "channels": fait}


@frappe.whitelist()
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
	inconnu = [t for t in tiles if t not in TILES]
	if inconnu:
		frappe.throw(_("Unknown tiles: {0}").format(", ".join(inconnu)))

	modele = _template_account(currency)
	crees = []

	for key in tiles:
		spec = TILES[key]
		gateway = _gateway_name(spec["suffix"])

		if not frappe.db.exists("Payment Gateway", gateway):
			frappe.get_doc({"doctype": "Payment Gateway", "gateway": gateway}).insert(
				ignore_permissions=True
			)

		account = _account_name(spec["suffix"]) or ""
		if not account or not frappe.db.exists("Payment Gateway Account", account):
			if not modele:
				frappe.throw(
					_(
						"No Payment Gateway Account to copy from. Create one for "
						"Payrexx in {0} first, then run this again."
					).format(currency)
				)
			doc = frappe.copy_doc(modele)
			doc.payment_gateway = gateway
			doc.is_default = 0
			doc.insert(ignore_permissions=True)
			account = doc.name

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
	return frappe.db.exists(
		"Payment Gateway Account", {"payment_gateway": _gateway_name(suffix)}
	)


def _template_account(currency: str):  # noqa: ANN202
	"""An existing Payrexx account to copy — accounts, defaults and all."""
	name = frappe.db.exists(
		"Payment Gateway Account", {"payment_gateway": "Payrexx", "currency": currency}
	)
	return frappe.get_doc("Payment Gateway Account", name) if name else None


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
	row.use_payment_intent = 1
	row.restrict_payment_methods = ",".join(spec["methods"])
	row.render_inline = 1 if spec["inline"] else 0
	settings.flags.ignore_permissions = True
	settings.save()
