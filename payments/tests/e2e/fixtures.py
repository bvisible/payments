# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""E2E fixture helpers — fixed test Customer / Address / User + state reset.

Strategy: ONE fixed test customer reused across runs, with a cleanup step
before every test. Keeps the dataset small, fast and predictable. The
customer name + login email are stored in ``site_config.json`` under
``e2e_test_customer`` / ``e2e_test_user_email`` so multiple deployments can
co-exist without colliding.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _


# Defaults used when site_config keys are absent — convenient on a brand-new
# bench. Production sites should pin them in site_config to avoid surprises.
_DEFAULT_CUSTOMER = "Test E2E Webshop"
_DEFAULT_EMAIL = "test.e2e@example.com"


def _cfg(key: str, default: str | None = None) -> str | None:
	return frappe.conf.get(key) or default


@frappe.whitelist()
def ensure_test_customer() -> dict[str, Any]:
	"""Create-or-reuse the fixed Test E2E Webshop Customer + Contact + Address + User.

	Idempotent: re-running is a no-op when records already exist.

	Returns the triplet (customer, user, password) so the runbook can copy
	the password into the Chrome login form.
	"""
	customer_name = _cfg("e2e_test_customer", _DEFAULT_CUSTOMER)
	email = _cfg("e2e_test_user_email", _DEFAULT_EMAIL)
	password = frappe.conf.get("e2e_test_user_password")
	if not password:
		frappe.throw(_("Missing 'e2e_test_user_password' in site_config"))

	# 1. User (Website User role + Customer role for portal access).
	# Always sync the password — if site_config rotates the value, ensure
	# the User account matches so the runbook login keeps working.
	from frappe.utils.password import update_password

	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": "Test",
				"last_name": "E2E",
				"send_welcome_email": 0,
				"enabled": 1,
				"user_type": "Website User",
				"new_password": password,
				"roles": [{"role": "Customer"}],
			}
		).insert(ignore_permissions=True)
	# Re-sync password every call (cheap, idempotent, fixes drift)
	try:
		update_password(email, password)
	except Exception:
		pass  # field not encrypted on some setups; ignore

	# 2. Customer record (Individual, default group/territory must exist).
	# ``default_currency`` is mandatory on some Frappe installs (custom field
	# from accounting localizations) — fall back to the Company default.
	if not frappe.db.exists("Customer", customer_name):
		default_company = frappe.defaults.get_global_default("company") or frappe.db.get_value(
			"Company", {}, "name"
		)
		default_currency = (
			frappe.db.get_value("Company", default_company, "default_currency")
			if default_company
			else None
		) or "CHF"
		frappe.get_doc(
			{
				"doctype": "Customer",
				"customer_name": customer_name,
				"customer_type": "Individual",
				"customer_group": frappe.db.get_single_value("Selling Settings", "customer_group")
				or "Individual",
				"territory": frappe.db.get_single_value("Selling Settings", "territory")
				or "All Territories",
				"default_currency": default_currency,
			}
		).insert(ignore_permissions=True)

	# 3. Contact linked to the Customer + User email (so portal sees the cart)
	contact_name = frappe.db.get_value(
		"Contact Email", {"email_id": email, "is_primary": 1}, "parent"
	)
	if not contact_name:
		contact = frappe.get_doc(
			{
				"doctype": "Contact",
				"first_name": "Test",
				"last_name": "E2E",
				"email_ids": [{"email_id": email, "is_primary": 1}],
				"links": [{"link_doctype": "Customer", "link_name": customer_name}],
			}
		).insert(ignore_permissions=True)
		contact_name = contact.name
	else:
		# Make sure the Contact links to our Customer (idempotent merge)
		doc = frappe.get_doc("Contact", contact_name)
		if not any(
			lnk.link_doctype == "Customer" and lnk.link_name == customer_name for lnk in doc.links
		):
			doc.append("links", {"link_doctype": "Customer", "link_name": customer_name})
			doc.save(ignore_permissions=True)

	# 4. Billing+shipping address attached to the Customer
	addr_title = f"{customer_name}-Billing"
	if not frappe.db.exists("Address", {"address_title": addr_title}):
		frappe.get_doc(
			{
				"doctype": "Address",
				"address_title": addr_title,
				"address_type": "Billing",
				"address_line1": "Rue du Test 1",
				"city": "Lausanne",
				"pincode": "1003",
				"country": "Switzerland",
				"is_primary_address": 1,
				"is_shipping_address": 1,
				"email_id": email,
				"phone": "+41 21 555 00 01",
				"links": [{"link_doctype": "Customer", "link_name": customer_name}],
			}
		).insert(ignore_permissions=True)

	frappe.db.commit()
	return {
		"customer": customer_name,
		"user": email,
		"password": password,
		"contact": contact_name,
		"address": addr_title,
	}


