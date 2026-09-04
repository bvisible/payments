#//// Neoffice — added file (no upstream equivalent). The TWINT poller: TWINT pushes no
#//// webhook, so state is pulled — an every-minute cron for the POS QR (`qr_bridge`),
#//// plus a fast-poll job for the webshop consumer (`twint_web`) and the operator's
#//// phone (`twint_mobile`) while the buyer waits, publishing
#//// `payment.intent.<name>.updated` over SocketIO so the overlay redirects without
#//// polling. TWINT in-store runs through our central PHP bridge rather than Stripe's
#//// TWINT QR (ADR-002); upstream has no TWINT at all.
#//// Commits: 258f8cf 2026-05-13 "feat(payments): Phase 4 — TWINT PHP bridge driver + scheduler poll"
#////          9a8e74f 2026-06-30 "feat(twint): enqueue fast-poll for POS (qr_bridge) so the till validates in a few seconds, not up to 1 min…"
# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""TWINT scheduler + helpers (POS terminal, webshop consumer, operator's phone).

Since TWINT does not push webhooks, the bridge does not signal state changes
proactively. We poll for the three channels of ``_TWINT_CHANNELS``:

- ``qr_bridge`` (POS terminal): the cashier dialog watches via SocketIO; an
  every-minute cron is enough since the cashier is staring at the screen and
  will see the update within a beat.
- ``twint_web`` (webshop consumer): the buyer is waiting on a checkout page,
  so we enqueue a **fast-poll job** at intent creation that polls every 5s for
  5 min, then backs off to 30s for another 10 min. The per-minute cron is the
  safety net for missed enqueues.
- ``twint_mobile`` (operator's phone): the same bridge flow drawn by the app in
  front of the customer. It settles through these pollers like the other two;
  it is a separate channel because a payment at the customer's door is not a
  webshop order and must not run the shop's settlement hook.

Status transitions publish ``payment.intent.<name>.updated`` via SocketIO so
the JS overlay can redirect without polling.
"""

from __future__ import annotations

import json
import time
from datetime import timedelta
from typing import Any

import frappe
from frappe import _
from frappe.utils import now_datetime


# Channels driven by the TWINT bridge — both flows share the SDK calls
# (Client.startOrder + monitorOrder), the difference is who shows the QR.
# `twint_mobile` is the same bridge flow drawn on the operator's phone; it settles
# through these pollers like the other two.
_TWINT_CHANNELS = ("qr_bridge", "twint_web", "twint_mobile")


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

	# Once the client confirms in their app the order sits in
	# ORDER_CONFIRMATION_PENDING and the MERCHANT must CAPTURE it (confirm_payment).
	# This is a property of the TWINT order, not of the channel: TwintWebDriver
	# subclasses TwintPHPBridgeDriver and registers the very same order, so the
	# webshop needs the capture exactly like the POS does. Scoping this to
	# qr_bridge left every webshop payment stuck in "processing" until the 10-min
	# timeout cancelled it — the buyer had confirmed and paid nothing.
	if (result.get("transaction_status") or "").upper() == "ORDER_CONFIRMATION_PENDING":
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

	_retry_unfinalized_web_orders(stats)

	frappe.db.commit()
	return stats


def _retry_unfinalized_web_orders(stats: dict[str, int]) -> None:
	"""Recover ``twint_web`` payments that succeeded but produced no Sales Order.

	Every TWINT path to ``succeeded`` finalises on the way through, so the order is
	normally created immediately. But finalisation can still fail after the money
	moved — webshop raising, a validation refusing the order, the site restarting
	mid-job — and once the intent is ``succeeded`` the sweep above no longer looks at
	it, since it only selects non-final states. Without this pass such a payment is
	invisible until the customer asks where their order is.

	Wallee has had this self-heal for a while; TWINT was the one channel of the
	ontology without it. Costs no bridge call: the payment is already known-good,
	only the local document is missing.

	**Bounded to the last 24 hours on purpose.** Without an upper bound the first run
	on an existing site reaches back through all of its history and silently settles
	old requests — observed on osiris, where it marked two Payment Requests from May
	as Paid. Nothing was double-charged there (no Payment Entry, no GL entry), but a
	payment left unfinalised for a day is no longer something to repair quietly: a
	human may have refunded it, entered it by hand, or cancelled the order. Past the
	window it is logged for someone to look at instead.
	"""
	horizon = now_datetime() - timedelta(hours=24)
	orphans = frappe.get_all(
		"Payment Intent",
		filters={
			"channel": "twint_web",
			"status": "succeeded",
			"reference_doctype": "Payment Request",
			"modified": [">", horizon],
		},
		fields=["name", "reference_name"],
		order_by="modified desc",
		limit_page_length=50,
	)

	for row in orphans:
		if not row.reference_name:
			continue
		pr_state = frappe.db.get_value(
			"Payment Request", row.reference_name, ["status", "docstatus"], as_dict=True
		)
		# docstatus 0 + not Paid is the signature of a finalisation that never ran:
		# handle_payment_success submits the request as part of creating the order.
		if not pr_state or pr_state.docstatus != 0 or pr_state.status == "Paid":
			continue
		try:
			result = _finalize_webshop_sales_order(frappe.get_doc("Payment Intent", row.name))
			if result and result.get("status") == "success":
				stats["advanced"] += 1
		except Exception as exc:  # noqa: BLE001 — one bad row must not stop the sweep
			stats["errors"] += 1
			frappe.log_error("twint web orphan finalize", f"intent={row.name}: {exc!r}")

	# Anything past the window still needs a human — the bound must not turn the
	# original gap back on, only stop the automatic repair from acting silently on
	# old money. Reported through stats rather than Error Log: this cron runs every
	# minute, and one row per minute would bury the log it is meant to draw attention
	# to. The count surfaces in Scheduled Job Log.
	stale = frappe.db.sql(
		"""select count(pi.name) from `tabPayment Intent` pi
		   join `tabPayment Request` pr on pr.name = pi.reference_name
		   where pi.channel = 'twint_web' and pi.status = 'succeeded'
		     and pi.reference_doctype = 'Payment Request'
		     and pi.modified <= %s and pr.docstatus = 0 and pr.status != 'Paid'""",
		horizon,
	)[0][0]
	if stale:
		stats["stale_unfinalized"] = stale
		frappe.logger("twint").warning(
			f"{stale} twint_web payment(s) succeeded but unfinalised for over 24h — needs review"
		)


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

	# Stay under the short queue's 300s RQ timeout: hand over to the per-minute
	# cron (the designed safety net) instead of getting killed mid-loop, which
	# logged a JobTimeoutException on every payment that wasn't near-instant.
	budget = 280
	started = time.monotonic()

	for delay in _FAST_POLL_DELAYS:
		if time.monotonic() - started + delay > budget:
			return stats  # cron takes over past this point (by design)
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

		try:
			_process_one_intent(dict(row), stats)
			frappe.db.commit()
		except frappe.TimestampMismatchError:
			# another poller (per-minute cron) advanced the same intent first —
			# fine, pick up the fresh state on the next tick
			frappe.db.rollback()
			stats["skipped"] += 1

		# Re-read post-process to check for terminal status reached this tick.
		new_status = frappe.db.get_value("Payment Intent", intent_name, "status")
		if new_status in terminal:
			return stats

		time.sleep(delay)

	return stats
