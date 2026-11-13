# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and Contributors
# License: MIT. See LICENSE

import frappe

from payments.utils.utils import require_text, safe_redirect_to

# //// Neoffice — the page was cached by path: the write below ran for the first visitor only, and each later
# //// buyer who turned back was shown that visitor's page with no cancel at all.
no_cache = 1


def get_context(context):
	token = frappe.local.form_dict.token

	# //// Neoffice — `set_value` reads anything but a text as a filter, and a guest's JSON body cancelled every
	# //// row it matched, of any gateway, settled ones included. The name in the link is the proof: only the
	# //// buyer's browser holds it (random, or the gateway's own token). And a buyer can only turn back from a
	# //// payment still waiting (`Queued`): the status of one that moved on is what other code reads to book it.
	require_text(token)
	if token:
		frappe.db.set_value("Integration Request", {"name": token, "status": "Queued"}, "status", "Cancelled")
		# A page render is a GET, which Frappe does not commit.
		frappe.db.commit()  # nosemgrep

	# //// Neoffice — see payment-cancel.html.
	context.redirect_to = safe_redirect_to(frappe.local.form_dict.redirect_to)
