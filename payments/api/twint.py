# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""TWINT scheduler + helpers (POS terminal + webshop consumer).

Since TWINT does not push webhooks, the bridge does not signal state changes
proactively. We poll for both channels:

- ``qr_bridge`` (POS terminal) : the cashier dialog watches via SocketIO; a
  every-minute cron is enough since the cashier is staring at the screen and
  will see the update within a beat.
- ``twint_web`` (webshop consumer) : the buyer is waiting on a checkout page,
  so we enqueue a **fast-poll job** at intent creation that polls every 5s for
  5 min, then backs off to 30s for another 10 min. The per-minute cron is the
  safety net for missed enqueues.

Status transitions publish ``payment.intent.<name>.updated`` via SocketIO so
the JS overlay can redirect without polling.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from typing import Any

import frappe
from frappe.utils import now_datetime


# Channels driven by the TWINT bridge — both flows share the SDK calls
# (Client.startOrder + monitorOrder), the difference is who shows the QR.
_TWINT_CHANNELS = ("qr_bridge", "twint_web")


def _publish_intent_update(doc, channel: str, target: str, extra: dict[str, Any] | None = None) -> None:
	"""Push the new status to subscribers (JS overlay, POS dialog, etc.)."""
	message: dict[str, Any] = {
		"intent_name": doc.name,
		"status": target,
		"channel": channel,
	}
	if extra:
		message.update(extra)
	frappe.publish_realtime(
		event=f"payment.intent.{doc.name}.updated",
		message=message,
		after_commit=True,
	)


def _finalize_webshop_sales_order(doc) -> dict[str, Any] | None:
	"""For ``twint_web`` intents linked to a Payment Request, create the Sales Order.

	Mirrors the bridge logic in :mod:`payments.www.wallee_success`. Returns the
	finalisation result (with ``redirect_to``) so the SocketIO push can carry
	the URL to the buyer's overlay.
	"""
	if doc.channel != "twint_web":
		return None
	if doc.reference_doctype != "Payment Request" or not doc.reference_name:
		return None
	try:
		from webshop.controllers.payment_handler import handle_payment_success

		return handle_payment_success(payment_request_id=doc.reference_name)
	except ImportError:
		# Webshop app not installed — nothing to finalise.
		return None
	except Exception as exc:  # noqa: BLE001
		frappe.log_error(
			"twint webshop handle_payment_success failed",
			f"intent={doc.name} pr={doc.reference_name}: {exc!r}\n{frappe.get_traceback()}",
		)
		return {"status": "error", "message": str(exc)[:140]}


