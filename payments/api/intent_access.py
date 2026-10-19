# //// Neoffice — added file (no upstream equivalent). Who may read or cancel a web checkout's
# //// Payment Intent without a desk account (maintenance#1731). The webshop pivots (TWINT dialog,
# //// Payrexx return page) are open to guests and used to act on whatever intent they were named,
# //// whatever its channel. Only the browser that created the intent holds its token now.
# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""Access to a web checkout's Payment Intent for callers without a desk account.

A guest cannot be told apart from another guest (they all share the ``Guest``
session), and requiring POST does not help: every guest gets a session and a CSRF
token of its own. So the right to touch an intent travels with a token instead.

:func:`web_access_token` is handed out by the ``create_web_transaction`` pivot that
created the intent, and the browser sends it back on every later call. It is an HMAC
of the intent name under a key derived from the site's encryption key: nothing is
stored, it never expires (an intent ends on its own), and it is worth one intent only.

A signed-in user whom the desk would let read the intent needs no token: the desk
already shows them the same document.
"""

from __future__ import annotations

import hashlib
import hmac

import frappe
from frappe import _

# Bumping the label invalidates every token handed out before.
_KEY_LABEL = b"payments.intent_access.web.v1"


def _signing_key() -> bytes:
	from frappe.utils.password import get_encryption_key

	# A key of its own, derived, so the HMAC never reuses the Fernet key as is.
	return hmac.new(get_encryption_key().encode(), _KEY_LABEL, hashlib.sha256).digest()


def web_access_token(intent_name: str) -> str:
	"""Return the token that lets a browser read or cancel ``intent_name``."""
	return hmac.new(_signing_key(), str(intent_name).encode(), hashlib.sha256).hexdigest()[:40]


def _deny() -> None:
	# One answer for a wrong token, another channel and a name that does not exist:
	# a guest must not learn which intent names are real.
	frappe.throw(_("Not permitted"), frappe.PermissionError)


def require_web_access(
	intent_name: str, access_token: str | None, channel: str, ptype: str = "read"
):
	"""Return the Payment Intent the caller may touch, or raise ``frappe.PermissionError``.

	Args:
	    intent_name: the Payment Intent the browser names.
	    access_token: the token :func:`web_access_token` gave that browser, if any.
	    channel: the only channel this entry point serves (``twint_web``,
	        ``payrexx_web``); an intent of any other channel is refused, token or not.
	    ptype: the desk permission a signed-in user without the token needs
	        (``read`` to look, ``write`` to cancel).
	"""
	if not intent_name or not isinstance(intent_name, str):
		_deny()

	if access_token and hmac.compare_digest(str(access_token), web_access_token(intent_name)):
		if not frappe.db.exists("Payment Intent", intent_name):
			_deny()
		doc = frappe.get_doc("Payment Intent", intent_name)
	elif frappe.session.user != "Guest" and frappe.db.exists("Payment Intent", intent_name):
		doc = frappe.get_doc("Payment Intent", intent_name)
		if not frappe.has_permission("Payment Intent", ptype, doc=doc):
			_deny()
	else:
		_deny()

	if doc.channel != channel:
		_deny()
	return doc
