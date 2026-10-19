# //// Neoffice — added file (no upstream equivalent). The till's and the shop's TWINT calls run on the instance's own
# //// engine (payments.twint_engine, Python) instead of the hub's PHP bridge, merchant by merchant, behind the site's
# //// `twint_local_engine` switch (maintenance#1721). The engine needs the merchant's .p12, which until now lived on
# //// the hub alone: the instance keeps it encrypted with the site's key (Frappe's __Auth table, so it is in the
# //// database backups), takes it from the form on upload, and fetches the ones already on the hub once, proving it
# //// holds the password that opens them. No certificate here and none to fetch: the call goes to the hub as before.
# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""The instance's TWINT engine, as seen from the driver."""

from __future__ import annotations

import base64
import logging
import time

import frappe

DOCTYPE = "Twint Bridge Settings"
CERTIFICATE_FIELD = (
	"p12_certificate_data"  # an __Auth entry of the merchant's record, not a field of the form
)
MAX_LOCAL_CERTIFICATE = (
	32 * 1024
)  # a .p12 is a few KB; __Auth holds 64 KB once encrypted and base64-encoded
FETCH_METHOD = "neoffice_devops.api.twint.fetch_certificate"
FETCH_PAUSE = (
	10 * 60
)  # seconds before asking the hub again after it gave nothing: each payment would pay for it


def _refused_key(merchant_uuid: str) -> str:
	return f"twint_engine_fetch_refused:{merchant_uuid}"


def runs_locally(merchant_uuid: str) -> bool:
	"""Whether this merchant's calls run on the instance's engine: `twint_local_engine` in site_config.json.

	Absent, false, "off" or "hub": the hub's bridge (the default until the engine has been proven with real money).
	True, "all" or "on": every merchant. A list (["uuid", ...] or "uuid, uuid"): only those merchants.
	"""
	setting = frappe.conf.get("twint_local_engine")
	if setting is None or setting is False or setting == 0:
		return False
	if setting is True or setting == 1:
		return True
	if isinstance(setting, str):
		text = setting.strip().lower()
		if text in ("all", "true", "1", "on", "yes"):
			return True
		if text in ("", "off", "false", "0", "no", "hub", "none"):
			return False
		setting = setting.split(",")
	if isinstance(setting, (list, tuple)):
		return (merchant_uuid or "").strip() in {str(name).strip() for name in setting}
	return False


def soap_version() -> str:
	"""TWINT's service version the engine speaks: `twint_soap_version`, "8.7" (the hub's bridge) unless set to "10"."""
	return str(frappe.conf.get("twint_soap_version") or "8.7")


# ----------------------------------------------------------------------------------------------------------------------
# The certificate on the instance
# ----------------------------------------------------------------------------------------------------------------------


def stored_certificate(merchant_uuid: str) -> bytes | None:
	from frappe.utils.password import get_decrypted_password

	encoded = get_decrypted_password(DOCTYPE, merchant_uuid, CERTIFICATE_FIELD, raise_exception=False)
	if not encoded:
		return None
	try:
		return base64.b64decode(encoded, validate=True)
	except Exception:  # noqa: BLE001 — an entry that is not base64 is as good as none
		return None


def store_certificate(merchant_uuid: str, raw: bytes) -> bool:
	"""Keep the merchant's .p12 on this instance, encrypted with the site's key. False when it is too large."""
	from frappe.utils.password import set_encrypted_password

	if not raw or len(raw) > MAX_LOCAL_CERTIFICATE:
		return False
	set_encrypted_password(DOCTYPE, merchant_uuid, base64.b64encode(raw).decode(), CERTIFICATE_FIELD)
	return True


def fetch_from_hub(provider, merchant_uuid: str, password: str) -> bytes | None:
	"""Take the .p12 the hub stores for this merchant, once, by proving the password that opens it."""
	import requests

	if not password:
		return None
	url = f"{provider.service_url}/api/method/{FETCH_METHOD}"
	try:
		response = requests.post(
			url,
			headers=provider._auth_headers(),  # noqa: SLF001 — the provider's helper
			json={"merchant_uuid": merchant_uuid, "current_password": password},
			timeout=30,
		)
		answer = (response.json() or {}).get("message") or {}
	except Exception as exc:  # noqa: BLE001 — the hub may not know the method yet: the call goes to the hub
		_log("fetch", merchant_uuid, {"success": False, "error": repr(exc)[:120]}, time.monotonic())
		return None
	if not (response.ok and answer.get("success") and answer.get("content_base64")):
		_log(
			"fetch",
			merchant_uuid,
			{"success": False, "error": answer.get("error") or f"HTTP {response.status_code}"},
			time.monotonic(),
		)
		return None
	try:
		raw = base64.b64decode(answer["content_base64"], validate=True)
	except Exception:  # noqa: BLE001
		return None
	if store_certificate(
		merchant_uuid, raw
	):  # saved with the request; a request rolled back fetches it again
		_log("fetch", merchant_uuid, {"success": True}, time.monotonic())
		return raw
	return None


# ----------------------------------------------------------------------------------------------------------------------
# A call
# ----------------------------------------------------------------------------------------------------------------------


def execute(
	provider, command: str, merchant_uuid: str, params: dict, context: dict
) -> dict | None:
	"""Run a bridge command on the engine. None when this instance has no certificate for the merchant and the hub
	would not give it: the caller then goes to the hub's bridge, as before."""
	from payments import twint_engine

	started = time.monotonic()
	certificate = stored_certificate(merchant_uuid)
	if certificate is None:
		if frappe.cache.get_value(_refused_key(merchant_uuid)):
			return None
		certificate = fetch_from_hub(provider, merchant_uuid, context.get("certificate_password") or "")
		if certificate is None:
			frappe.cache.set_value(_refused_key(merchant_uuid), 1, expires_in_sec=FETCH_PAUSE)
			return None
	answer = twint_engine.execute(
		command,
		{
			"certificate": certificate,
			"certificate_password": context.get("certificate_password") or "",
			"store_uuid": context.get("store_uuid") or "",
			"cash_register_id": context.get("cash_register_id"),
			"environment": context.get("environment") or "production",
			"soap_version": soap_version(),
		},
		params,
		**_client_options(),
	)
	_log(command, merchant_uuid, answer, started)
	return answer


def _client_options() -> dict:
	"""Where the engine reaches TWINT: TWINT's own hosts. Tests point it at their fake server here, never a site."""
	return {}


def _log(operation: str, merchant_uuid: str, answer: dict, started: float) -> None:
	"""One line per call in logs/twint.log of the instance: never a password, never a certificate."""
	try:
		log = frappe.logger("twint", allow_site=False, file_count=10)
		log.setLevel(logging.INFO)  # Frappe's loggers start at ERROR on a production site
		error = ""
		if answer.get("success") is False:
			error = " error=" + " ".join(str(answer.get("error") or "")[:120].split())
		log.info(
			"call engine=python op=%s merchant=%s ok=%s status=%s ms=%d%s",
			operation,
			(merchant_uuid or "")[:8],
			answer.get("success") is not False,
			answer.get("transaction_status") or answer.get("reversal_status") or "-",
			(time.monotonic() - started) * 1000,
			error,
		)
	except Exception:  # noqa: BLE001 — a log line never breaks a payment
		pass
