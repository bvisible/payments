# //// Neoffice — added file (no upstream equivalent). Backend of the Wallee setup page, the twin of the
# //// Payrexx one (Jérémy, 2026-11-10: « the same thing for Wallee, the same elements »). Paste the space,
# //// the application user and its key: Neoffice saves Wallee Settings, binds the channels, lays down the
# //// ERP side (payments/setup/provider_setup.py) and sets up the webhook at Wallee itself, which Wallee's
# //// API allows (Payrexx's does not). Until then Wallee had no page for the shop at all, only a form.
# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""Wallee in one gesture: connect, prepare, set up the webhook, say test or production."""

from __future__ import annotations

import json
from typing import Any

import frappe
from frappe import _

ROLES = ("System Manager", "Accounts Manager")
STATUS_KEY = "wallee_setup_status"
PROVIDER_DRIVER = "payments.drivers.wallee.terminal_driver.WalleeTerminalDriver"
CHANNEL_DRIVERS = {
	"wallee_web": "payments.drivers.wallee.web_driver.WalleeWebDriver",
	"terminal": "payments.drivers.wallee.terminal_driver.WalleeTerminalDriver",
}
#: Wallee's id of the « Transaction » entity, for a webhook listener (Wallee's webhook documentation).
TRANSACTION_ENTITY = 1472041829003
GATEWAY = "Wallee"
WEBHOOK_NAME = "Neoffice"


def _site_lang() -> str:
	return frappe.db.get_single_value("System Settings", "language") or "en"


def _status() -> dict[str, Any]:
	try:
		return frappe.parse_json(frappe.db.get_default(STATUS_KEY) or "{}") or {}
	except (ValueError, TypeError):
		return {}


def _save_status(status: dict[str, Any]) -> None:
	frappe.db.set_default(STATUS_KEY, json.dumps(status, default=str))


def _provider_name() -> str:
	"""The Wallee provider of this site: the live one first, else any, else « wallee »."""
	return (
		frappe.db.get_value(
			"Payment Provider",
			{"driver_class": ["like", "payments.drivers.wallee.%"]},
			"name",
			order_by="enabled desc, mode asc",
		)
		or "wallee"
	)


def _settings_name(provider: str) -> str | None:
	return frappe.db.get_value("Wallee Settings", {"provider": provider}, "name")


def _webhook_url() -> str:
	return frappe.utils.get_url("/api/method/payments.api.webhook_wallee.handle")


# //// Neoffice — POST only (maintenance#1721): it saves credentials and acts at Wallee.
@frappe.whitelist(methods=["POST"])
def connect(space_id: str, user_id: str, authentication_key: str | None = None) -> dict[str, Any]:
	"""Save the access, prove it, bind the channels, lay down the ERP side, set up the webhook."""
	frappe.only_for(ROLES)
	space = frappe.utils.cint(str(space_id or "").strip())
	user = frappe.utils.cint(str(user_id or "").strip())
	if not space or not user:
		frappe.throw(_("The space and the application user are required"))

	name = _provider_name()
	if frappe.db.exists("Payment Provider", name):
		provider = frappe.get_doc("Payment Provider", name)
	else:
		provider = frappe.new_doc("Payment Provider")
		provider.provider_name = name
		provider.display_label = "Wallee"
		provider.driver_class = PROVIDER_DRIVER
		provider.mode = "test"
		provider.credentials_json = "{}"
	provider.enabled = 1
	provider.flags.ignore_permissions = True
	provider.save()

	settings_name = _settings_name(name)
	settings = (
		frappe.get_doc("Wallee Settings", settings_name)
		if settings_name
		else frappe.new_doc("Wallee Settings")
	)
	settings.provider = name
	settings.enabled = 1
	settings.space_id = space
	settings.user_id = user
	key = (authentication_key or "").strip()
	if key:
		settings.authentication_key = key
	elif not settings_name:
		frappe.throw(_("The authentication key is required"))
	settings.flags.ignore_permissions = True
	settings.save()
	frappe.db.commit()

	health = _health(provider)
	if not health.get("ok"):
		return {"ok": False, "error": health.get("error") or _("Connection failed"), **get_status()}

	_bind_channels(name)
	_prepare_erp()
	status = _status()
	status["connected_by"] = frappe.session.user
	status["connection"] = {"space_id": space, "checked_at": frappe.utils.now()}
	status["webhook"] = _ensure_webhook(provider)
	_save_status(status)
	frappe.db.commit()
	return {"ok": True, **get_status()}


