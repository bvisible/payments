# //// Neoffice — added file (no upstream equivalent). Who may open a payment on a Payment Request from the
# //// web checkouts of TWINT, Payrexx and Wallee. These pivots are open to guests, and they opened a
# //// transaction at the PSP on any Inward request a guest named, at any provider a guest named, without end.
# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""Who may open a payment on a Payment Request from a web checkout.

The shop's checkout raises a Payment Request on the visitor's cart, then calls a pivot with its name. A name
is no proof: request names are sequential. What proves a request is the caller's is on the server:

- a desk account the desk lets read the request;
- the cart of the request is this session's: the ``guest_session_id`` cookie for a guest, the customer for a
  signed-in buyer. That is webshop's own rule, the one ``create_payment_request`` applied a moment earlier;
- this account opened the request, or its Contact is its customer (webshop's rule to fail a request).

An e-mailed payment link does not come through here. Upstream's gateways send the buyer to their own page with
an Integration Request in the link, and Wallee's ``payment_url`` is the hosted page itself. If a link to these
pivots is ever added, it carries a signed token: :mod:`payments.api.intent_access` shows how.
"""

from __future__ import annotations

import frappe
from frappe import _

# Per address: a buyer starts one payment and retries by hand, while each attempt holds a TWINT worker for
# minutes or spends a share of the Payrexx quota.
WEB_TRANSACTIONS_PER_MINUTE = 10
SCRIPT_URLS_PER_MINUTE = 30

# Per request, whichever address it comes from: an open attempt is a Payment Intent, an object at the PSP
# and, on TWINT, a poller. A slot comes back when the attempt ends, which the pollers see within minutes.
_OPEN_ATTEMPTS_PER_REQUEST = 20
_OPEN_STATUSES = ("requires_action", "processing")

_CLOSED_STATUSES = ("Paid", "Cancelled")


def failure_message(intent: dict, fallback: str) -> str:
	"""What a failed attempt tells its caller.

	The reason a PSP or the network gave is in English and can name a host and our merchant instance: it stays
	on the intent, for whoever reads intents at the desk.
	"""
	if frappe.has_permission("Payment Intent", "read"):
		return intent.get("error_message") or fallback
	return fallback


def _deny() -> None:
	# One answer for a stranger's request and a name that does not exist: names must not be enumerated.
	frappe.throw(_("Not permitted"), frappe.PermissionError)


def _is_the_buyers(request) -> bool:  # noqa: ANN001
	try:
		from webshop.controllers import payment_handler
	except ImportError:
		return False  # no shop on this site: no cart, and no buyer without a desk account

	# First: it answers a guest with no, and it does not look the customer up, which may create one.
	if payment_handler._may_conclude(request.name, money_confirmed=False):
		return True
	return payment_handler._is_the_session_s_quotation(payment_handler._cart_of_request(request))


def require_own_request(payment_request_id: str):
	"""Return the Payment Request the caller may pay, or raise ``frappe.PermissionError``.

	The annotation does not settle it: on live traffic a module with deferred annotations only reports a wrong
	type, and a dict here is a filter of the caller's choice in ``frappe.get_doc``.
	"""
	if not payment_request_id or not isinstance(payment_request_id, str):
		_deny()
	try:
		request = frappe.get_doc("Payment Request", payment_request_id)
	except frappe.DoesNotExistError:
		frappe.clear_last_message()
		_deny()

	if request.payment_request_type != "Inward":
		_deny()
	if not (frappe.has_permission("Payment Request", "read", doc=request) or _is_the_buyers(request)):
		_deny()
	return request


def require_provider_choice(provider: str | None) -> None:
	"""A checkout runs on the provider the site resolves; naming another one is a desk right.

	The name skipped the rule that a live provider beats a test one: a test payment concludes an order.
	"""
	if not provider:
		return
	if not isinstance(provider, str) or not frappe.has_permission("Payment Provider", "read"):
		_deny()


def require_own_transaction(transaction_id: int | str) -> frappe._dict:
	"""Return the ``wallee_web`` intent of a transaction id, when its request is the caller's."""
	text = str(transaction_id).strip()
	if not text.isdigit():
		_deny()
	intent = frappe.db.get_value(
		"Payment Intent",
		{"provider_intent_id": text, "channel": "wallee_web"},
		["provider", "reference_doctype", "reference_name"],
		as_dict=True,
	)
	if not intent or intent.reference_doctype != "Payment Request":
		_deny()
	require_own_request(intent.reference_name)
	return intent


def refusal(request, payment_request_id: str, channel: str) -> str | None:  # noqa: ANN001
	"""Why this request opens no new transaction, in words for its buyer; ``None`` when it may."""
	if request.docstatus == 2 or request.status in _CLOSED_STATUSES:
		return _("This payment request is closed.")

	attempts = frappe.db.count(
		"Payment Intent",
		{
			"reference_doctype": "Payment Request",
			"reference_name": payment_request_id,
			"channel": channel,
			"status": ["in", _OPEN_STATUSES],
		},
	)
	if attempts >= _OPEN_ATTEMPTS_PER_REQUEST:
		return _("Too many payment attempts. Please try again later.")
	return None
