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

	# 3. Contact linked to the Customer + User email (so portal sees the cart).
	# Make sure phone + email are set so the webshop checkout step-address
	# validation passes without the user having to type anything.
	contact_name = frappe.db.get_value(
		"Contact Email", {"email_id": email, "is_primary": 1}, "parent"
	)
	phone_number = "+41 21 555 00 01"
	if not contact_name:
		contact = frappe.get_doc(
			{
				"doctype": "Contact",
				"first_name": "Test",
				"last_name": "E2E",
				"email_ids": [{"email_id": email, "is_primary": 1}],
				"phone_nos": [{"phone": phone_number, "is_primary_phone": 1, "is_primary_mobile_no": 1}],
				"links": [{"link_doctype": "Customer", "link_name": customer_name}],
			}
		).insert(ignore_permissions=True)
		contact_name = contact.name
	else:
		# Make sure the Contact has Customer link + a phone number.
		# Only save when something actually changed — and tolerate a
		# concurrent ensure_test_customer (TimestampMismatch → reload+retry).
		for _retry in range(3):
			doc = frappe.get_doc("Contact", contact_name)
			changed = False
			if not any(
				lnk.link_doctype == "Customer" and lnk.link_name == customer_name
				for lnk in doc.links
			):
				doc.append("links", {"link_doctype": "Customer", "link_name": customer_name})
				changed = True
			if not doc.phone_nos:
				doc.append(
					"phone_nos",
					{"phone": phone_number, "is_primary_phone": 1, "is_primary_mobile_no": 1},
				)
				changed = True
			if not changed:
				break
			try:
				doc.save(ignore_permissions=True)
				break
			except frappe.TimestampMismatchError:
				if _retry == 2:
					raise
				continue

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

	# 0. Payment Entries first — a submitted Payment Entry linked to a Sales
	#    Order / Payment Request blocks the cascade below. Cancel + delete.
	stats["payment_entries"] = 0
	for pe in frappe.get_all(
		"Payment Entry",
		filters={"party_type": "Customer", "party": customer, "docstatus": ["<", 2]},
		pluck="name",
	):
		if _safe_cancel_and_delete("Payment Entry", pe):
			stats["payment_entries"] += 1

	# 1. Sales Orders attached to the test customer.
	for so in frappe.get_all(
		"Sales Order", filters={"customer": customer, "docstatus": ["<", 2]}, pluck="name"
	):
		if _safe_cancel_and_delete("Sales Order", so):
			stats["sales_orders"] += 1

	# 2. Payment Requests — every state (incl. cancelled) so no PI is orphaned.
	for pr in frappe.get_all(
		"Payment Request",
		filters={"party_type": "Customer", "party": customer},
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
def get_e2e_site_config() -> dict[str, Any]:
	"""Expose the E2E site_config keys via HTTPS API.

	Used by the Playwright suite to retrieve the test customer / user / password
	without an SSH session. Returns ONLY the 4 e2e keys — no sensitive secrets
	leak via this endpoint.
	"""
	return {
		"e2e_test_customer": _cfg("e2e_test_customer", _DEFAULT_CUSTOMER),
		"e2e_test_user_email": _cfg("e2e_test_user_email", _DEFAULT_EMAIL),
		"e2e_test_user_password": frappe.conf.get("e2e_test_user_password"),
		"enable_e2e_simulators": bool(frappe.conf.get("enable_e2e_simulators")),
	}


@frappe.whitelist()
def get_loyalty_balance() -> dict[str, Any]:
	"""Return the test customer's loyalty program + redeemable point balance.

	The webshop rounds the redeemable amount DOWN to the nearest 10 (see
	webshop/templates/pages/checkout.py). Returns ``{loyalty_program,
	available_points, redeemable_points, conversion_factor}``. If the customer
	has no program / no points, ``available_points`` is 0 and the loyalty
	test should skip.
	"""
	import math

	customer = _cfg("e2e_test_customer", _DEFAULT_CUSTOMER)
	program = frappe.db.get_value("Customer", customer, "loyalty_program")
	if not program:
		return {"loyalty_program": None, "available_points": 0, "redeemable_points": 0}

	# The webshop checkout calls apply_loyalty_points with the quotation's
	# company; ERPNext filters Loyalty Point Entries by company, so a customer
	# can have a non-zero global balance but zero in the webshop's company.
	# Resolve the company the webshop will use: Webshop Settings → system default.
	company = frappe.db.get_single_value("Webshop Settings", "company")
	if not company:
		company = frappe.defaults.get_global_default("company")

	from erpnext.accounts.doctype.loyalty_program.loyalty_program import (
		get_loyalty_program_details_with_points,
	)

	try:
		details = get_loyalty_program_details_with_points(
			customer, loyalty_program=program, company=company, silent=True
		)
		available = int(details.get("loyalty_points") or 0)
	except Exception:
		# Fallback: sum the Loyalty Point Entry rows directly (same company filter).
		filters = {"customer": customer, "loyalty_program": program}
		if company:
			filters["company"] = company
		rows = frappe.get_all(
			"Loyalty Point Entry",
			filters=filters,
			fields=["loyalty_points"],
		)
		available = sum(int(r["loyalty_points"] or 0) for r in rows)

	conversion = frappe.db.get_value("Loyalty Program", program, "conversion_factor") or 0
	return {
		"loyalty_program": program,
		"company": company,
		"available_points": available,
		"redeemable_points": math.floor(available / 10) * 10,
		"conversion_factor": float(conversion),
	}


@frappe.whitelist()
def ensure_loyalty_points(min_points: int = 50) -> dict[str, Any]:
	"""Make sure the test customer has at least ``min_points`` redeemable in
	the webshop's company. Inserts a single positive Loyalty Point Entry if
	needed — idempotent: re-running while the balance is already healthy is
	a no-op.

	Loyalty Point Entries are normally created from Sales Invoices via the
	Loyalty Program collection rules. For E2E we top up directly so the test
	does not depend on a prior purchase flow.
	"""
	from frappe.utils import add_days, today

	min_points = int(min_points)
	customer = _cfg("e2e_test_customer", _DEFAULT_CUSTOMER)
	program = frappe.db.get_value("Customer", customer, "loyalty_program")
	if not program:
		frappe.throw(f"Customer {customer} has no Loyalty Program assigned")

	company = frappe.db.get_single_value("Webshop Settings", "company")
	if not company:
		company = frappe.defaults.get_global_default("company")
	if not company:
		frappe.throw("No company resolvable for the webshop")

	balance = get_loyalty_balance()
	if balance.get("available_points", 0) >= min_points:
		return {"action": "noop", "balance": balance}

	tier = frappe.db.get_value("Customer", customer, "loyalty_program_tier")
	if not tier:
		tiers = frappe.get_all(
			"Loyalty Program Collection",
			filters={"parent": program},
			fields=["tier_name"],
			limit=1,
		)
		tier = tiers[0]["tier_name"] if tiers else None

	expiry_days = frappe.db.get_value("Loyalty Program", program, "expiry_duration") or 365

	entry = frappe.get_doc(
		{
			"doctype": "Loyalty Point Entry",
			"loyalty_program": program,
			"loyalty_program_tier": tier,
			"customer": customer,
			"company": company,
			"loyalty_points": int(min_points),
			"purchase_amount": 0,
			"posting_date": today(),
			"expiry_date": add_days(today(), int(expiry_days)),
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()

	return {
		"action": "topped_up",
		"entry": entry.name,
		"added_points": int(min_points),
		"balance": get_loyalty_balance(),
	}


@frappe.whitelist()
def get_test_item_for_checkout() -> dict[str, Any]:
	"""Pick a published Website Item with a positive price for the cart test.

	Returns ``{item_code, route, price}`` so the runbook can navigate directly
	to the product page.

	Deterministic + addable: we require the item to actually have an
	"Add to cart" button, i.e. either a non-stock item (always purchasable) or
	a stock item with positive on-hand quantity. Ordered by ``is_stock_item``
	then name so the result is stable across runs (the previous ``modified
	DESC`` ordering drifted to whatever item price was last edited — which
	could be an out-of-stock product with no add-to-cart button).
	"""
	row = frappe.db.sql(
		"""
		SELECT i.name AS item_code,
		       wi.route,
		       ip.price_list_rate
		FROM `tabItem` i
		JOIN `tabItem Price` ip ON ip.item_code = i.name
		JOIN `tabWebsite Item` wi ON wi.item_code = i.name
		LEFT JOIN (
			SELECT item_code, SUM(actual_qty) AS qty
			FROM `tabBin` GROUP BY item_code
		) b ON b.item_code = i.name
		WHERE ip.price_list_rate > 0
		  AND i.has_variants = 0
		  AND (i.variant_of IS NULL OR i.variant_of = '')
		  AND i.disabled = 0
		  AND wi.published = 1
		  AND (i.is_stock_item = 0 OR COALESCE(b.qty, 0) > 0)
		ORDER BY i.is_stock_item ASC, i.name ASC
		LIMIT 1
		""",
		as_dict=True,
	)
	if not row:
		frappe.throw(_("No published, addable Website Item with positive price found on this site"))
	r = row[0]
	return {
		"item_code": r["item_code"],
		"route": r.get("route") or f"products/{r['item_code']}",
		"price": float(r["price_list_rate"]),
	}


# ---------------------------------------------------------------------------
# B2B checkout fixtures
# ---------------------------------------------------------------------------
#
# The webshop redirects /checkout → /checkout_b2b when the logged-in
# customer's Customer Group is listed in Webshop Settings.b2b_customer_group
# AND activate_b2b_checkout is on (see webshop/templates/pages/checkout.py
# lines 80-88 and webshop/shopping_cart/cart.py lines 158-170).
#
# The fixtures below create the Customer Group + Pricing Rule + Webshop
# Settings mutation needed to drive the B2B branch, then let the Playwright
# test drive an ephemeral signup and reassign the resulting Customer.

_B2B_GROUP = "B2B"
_B2B_PRICING_RULE = "B2B 10% E2E"


@frappe.whitelist()
def ensure_b2b_environment() -> dict[str, Any]:
	"""Set up the minimum data + settings required for the B2B checkout flow.

	Idempotent — re-running while everything exists is a no-op.

	Creates / updates:
	  1. Customer Group ``B2B`` (leaf, ``is_group=0``).
	  2. Pricing Rule ``B2B 10% E2E`` — 10% discount on the whole transaction,
	     scoped to Customer Group B2B, selling-side.
	  3. Webshop Settings: ``activate_b2b_checkout=1`` and appends
	     ``B2B`` to the ``b2b_customer_group`` child table.

	Returns ``{customer_group, pricing_rule, discount_percentage,
	settings_snapshot}`` where snapshot reflects the prior Webshop Settings
	state (kept for diagnostics — restoration is intentionally NOT done so
	the env stays B2B-ready between runs).
	"""
	# 1. Customer Group
	if not frappe.db.exists("Customer Group", _B2B_GROUP):
		frappe.get_doc(
			{
				"doctype": "Customer Group",
				"customer_group_name": _B2B_GROUP,
				"is_group": 0,
			}
		).insert(ignore_permissions=True)

	# 2. Pricing Rule — 10% off transaction for Customer Group B2B
	discount_pct = 10
	if not frappe.db.exists("Pricing Rule", _B2B_PRICING_RULE):
		frappe.get_doc(
			{
				"doctype": "Pricing Rule",
				"title": _B2B_PRICING_RULE,
				"apply_on": "Transaction",
				"price_or_product_discount": "Price",
				"selling": 1,
				"buying": 0,
				"applicable_for": "Customer Group",
				"customer_group": _B2B_GROUP,
				"rate_or_discount": "Discount Percentage",
				"discount_percentage": discount_pct,
				"min_qty": 0,
				"priority": "1",
			}
		).insert(ignore_permissions=True)

	# 3. Webshop Settings — capture snapshot, then mutate
	ws = frappe.get_single("Webshop Settings")
	snapshot = {
		"activate_b2b_checkout": int(ws.activate_b2b_checkout or 0),
		"b2b_customer_group": [r.customer_group for r in (ws.b2b_customer_group or [])],
	}
	changed = False
	if not ws.activate_b2b_checkout:
		ws.activate_b2b_checkout = 1
		changed = True
	if not any(r.customer_group == _B2B_GROUP for r in (ws.b2b_customer_group or [])):
		ws.append("b2b_customer_group", {"customer_group": _B2B_GROUP})
		changed = True
	if changed:
		ws.save(ignore_permissions=True)
	frappe.db.commit()

	return {
		"customer_group": _B2B_GROUP,
		"pricing_rule": _B2B_PRICING_RULE,
		"discount_percentage": discount_pct,
		"settings_snapshot": snapshot,
	}


def _resolve_customer_for_email(email: str) -> str | None:
	"""Resolve the Customer linked to a Website User email.

	Walks the standard webshop chain: ``Portal User → Customer``, then falls
	back to ``Contact → Customer`` (the path ``cart.get_party`` actually uses).
	"""
	# Path 1 — Portal User row stored against the Customer
	cust = frappe.db.get_value("Portal User", {"user": email}, "parent")
	if cust and frappe.db.exists("Customer", cust):
		return cust

	# Path 2 — Contact email_id → Dynamic Link to Customer
	contact_name = frappe.db.get_value(
		"Contact Email", {"email_id": email, "is_primary": 1}, "parent"
	)
	if not contact_name:
		contact_name = frappe.db.get_value("Contact Email", {"email_id": email}, "parent")
	if contact_name:
		link = frappe.db.get_value(
			"Dynamic Link",
			{"parent": contact_name, "link_doctype": "Customer"},
			"link_name",
		)
		if link and frappe.db.exists("Customer", link):
			return link

	return None


@frappe.whitelist()
def wait_for_customer(email: str, timeout_s: int = 10) -> dict[str, Any]:
	"""Poll the backend until a Customer record exists for ``email``.

	The webshop creates the Customer lazily in ``get_party()`` — the first
	time the logged-in user touches the cart (add_to_cart / load /cart).
	Returns ``{customer, attempts}`` or throws if the timeout elapses.
	"""
	import time

	timeout_s = int(timeout_s)
	attempts = 0
	start = time.time()
	while time.time() - start < timeout_s:
		attempts += 1
		customer = _resolve_customer_for_email(email)
		if customer:
			return {"customer": customer, "attempts": attempts}
		time.sleep(1)
	frappe.throw(
		_("No Customer found for {0} after {1}s ({2} attempts)").format(
			email, timeout_s, attempts
		)
	)


@frappe.whitelist()
def assign_customer_to_b2b(email: str) -> dict[str, Any]:
	"""Set the test user's Customer.customer_group to ``B2B`` and propagate
	to any existing draft cart Quotation so its pricing rules get recomputed.

	Required AFTER the webshop has lazily created the Customer (see
	``wait_for_customer``). The propagation step is what actually triggers
	the B2B Pricing Rule on the line items — the Customer.customer_group
	change alone leaves a stale snapshot on the Quotation.
	"""
	customer = _resolve_customer_for_email(email)
	if not customer:
		frappe.throw(_("No Customer linked to user {0}").format(email))

	previous = frappe.db.get_value("Customer", customer, "customer_group")
	if previous != _B2B_GROUP:
		doc = frappe.get_doc("Customer", customer)
		doc.customer_group = _B2B_GROUP
		doc.save(ignore_permissions=True)

	# Force every draft cart Quotation linked to this customer to recompute
	# its pricing — save() re-runs apply_pricing_rule with the new group.
	quots_updated = []
	for q in frappe.get_all(
		"Quotation",
		filters={
			"party_name": customer,
			"order_type": "Shopping Cart",
			"docstatus": 0,
		},
		pluck="name",
	):
		quot = frappe.get_doc("Quotation", q)
		quot.customer_group = _B2B_GROUP
		quot.save(ignore_permissions=True)
		quots_updated.append(quot.name)

	frappe.db.commit()
	return {
		"customer": customer,
		"customer_group": _B2B_GROUP,
		"previous": previous,
		"quotations_updated": quots_updated,
	}


@frappe.whitelist()
def get_b2b_quotation_summary(email: str) -> dict[str, Any]:
	"""Return the pricing breakdown of the user's latest cart Quotation.

	Used by the B2B test to verify the Pricing Rule beyond the displayed
	grand total (which shipping fees + taxes can obscure). For a
	``Transaction``-scoped 10% discount rule, ERPNext stores the effect in
	``additional_discount_percentage`` on the document — that's the
	cleanest single attribute to assert against.
	"""
	customer = _resolve_customer_for_email(email)
	if not customer:
		frappe.throw(_("No Customer linked to user {0}").format(email))

	quots = frappe.get_all(
		"Quotation",
		filters={
			"party_name": customer,
			"order_type": "Shopping Cart",
			"docstatus": 0,
		},
		order_by="modified desc",
		limit=1,
		pluck="name",
	)
	if not quots:
		frappe.throw(_("No cart Quotation found for {0}").format(customer))

	doc = frappe.get_doc("Quotation", quots[0])
	return {
		"name": doc.name,
		"customer": customer,
		"customer_group": doc.customer_group,
		"total": float(doc.total or 0),
		"net_total": float(doc.net_total or 0),
		"discount_amount": float(doc.discount_amount or 0),
		"additional_discount_percentage": float(doc.additional_discount_percentage or 0),
		"grand_total": float(doc.grand_total or 0),
		"total_taxes_and_charges": float(doc.total_taxes_and_charges or 0),
		"items": [
			{
				"item_code": it.item_code,
				"qty": float(it.qty or 0),
				"rate": float(it.rate or 0),
				"amount": float(it.amount or 0),
				"price_list_rate": float(it.price_list_rate or 0),
				"discount_amount": float(it.discount_amount or 0),
				"discount_percentage": float(it.discount_percentage or 0),
				"pricing_rules": it.pricing_rules or "",
			}
			for it in (doc.items or [])
		],
	}


@frappe.whitelist()
def cleanup_b2b_user(email: str) -> dict[str, Any]:
	"""Delete the ephemeral B2B test user + linked Customer / Contact / Quotations.

	Best-effort: every step is wrapped so a partial run leaves the env in a
	usable state. The shared B2B Customer Group / Pricing Rule / Webshop
	Settings are deliberately preserved so the next run starts ready.

	Returns counters per doctype for visibility.
	"""
	stats = {
		"quotations": 0,
		"contacts": 0,
		"customers": 0,
		"portal_users": 0,
		"users": 0,
	}

	customer = _resolve_customer_for_email(email)

	# 1. Draft cart-typed Quotations tied to the Customer
	if customer:
		for q in frappe.get_all(
			"Quotation",
			filters={"party_name": customer, "order_type": "Shopping Cart", "docstatus": 0},
			pluck="name",
		):
			if _safe_cancel_and_delete("Quotation", q):
				stats["quotations"] += 1

	# 2. Portal User rows referencing this email
	for pu in frappe.get_all("Portal User", filters={"user": email}, pluck="name"):
		try:
			frappe.delete_doc("Portal User", pu, force=True, ignore_permissions=True)
			stats["portal_users"] += 1
		except Exception:
			pass

	# 3. Contact(s) linked via Contact Email
	contact_names = set()
	for ce in frappe.get_all(
		"Contact Email", filters={"email_id": email}, fields=["parent"]
	):
		contact_names.add(ce["parent"])
	for cn in contact_names:
		try:
			frappe.delete_doc("Contact", cn, force=True, ignore_permissions=True)
			stats["contacts"] += 1
		except Exception:
			pass

	# 4. Customer
	if customer and frappe.db.exists("Customer", customer):
		try:
			frappe.delete_doc("Customer", customer, force=True, ignore_permissions=True)
			stats["customers"] += 1
		except Exception:
			pass

	# 5. User
	if frappe.db.exists("User", email):
		try:
			frappe.delete_doc("User", email, force=True, ignore_permissions=True)
			stats["users"] += 1
		except Exception:
			pass

	frappe.db.commit()
	return stats
