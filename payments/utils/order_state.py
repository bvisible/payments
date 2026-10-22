# //// Neoffice — added file (no upstream equivalent). When a Payment Request is « on an order ».
"""Whether the Sales Order a Payment Request points at is a real order yet.

The webshop raises a card payment on a PLACEHOLDER draft Sales Order (`PENDING-<quotation>`) and makes the real
order only once the money is confirmed (webshop/shopping_cart/pending_order.py), because upstream ERPNext refuses
a Payment Request against a Quotation. Reading « the request points at a Sales Order » as « the order exists »
would send a buyer to the thank-you page of an order that is not made: the order exists once it is submitted.
Asked of the database, not of a name, so the answer holds for a request raised before the placeholder too.
"""

import frappe


def submitted_sales_order(reference_doctype, reference_name):
	"""The name of the real (submitted) Sales Order a request ends on, or None."""
	if reference_doctype != "Sales Order" or not reference_name:
		return None
	if frappe.db.get_value("Sales Order", reference_name, "docstatus") == 1:
		return reference_name
	return None
