# //// Neoffice — added file (no upstream equivalent). Repairs what #219 left behind.
# //// The endpoint used to be built from the Payment Provider RECORD name, so every
# //// provider whose name is not exactly a family name carried a path that 404s:
# //// `wallee_test` produced `/api/method/payments.api.webhook_wallee_test.handle`,
# //// and only `webhook_stripe`, `webhook_wallee` and `webhook_payrexx` ship.
# //// Nothing in the app READS the field — a human copies it into the PSP's own
# //// dashboard — so a wrong value here is a webhook the provider can never deliver,
# //// with no error anywhere on our side. Measured on osiris: 9 of 12 rows wrong.
import frappe


def execute():
	"""Recompute every binding's endpoint under the family rule, in place.

	Only touches values the old code computed (they carry the convention prefix):
	an endpoint someone set by hand is left exactly as it is. A family with no
	receiver has its stale computed path CLEARED — an empty field is honest, a
	path that answers 404 is not.
	"""
	rows = frappe.get_all("Provider Channel Settings", pluck="name")
	fixed, cleared, kept = 0, 0, 0

	for name in rows:
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
		if after:
			fixed += 1
		else:
			cleared += 1

	frappe.db.commit()
	print(
		f"Provider Channel Settings webhook endpoints: {fixed} corrected, "
		f"{cleared} cleared (no receiver ships), {kept} already right"
	)