def _safe_cancel_and_delete(doctype: str, name: str) -> bool:
	"""Cancel-if-submitted then delete. Returns True if deleted, False otherwise."""
	try:
		doc = frappe.get_doc(doctype, name)
		if getattr(doc, "docstatus", 0) == 1:
			try:
				doc.cancel()
			except Exception:
				pass  # carry on — force=True below handles linked-doc errors
		frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)
		return True
	except Exception:
		return False


@frappe.whitelist()
def reset_test_env() -> dict[str, Any]:
	"""Cancel/delete Quotations + Sales Orders + Payment Requests + Payment Intents
	linked to the fixed test customer. Safe to run before every test for
	idempotence.

	Returns ``{quotations, sales_orders, payment_requests, payment_intents}``
	counts for visibility.
	"""
	customer = _cfg("e2e_test_customer", _DEFAULT_CUSTOMER)
	stats = {"quotations": 0, "sales_orders": 0, "payment_requests": 0, "payment_intents": 0}

	# 1. Sales Orders attached to the test customer.
	for so in frappe.get_all(
		"Sales Order", filters={"customer": customer, "docstatus": ["<", 2]}, pluck="name"
	):
		if _safe_cancel_and_delete("Sales Order", so):
			stats["sales_orders"] += 1

	# 2. Payment Requests — keyed by the party link, not always the customer.
	for pr in frappe.get_all(
		"Payment Request",
		filters={"party_type": "Customer", "party": customer, "docstatus": ["<", 2]},
		pluck="name",
	):
		# Delete PIs that reference this PR before we drop the PR.
		for pi in frappe.get_all(
			"Payment Intent",
			filters={"reference_doctype": "Payment Request", "reference_name": pr},
			pluck="name",
		):
			if _safe_cancel_and_delete("Payment Intent", pi):
				stats["payment_intents"] += 1
		if _safe_cancel_and_delete("Payment Request", pr):
			stats["payment_requests"] += 1

	# 3. Stray Payment Intents (not yet linked, e.g. failed mid-create_intent).
	#    Filter via metadata when available (the webshop helper stores
	#    twint_merchant_uuid + description "Webshop ACC-PRQ-...").
	for pi in frappe.get_all(
		"Payment Intent",
		filters={"status": ["in", ["requires_action", "processing"]]},
		fields=["name", "reference_doctype", "reference_name"],
	):
		# Only clean intents whose PR (if any) is already gone — that means
		# they're orphaned by step 2 above.
		ref = pi.get("reference_name")
		if pi.get("reference_doctype") == "Payment Request" and ref:
			if not frappe.db.exists("Payment Request", ref):
				if _safe_cancel_and_delete("Payment Intent", pi["name"]):
					stats["payment_intents"] += 1

	# 4. Cart-typed Quotations (drafts only — submitted ones stay for history).
	for q in frappe.get_all(
		"Quotation",
		filters={"party_name": customer, "order_type": "Shopping Cart", "docstatus": 0},
		pluck="name",
	):
		if _safe_cancel_and_delete("Quotation", q):
			stats["quotations"] += 1

	frappe.db.commit()
	return stats


@frappe.whitelist()
def get_test_item_for_checkout() -> dict[str, Any]:
	"""Pick a published Website Item with a positive price for the cart test.

	Returns ``{item_code, route, price}`` so the runbook can navigate directly
	to the product page.
	"""
	row = frappe.db.sql(
		"""
		SELECT i.name AS item_code,
		       wi.route,
		       ip.price_list_rate
		FROM `tabItem` i
		JOIN `tabItem Price` ip ON ip.item_code = i.name
		JOIN `tabWebsite Item` wi ON wi.item_code = i.name
		WHERE ip.price_list_rate > 0
		  AND i.has_variants = 0
		  AND i.disabled = 0
		  AND wi.published = 1
		ORDER BY ip.modified DESC
		LIMIT 1
		""",
		as_dict=True,
	)
	if not row:
		frappe.throw(_("No published Website Item with positive price found on this site"))
	r = row[0]
	return {
		"item_code": r["item_code"],
		"route": r.get("route") or f"products/{r['item_code']}",
		"price": float(r["price_list_rate"]),
	}
