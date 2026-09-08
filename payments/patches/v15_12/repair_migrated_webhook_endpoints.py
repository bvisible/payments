# //// Neoffice — added file (no upstream equivalent). Second pass on #219.
# //// v15_11 derived the endpoint from the provider FAMILY, but only knew the `_test`
# //// and `_live` suffixes plus the provider's own `mode`. The twint_integration /
# //// webshopsi_integration merge into `payments` left providers named
# //// `wallee_migrated` (mode=test, on dmis and demo) and `twint_migrated` (mode=live,
# //// on blowbackshop) where `_migrated` is part of the historical NAME. The family
# //// stayed `wallee_migrated`, no receiver matched, and v15_11 CLEARED the field:
# //// an enabled provider on a live site left with no URL to register.
# //// v15_11 is already in Patch Log everywhere, so it cannot re-run — hence this one.
import frappe


def execute():
	"""Recompute the endpoints v15_11 could not resolve, now that `_migrated` is known.

	Same rule as v15_11 and same guard: only a value carrying our convention prefix
	(or an empty field) is touched, so an endpoint set by hand is never overwritten.
	A family with no receiver — TWINT ships none — legitimately stays empty.
	"""
	fixed, kept = 0, 0

	for name in frappe.get_all("Provider Channel Settings", pluck="name"):
		doc = frappe.get_doc("Provider Channel Settings", name)
		before = (doc.webhook_endpoint or "").strip()
		doc._compute_webhook_endpoint()
		after = (doc.webhook_endpoint or "").strip()
		if after == before:
			kept += 1
			continue
		# db_set, not save(): validate() would re-run uniqueness on rows this patch
		# does not own, and a legacy duplicate would abort the whole migration.
		doc.db_set("webhook_endpoint", after, update_modified=False)
		fixed += 1

	frappe.db.commit()
	print(f"Provider Channel Settings webhook endpoints: {fixed} resolved by family, {kept} unchanged")