def _health(provider) -> dict[str, Any]:  # noqa: ANN001
	from payments.drivers.wallee.provider import WalleeProvider

	result = WalleeProvider(provider).health_check()
	if not result.get("ok"):
		result["error"] = _wallee_message(result.get("error"))
	return result


def _wallee_message(error: Any) -> str:
	text = str(error or "")
	if "401" in text or "403" in text or "Unauthorized" in text or "Forbidden" in text:
		return _(
			"Wallee refused the access: check the application user, its key, and its role on the space."
		)
	if "404" in text:
		return _("Wallee does not know this space: check its number.")
	return text[:300] or _("Connection failed")


def _bind_channels(provider: str) -> None:
	from payments.setup.payment_channels import provision_payment_channels

	provision_payment_channels(codes=list(CHANNEL_DRIVERS))
	for channel, driver in CHANNEL_DRIVERS.items():
		existing = frappe.db.exists(
			"Provider Channel Settings", {"provider": provider, "channel": channel}
		)
		doc = (
			frappe.get_doc("Provider Channel Settings", existing)
			if existing
			else frappe.new_doc("Provider Channel Settings")
		)
		doc.provider = provider
		doc.channel = channel
		doc.driver_class = driver
		doc.enabled = 1
		doc.flags.ignore_permissions = True
		doc.save()


def _prepare_erp() -> dict[str, Any]:
	from payments.setup.provider_setup import (
		companies_in_use,
		ensure_clearing_account,
		ensure_gateway_account,
		ensure_till_mode,
	)

	accounts = {}
	for company in companies_in_use():
		account = ensure_clearing_account(company, _("Wallee clearing account", lang=_site_lang()))
		if account:
			accounts[company] = account
			ensure_gateway_account(GATEWAY, company, account)
	status = _status()
	mode = status.get("till_mode") or _("Wallee Terminal", lang=_site_lang())
	tills = ensure_till_mode(mode, accounts)
	status["till_mode"] = mode
	_save_status(status)
	return {"accounts": accounts, "tills": tills, "till_mode": mode}


def _ensure_webhook(provider) -> dict[str, Any]:  # noqa: ANN001
	"""The webhook URL and its Transaction listener at Wallee; found again, never doubled."""
	from wallee.models.creation_entity_state import CreationEntityState
	from wallee.models.webhook_listener_create import WebhookListenerCreate
	from wallee.models.webhook_url_create import WebhookUrlCreate
	from wallee.service.webhook_listeners_service import WebhookListenersService
	from wallee.service.webhook_urls_service import WebhookURLsService

	from payments.drivers.wallee.provider import WalleeProvider

	address = _webhook_url()
	try:
		wallee = WalleeProvider(provider)
		config = wallee.build_configuration()
		space = wallee.space_id
		urls = WebhookURLsService(config)
		existing = [u for u in (urls.get_webhooks_urls(space, limit=100).data or []) if u.url == address]
		url = (
			existing[0]
			if existing
			else urls.post_webhooks_urls(
				space, WebhookUrlCreate(name=WEBHOOK_NAME, url=address, state=CreationEntityState.ACTIVE)
			)
		)
		listeners = WebhookListenersService(config)
		mine = [
			item
			for item in (listeners.get_webhooks_listeners(space, limit=100).data or [])
			if _id_of(item.url) == url.id and _id_of(item.entity) == TRANSACTION_ENTITY
		]
		listener = (
			mine[0]
			if mine
			else listeners.post_webhooks_listeners(
				space,
				WebhookListenerCreate(
					name=WEBHOOK_NAME,
					entity=TRANSACTION_ENTITY,
					url=url.id,
					state=CreationEntityState.ACTIVE,
					notify_every_change=False,
				),
			)
		)
		return {"url_id": url.id, "listener_id": listener.id, "address": address}
	except Exception as exc:  # noqa: BLE001 - reported on the page; the poller settles payments meanwhile
		frappe.log_error("Wallee webhook setup failed", frappe.get_traceback())
		return {"error": _wallee_message(exc), "address": address}


