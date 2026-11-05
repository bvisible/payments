# //// Neoffice — added file (no upstream equivalent). `/payment-failed` had no controller, so it was cached
# //// by path and language: the first visitor's `redirect_to` was stored in the page and served to the next
# //// ones, and a script in it reached them with no link at all. The page is built for each visitor now, and
# //// its link stays on this site.
# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE

import frappe

from payments.utils.utils import safe_redirect_to

no_cache = 1


def get_context(context):
	context.redirect_to = safe_redirect_to(frappe.local.form_dict.redirect_to)