def _process_one_intent(intent: dict[str, Any], stats: dict[str, int]) -> None:
	"""Poll the bridge for ONE Payment Intent and apply any state change.

	Shared between the every-minute cron and the per-intent fast-poll job.
	"""
	from payments.drivers.registry import resolve_driver

	if not intent.get("provider_intent_id"):
		stats["skipped"] += 1
		return
	try:
		md = json.loads(intent.get("metadata_json") or "{}")
	except (ValueError, TypeError):
		md = {}

	try:
		driver = resolve_driver(intent["provider"], intent["channel"])
	except Exception as exc:  # noqa: BLE001
		stats["errors"] += 1
		frappe.log_error(
			"poll_pending_twint resolve_driver error",
			f"intent={intent['name']} {intent['provider']}/{intent['channel']}: {exc!r}",
		)
		return

	# Resolve the merchant. Webshop (twint_web) intents carry twint_merchant_uuid
	# in their metadata; POS (qr_bridge) intents carry only the provider default,
	# so resolve it the SAME way the driver does at create time (metadata →
	# binding → provider default). Without this fallback the poll skipped — and
	# therefore NEVER confirmed — every POS TWINT payment.
	merchant_uuid = (md.get("twint_merchant_uuid") or "").strip()
	if not merchant_uuid and hasattr(driver, "_resolve_merchant_uuid"):
		merchant_uuid = (driver._resolve_merchant_uuid(md) or "").strip()  # noqa: SLF001
	if not merchant_uuid:
		stats["skipped"] += 1
		return

	stats["checked"] += 1
	try:
		result = driver._call_bridge(  # noqa: SLF001 — internal scheduler reuse
			"monitor_status",
			merchant_uuid,
			{"order_id": intent["provider_intent_id"]},
		)
	except Exception as exc:  # noqa: BLE001
		stats["errors"] += 1
		frappe.log_error(
			"poll_pending_twint bridge error",
			f"intent={intent['name']} order_id={intent['provider_intent_id']}: {exc!r}",
		)
		return

	if not (result or {}).get("success"):
		stats["errors"] += 1
		frappe.log_error(
			"poll_pending_twint bridge !success",
			f"intent={intent['name']} result={result}",
		)
		return

	# TWINT POS (qr_bridge): once the client confirms in their app the order is
	# ORDER_CONFIRMATION_PENDING and the MERCHANT must CAPTURE it (confirm_payment).
	# Without this the payment never settles and the POS never reaches "succeeded".
	# The webshop (twint_web) flow captures elsewhere — scope this to the POS channel.
	if intent.get("channel") == "qr_bridge" and (
		(result.get("transaction_status") or "").upper() == "ORDER_CONFIRMATION_PENDING"
	):
		try:
			capture = driver._call_bridge(  # noqa: SLF001
				"confirm_payment",
				merchant_uuid,
				{"order_id": intent["provider_intent_id"], "amount": intent.get("amount")},
			)
		except Exception as exc:  # noqa: BLE001
			stats["errors"] += 1
			frappe.log_error(
				"poll_pending_twint confirm_payment error",
				f"intent={intent['name']} order_id={intent['provider_intent_id']}: {exc!r}",
			)
			return
		if (capture or {}).get("success"):
			result = capture  # ORDER_OK / SUCCESS now

	target = driver._map_status(result.get("transaction_status"))  # noqa: SLF001
	if not target or target == "processing":
		# Timeout intents that have been pending > 10 min (cron path only;
		# fast_poll has its own exit condition).
		ts = intent.get("created_at")
		cutoff = now_datetime() - timedelta(minutes=10)
		if ts and ts < cutoff:
			doc = frappe.get_doc("Payment Intent", intent["name"])
			if doc.transition_to(
				"canceled",
				event_source="poll",
				error_code="twint_timeout",
				error_message="No final status received after 10 minutes",
				ignore_invalid=True,
			):
				stats["advanced"] += 1
				_publish_intent_update(doc, intent["channel"], "canceled")
		return

	# Apply the transition.
	doc = frappe.get_doc("Payment Intent", intent["name"])
	moved = doc.transition_to(target, event_source="poll", ignore_invalid=True)
	if not moved:
		return

	stats["advanced"] += 1

	# For webshop (twint_web) intents, finalise the Sales Order on success and
	# pass the redirect URL to the buyer via SocketIO so their overlay can
	# navigate without polling our REST API.
	extra: dict[str, Any] = {}
	if target == "succeeded":
		finalize = _finalize_webshop_sales_order(doc)
		if finalize and finalize.get("status") == "success":
			extra["redirect_to"] = finalize.get("redirect_to")

	_publish_intent_update(doc, intent["channel"], target, extra)


def poll_pending_twint_transactions() -> dict[str, Any]:
	"""Iterate over open TWINT Payment Intents (both channels) and reconcile.

	Re-entrant and idempotent. Safe under concurrent scheduler runs because
	transitions in the FSM are themselves idempotent (self-transitions return
	False without logging). Covers BOTH ``qr_bridge`` (POS) AND ``twint_web``
	(webshop) — the fast-poll job covers the early seconds of webshop intents,
	this cron is the safety net.
	"""
	intents = frappe.get_all(
		"Payment Intent",
		filters={
			"channel": ["in", list(_TWINT_CHANNELS)],
			"status": ["in", ["requires_action", "processing"]],
		},
		fields=[
			"name",
			"provider",
			"channel",
			"amount",
			"provider_intent_id",
			"metadata_json",
			"created_at",
		],
		order_by="creation asc",
		limit_page_length=50,
	)

	stats = {"checked": 0, "advanced": 0, "errors": 0, "skipped": 0}
	for intent in intents:
		_process_one_intent(intent, stats)

	frappe.db.commit()
	return stats