def _id_of(value: Any) -> Any:
	"""A Wallee reference comes back as an id or as the object, depending on expansion."""
	return getattr(value, "id", value)


@frappe.whitelist()
def get_status() -> dict[str, Any]:
	frappe.only_for(ROLES)
	from payments.setup.provider_setup import till_mode_tills

	status = _status()
	name = _provider_name()
	provider = None
	if frappe.db.exists("Payment Provider", name):
		doc = frappe.get_doc("Payment Provider", name)
		settings = (
			frappe.get_doc("Wallee Settings", _settings_name(name)) if _settings_name(name) else None
		)
		provider = {
			"name": name,
			"enabled": bool(doc.enabled),
			"mode": doc.mode,
			"space_id": settings.space_id if settings else None,
			"user_id": settings.user_id if settings else None,
			"has_key": bool(settings and settings.authentication_key),
		}
	connection = status.get("connection") or {}
	webhook = status.get("webhook") or {}
	till_mode = status.get("till_mode")
	tills = till_mode_tills(till_mode) if till_mode else []
	clearing = frappe.db.get_value(
		"Account",
		{"account_name": _("Wallee clearing account", lang=_site_lang()), "is_group": 0},
		"name",
	)
	gateway_account = frappe.db.get_value(
		"Payment Gateway Account", {"payment_gateway": GATEWAY}, "name"
	)
	live = bool(provider and provider["mode"] == "live")
	rows = [
		{
			"key": "account",
			"state": "ok" if provider and connection else "todo",
			"detail": provider
			and provider.get("space_id")
			and _("Space {0}", lang=_site_lang()).format(provider["space_id"]),
		},
		{
			"key": "accounting",
			"state": "ok" if clearing and gateway_account else "todo",
			"detail": clearing,
		},
		{
			"key": "till",
			"state": "ok" if tills else "todo",
			"detail": ", ".join(tills),
			"till_mode": till_mode,
		},
		{"key": "shop", "state": ("ok" if live else "waiting") if gateway_account else "todo"},
		{
			"key": "webhook",
			"state": "ok" if webhook.get("listener_id") else "error" if webhook.get("error") else "todo",
			"detail": webhook.get("error"),
		},
	]
	return {
		"provider": provider,
		"rows": rows,
		"mode": ("live" if live else "test") if provider else "unknown",
		"webhook_url": _webhook_url(),
	}


# //// Neoffice — POST only (maintenance#1721): it changes how the provider is used.
@frappe.whitelist(methods=["POST"])
def set_mode(mode: str) -> dict[str, Any]:
	"""Production or test, as Neoffice uses it: terminals are created in that environment, and a
	provider in test cannot be chosen for the shop. Online payments follow the space's own setup."""
	frappe.only_for(ROLES)
	if mode not in ("test", "live"):
		frappe.throw(_("Mode must be 'test' or 'live'"))
	name = _provider_name()
	if not frappe.db.exists("Payment Provider", name):
		frappe.throw(_("Wallee is not configured yet"))
	if mode == "live" and not _health(frappe.get_doc("Payment Provider", name)).get("ok"):
		frappe.throw(_("Wallee does not answer: connect it again before going to production."))
	frappe.db.set_value("Payment Provider", name, "mode", mode)
	frappe.db.commit()
	return get_status()


# //// Neoffice — POST only (maintenance#1721): it acts at Wallee.
@frappe.whitelist(methods=["POST"])
def setup_webhook() -> dict[str, Any]:
	"""Set the webhook up again (after a failure or a change of address)."""
	frappe.only_for(ROLES)
	name = _provider_name()
	if not frappe.db.exists("Payment Provider", name):
		frappe.throw(_("Wallee is not configured yet"))
	status = _status()
	status["webhook"] = _ensure_webhook(frappe.get_doc("Payment Provider", name))
	_save_status(status)
	frappe.db.commit()
	return get_status()
