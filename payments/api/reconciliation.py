# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""Auto-reconciliation — turn a succeeded Payment Intent into an ERPNext payment record.

When a Payment Intent transitions to ``succeeded`` we want the source-of-truth
document (Sales Invoice, POS Invoice, Web Form, …) to reflect the money it just
received. This module centralizes that logic:

- **POS Invoice / Sales Invoice**: append a ``Sales Invoice Payment`` line in
  the ``payments`` child table with the right ``mode_of_payment`` and amount.
- Otherwise: log + skip (custom integrations can plug their own logic via
  ``doc_events`` on Payment Intent).

Reconciliation is **idempotent**: a flag ``reconciled_at`` is stored in the
Payment Intent's ``metadata_json``. A second invocation is a no-op.

The entry point ``reconcile_payment_intent`` is called from
:func:`payments.utils.hook.on_payment_intent_update` (wired in ``hooks.py``)
so callers don't need to know about it.
"""

from __future__ import annotations

import json
from typing import Any

import frappe
from frappe import _
from frappe.utils import flt, now_datetime


def reconcile_payment_intent(intent_name: str) -> dict[str, Any]:
	"""Idempotently reconcile a succeeded Payment Intent with its reference doc.

	Returns a dict ``{action, target_doctype, target_name, message}`` summarizing
	what happened. ``action`` is one of: ``skipped_not_succeeded``,
	``skipped_already_reconciled``, ``skipped_no_reference``,
	``invoice_payment_recorded``, ``unsupported_reference``.
	"""
	doc = frappe.get_doc("Payment Intent", intent_name)

	if doc.status != "succeeded":
		return {"action": "skipped_not_succeeded", "status": doc.status}

	md = doc.get_metadata() if hasattr(doc, "get_metadata") else {}
	if md.get("reconciled_at"):
		return {
			"action": "skipped_already_reconciled",
			"reconciled_at": md["reconciled_at"],
		}

	if not doc.reference_doctype or not doc.reference_name:
		return {"action": "skipped_no_reference"}

	ref_dt = doc.reference_doctype
	ref_name = doc.reference_name

	# Mode of payment selection priority:
	# 1. explicit metadata.mode_of_payment (set by POSNext.pos_start_payment)
	# 2. fall back to the channel-specific default if configured
	mode_of_payment = (md.get("mode_of_payment") or "").strip() or None

	result: dict[str, Any]
	if ref_dt == "POS Invoice":
		result = _reconcile_pos_invoice(doc, ref_name, mode_of_payment)
	elif ref_dt == "Sales Invoice":
		result = _reconcile_sales_invoice(doc, ref_name, mode_of_payment)
	else:
		frappe.log_error(
			"reconcile_payment_intent unsupported reference",
			f"intent={intent_name} ref_doctype={ref_dt} ref_name={ref_name}",
		)
		result = {"action": "unsupported_reference", "reference_doctype": ref_dt}

	# Always stamp reconciliation flag so the doc_event doesn't loop, even for
	# skipped / unsupported paths. ``db_update`` bypasses doc_events so we won't
	# trigger ourselves recursively.
	md["reconciled_at"] = str(now_datetime())
	md["reconciliation_action"] = result.get("action")
	doc.metadata_json = json.dumps(md)
	doc.db_update()
	frappe.db.commit()

	return result


# ----------------------------------------------------------------------------
# POS Invoice path — append a Sales Invoice Payment row in `payments` child table
# ----------------------------------------------------------------------------


def _reconcile_pos_invoice(intent, name: str, mode_of_payment: str | None) -> dict[str, Any]:
	if not frappe.db.exists("POS Invoice", name):
		return {"action": "skipped_missing_target", "reference_doctype": "POS Invoice", "reference_name": name}

	pos_doc = frappe.get_doc("POS Invoice", name)
	# If already paid for the same provider_intent_id, skip.
	for row in pos_doc.get("payments") or []:
		if (row.get("reference_no") or "") == (intent.provider_intent_id or ""):
			return {
				"action": "skipped_already_paid",
				"reference_no": row.get("reference_no"),
			}

	mop = mode_of_payment or _default_mode_of_payment(pos_doc)
	if not mop:
		return {"action": "skipped_no_mode_of_payment"}

	major_amount = flt(intent.amount) / 100.0
	pos_doc.append(
		"payments",
		{
			"mode_of_payment": mop,
			"amount": major_amount,
			"reference_no": intent.provider_intent_id or intent.name,
		},
	)
	# Save without submit — POS Invoice submission is owned by POSNext logic.
	pos_doc.save(ignore_permissions=True)
	return {
		"action": "invoice_payment_recorded",
		"target_doctype": "POS Invoice",
		"target_name": name,
		"mode_of_payment": mop,
		"amount": major_amount,
	}


# ----------------------------------------------------------------------------
# Sales Invoice path — same idea (Sales Invoice has the same `payments` table when is_pos)
# ----------------------------------------------------------------------------


def _reconcile_sales_invoice(intent, name: str, mode_of_payment: str | None) -> dict[str, Any]:
	if not frappe.db.exists("Sales Invoice", name):
		return {
			"action": "skipped_missing_target",
			"reference_doctype": "Sales Invoice",
			"reference_name": name,
		}

	si = frappe.get_doc("Sales Invoice", name)
	# Skip if already paid via this provider_intent_id.
	for row in si.get("payments") or []:
		if (row.get("reference_no") or "") == (intent.provider_intent_id or ""):
			return {"action": "skipped_already_paid", "reference_no": row.get("reference_no")}

	mop = mode_of_payment or _default_mode_of_payment(si)
	if not mop:
		return {"action": "skipped_no_mode_of_payment"}

	major_amount = flt(intent.amount) / 100.0
	si.append(
		"payments",
		{
			"mode_of_payment": mop,
			"amount": major_amount,
			"reference_no": intent.provider_intent_id or intent.name,
		},
	)
	si.save(ignore_permissions=True)
	return {
		"action": "invoice_payment_recorded",
		"target_doctype": "Sales Invoice",
		"target_name": name,
		"mode_of_payment": mop,
		"amount": major_amount,
	}


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------


def _default_mode_of_payment(invoice_doc) -> str | None:
	"""Read the default Mode of Payment from the POS Profile when available."""
	profile = getattr(invoice_doc, "pos_profile", None)
	if not profile:
		return None
	try:
		profile_doc = frappe.get_doc("POS Profile", profile)
	except Exception:  # noqa: BLE001
		return None
	# POS Profile.payments is a child table with `mode_of_payment` + `default`.
	for row in getattr(profile_doc, "payments", []) or []:
		if row.get("default"):
			return row.get("mode_of_payment")
	# Fallback to the first row.
	rows = getattr(profile_doc, "payments", []) or []
	if rows:
		return rows[0].get("mode_of_payment")
	return None


# ----------------------------------------------------------------------------
# Frappe doc_event entry point
# ----------------------------------------------------------------------------


def on_payment_intent_after_update(doc, method=None):  # noqa: ANN001 — Frappe signature
	"""Wired in ``hooks.py`` ``doc_events``. Reconciles on every save where status=succeeded."""
	if not getattr(doc, "status", None) == "succeeded":
		return
	try:
		reconcile_payment_intent(doc.name)
	except Exception as exc:  # noqa: BLE001 — never break the save
		frappe.log_error(
			"reconcile_payment_intent failed in doc_event",
			f"intent={doc.name}: {exc!r}",
		)
