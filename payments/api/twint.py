# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""TWINT scheduler + helpers.

Since TWINT does not push webhooks, the bridge does not signal state changes
proactively. To keep the Frappe Payment Intent in sync with the TWINT side, we
poll the bridge for every intent in ``processing`` / ``requires_action`` state
older than a few seconds. The cadence is a scheduler ``every_minute`` cron
declared in ``hooks.py``; the POS UI uses SocketIO to push the status updates
to the cashier dialog.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

import frappe
from frappe.utils import now_datetime


def poll_pending_twint_transactions() -> dict[str, Any]:
	"""Iterate over open TWINT Payment Intents and reconcile via the bridge.

	Re-entrant and idempotent. Safe under concurrent scheduler runs because
	transitions in the FSM are themselves idempotent (self-transitions return
	False without logging).
	"""
	from payments.drivers.registry import resolve_driver

	# Pick up only intents created via TWINT and not yet in a terminal state.
	# Cap the batch to avoid long-running ticks.
	intents = frappe.get_all(
		"Payment Intent",
		filters={
			"channel": "qr_bridge",
			"status": ["in", ["requires_action", "processing"]],
		},
		fields=["name", "provider", "channel", "provider_intent_id", "metadata_json", "created_at"],
		order_by="creation asc",
		limit_page_length=50,
	)

	stats = {"checked": 0, "advanced": 0, "errors": 0, "skipped": 0}
	cutoff = now_datetime() - timedelta(minutes=10)

	for intent in intents:
		if not intent["provider_intent_id"]:
			stats["skipped"] += 1
			continue
		try:
			md = json.loads(intent["metadata_json"] or "{}")
		except (ValueError, TypeError):
			md = {}
		merchant_uuid = (md.get("twint_merchant_uuid") or "").strip()
		if not merchant_uuid:
			stats["skipped"] += 1
			continue

		stats["checked"] += 1
		try:
			driver = resolve_driver(intent["provider"], intent["channel"])
			result = driver._call_bridge(  # noqa: SLF001 — internal scheduler reuse
				"monitor_status",
				merchant_uuid,
				{"order_id": intent["provider_intent_id"]},
			)
		except Exception as exc:  # noqa: BLE001
			stats["errors"] += 1
			frappe.log_error(
				"poll_pending_twint_transactions bridge error",
				f"intent={intent['name']} order_id={intent['provider_intent_id']}: {exc!r}",
			)
			continue

		if not (result or {}).get("success"):
			stats["errors"] += 1
			frappe.log_error(
				"poll_pending_twint_transactions bridge !success",
				f"intent={intent['name']} result={result}",
			)
			continue

		target = driver._map_status(result.get("transaction_status"))  # noqa: SLF001
		if not target or target == "processing":
			# Optional: timeout intents that have been pending > 10 min.
			# Compare ``created_at`` rather than ``modified`` since polling rewrites the latter.
			ts = intent.get("created_at")
			if ts and ts < cutoff:
				doc = frappe.get_doc("Payment Intent", intent["name"])
				doc.transition_to(
					"canceled",
					event_source="poll",
					error_code="twint_timeout",
					error_message="No final status received after 10 minutes",
					ignore_invalid=True,
				)
				stats["advanced"] += 1
			continue

		# Apply the transition.
		doc = frappe.get_doc("Payment Intent", intent["name"])
		moved = doc.transition_to(
			target,
			event_source="poll",
			ignore_invalid=True,
		)
		if moved:
			stats["advanced"] += 1
			frappe.publish_realtime(
				event=f"payment.intent.{doc.name}.updated",
				message={"intent_name": doc.name, "status": target, "channel": "qr_bridge"},
				after_commit=True,
			)

	frappe.db.commit()
	return stats