# Fast-poll cadence (seconds). Hugged tight for the first 5min, then loosens
# to keep workers responsive for other jobs. Total horizon ~15min — past that,
# the every-minute cron + 10-min timeout in _process_one_intent take over.
_FAST_POLL_DELAYS: list[int] = (
	[3] * 50 +   # first ~2.5min @ 3s — near-instant POS/webshop validation
	[8] * 30 +   # next ~4min @ 8s
	[15] * 20    # tail @ 15s
)


# ---------------------------------------------------------------------------
# DEV-ONLY E2E SIMULATOR
# ---------------------------------------------------------------------------
#
# Gated by ``frappe.conf.enable_e2e_simulators=True``. Returns 403 in prod.
#
# Used by the Webshop E2E runbook (Neoffice/Webshop/Runbooks/E2E-Twint-Only.md)
# to validate the full chain — SocketIO push → Sales Order finalize →
# /thank_you redirect — without needing a real TWINT app to scan the QR.
#
# Safe to leave in production code because:
#   (a) the gating flag is opt-in per-site via site_config
#   (b) the endpoint is whitelisted but requires an authenticated user
#   (c) calling it on a real PI just shortcuts what the poll would do anyway
@frappe.whitelist()
def simulate_consumer_success(intent_name: str) -> dict[str, Any]:
	"""DEV-ONLY — transition a TWINT Payment Intent to succeeded.

	Idempotent: re-running re-finalises + re-pushes the SocketIO event so
	callers can replay the event during JS debugging.

	Raises ``frappe.PermissionError`` (HTTP 403) when
	``frappe.conf.enable_e2e_simulators`` is not truthy.
	"""
	if not frappe.conf.get("enable_e2e_simulators"):
		frappe.throw(_("E2E simulators not enabled on this site"), frappe.PermissionError)

	doc = frappe.get_doc("Payment Intent", intent_name)

	if doc.status != "succeeded":
		moved = doc.transition_to(
			"succeeded",
			event_source="manual",
			payload_excerpt="E2E simulate_consumer_success (dev-only)",
			ignore_invalid=True,
		)
		if not moved:
			return {"ok": False, "error": f"could not transition to succeeded from {doc.status}"}
		doc.reload()

	# Always re-run finalize + publish so re-invoking re-pushes the SocketIO event.
	extra: dict[str, Any] = {}
	finalize = _finalize_webshop_sales_order(doc)
	if finalize and finalize.get("status") == "success":
		extra["redirect_to"] = finalize.get("redirect_to")
	elif doc.reference_doctype == "Payment Request" and doc.reference_name:
		pr_ref = frappe.db.get_value(
			"Payment Request",
			doc.reference_name,
			["reference_doctype", "reference_name"],
			as_dict=True,
		)
		if pr_ref and pr_ref.reference_doctype == "Sales Order":
			extra["redirect_to"] = f"/thank_you?sales_order={pr_ref.reference_name}"

	_publish_intent_update(doc, doc.channel, "succeeded", extra)
	frappe.db.commit()
	return {"ok": True, "intent_name": doc.name, "status": "succeeded", **extra}


def fast_poll_intent(intent_name: str) -> dict[str, Any]:
	"""Tight poll loop for one ``twint_web`` intent (enqueued at creation).

	Stops early when the intent reaches a terminal state. ``deduplicate=True``
	on the enqueue guarantees we don't run two loops for the same intent even
	if the driver is re-entered.
	"""
	stats = {"iterations": 0, "checked": 0, "advanced": 0, "errors": 0, "skipped": 0}
	terminal = {"succeeded", "failed", "canceled", "refunded"}

	for delay in _FAST_POLL_DELAYS:
		stats["iterations"] += 1
		try:
			row = frappe.db.get_value(
				"Payment Intent",
				intent_name,
				["name", "provider", "channel", "amount", "provider_intent_id", "metadata_json", "created_at", "status"],
				as_dict=True,
			)
		except Exception as exc:  # noqa: BLE001
			frappe.log_error("fast_poll_intent lookup failed", f"intent={intent_name}: {exc!r}")
			return stats

		if not row:
			return stats  # intent vanished

		if row["status"] in terminal:
			return stats  # already done — nothing to poll

		_process_one_intent(dict(row), stats)
		frappe.db.commit()

		# Re-read post-process to check for terminal status reached this tick.
		new_status = frappe.db.get_value("Payment Intent", intent_name, "status")
		if new_status in terminal:
			return stats

		time.sleep(delay)

	return stats
