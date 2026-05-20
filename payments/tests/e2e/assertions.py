# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""E2E assertion helpers — run via bench execute after each PSP test step."""

from __future__ import annotations

from typing import Any

import frappe


@frappe.whitelist()
def assert_payment_complete(payment_intent: str) -> dict[str, Any]:
	"""Return the PI / PR / SO triplet status for a single Payment Intent.

	The runbook calls this after each PSP step and asserts ``ok=True``. Failing
	cases come back with ``ok=False`` plus enough fields to root-cause.
	"""
	pi = frappe.get_doc("Payment Intent", payment_intent)
	out: dict[str, Any] = {
		"payment_intent": pi.name,
		"pi_status": pi.status,
		"channel": pi.channel,
		"provider": pi.provider,
		"amount": pi.amount,
		"currency": pi.currency,
		"provider_intent_id": pi.provider_intent_id,
	}

	if pi.reference_doctype == "Payment Request" and pi.reference_name:
		try:
			pr = frappe.get_doc("Payment Request", pi.reference_name)
		except frappe.DoesNotExistError:
			out["pr_missing"] = True
		else:
			out["payment_request"] = pr.name
			out["pr_status"] = pr.status
			out["pr_docstatus"] = pr.docstatus
			if pr.reference_doctype == "Sales Order" and pr.reference_name:
				try:
					so = frappe.get_doc("Sales Order", pr.reference_name)
				except frappe.DoesNotExistError:
					out["so_missing"] = True
				else:
					out["sales_order"] = so.name
					out["so_status"] = so.status
					out["so_docstatus"] = so.docstatus
					out["so_grand_total"] = so.grand_total

	out["ok"] = (
		out.get("pi_status") == "succeeded"
		and out.get("pr_status") == "Paid"
		and out.get("so_docstatus") == 1
	)
	return out


@frappe.whitelist()
def assert_recent_sales_order(minutes: int = 10) -> dict[str, Any]:
	"""Return the most recent submitted Sales Order for the test customer.

	Works for ALL PSPs — Stripe webshop goes through the upstream
	``stripe_checkout.make_payment`` which does NOT create a unified Payment
	Intent, so an SO-based assertion is the common denominator.

	Returns ``{ok, sales_order, so_status, grand_total, payment_request,
	pr_status}``.
	"""
	customer = frappe.conf.get("e2e_test_customer") or "Test E2E Webshop"
	cutoff = frappe.utils.add_to_date(None, minutes=-int(minutes))

	rows = frappe.get_all(
		"Sales Order",
		filters={"customer": customer, "creation": [">", cutoff]},
		fields=["name", "status", "docstatus", "grand_total"],
		order_by="creation desc",
		limit=1,
	)
	if not rows:
		return {"ok": False, "error": f"No Sales Order for {customer} in last {minutes} min"}

	so = rows[0]
	out: dict[str, Any] = {
		"sales_order": so["name"],
		"so_status": so["status"],
		"so_docstatus": so["docstatus"],
		"grand_total": so["grand_total"],
	}
	# Find the linked Payment Request (if any).
	pr = frappe.db.get_value(
		"Payment Request",
		{"reference_doctype": "Sales Order", "reference_name": so["name"]},
		["name", "status", "docstatus"],
		as_dict=True,
	)
	if pr:
		out["payment_request"] = pr["name"]
		out["pr_status"] = pr["status"]
		out["pr_docstatus"] = pr["docstatus"]

	out["ok"] = so["docstatus"] == 1
	return out


@frappe.whitelist()
def list_recent_test_intents(minutes: int = 30) -> list[dict[str, Any]]:
	"""Return Payment Intents created in the last N minutes — for runbook fetch.

	Used when the runbook lost track of the intent_name (e.g. tab refresh) and
	needs to recover it from the most recent test activity.
	"""
	import json

	customer = frappe.conf.get("e2e_test_customer") or "Test E2E Webshop"
	cutoff = frappe.utils.add_to_date(None, minutes=-int(minutes))

	# Find PRs of the test customer, then enumerate their PIs.
	pr_names = frappe.get_all(
		"Payment Request",
		filters={"party_type": "Customer", "party": customer, "creation": [">", cutoff]},
		pluck="name",
	)
	if not pr_names:
		return []

	intents = frappe.get_all(
		"Payment Intent",
		filters={"reference_doctype": "Payment Request", "reference_name": ["in", pr_names]},
		fields=["name", "status", "channel", "provider_intent_id", "reference_name", "creation"],
		order_by="creation desc",
		limit=20,
	)
	return [dict(i) for i in intents]
