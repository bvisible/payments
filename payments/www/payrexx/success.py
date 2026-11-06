# //// Neoffice — added file (no upstream equivalent). Controller of `/payrexx/success`:
# //// look the Payment Intent up, refresh it from Payrexx (the redirect can beat the
# //// settlement), finalise the Sales Order through `webshop.controllers.payment_handler`
# //// and redirect to /thank-you, otherwise render the pending or failed state. The
# //// polling is bounded, one read of the gateway per load, because every attempt is an API
# //// call and Payrexx rate-limits at ~600 requests per five minutes per account.
# //// Commits: 4c05756 2026-08-11 "feat(payrexx): add Payrexx as a third payment provider"
# ////          a009847 2026-08-31 "fix(payrexx): the return page span for ever on a payment that never came"
# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""``/payrexx/success`` — landing page after a Payrexx hosted checkout.

URL: ``/payrexx/success?payment_intent=PI-2026-XXXXX``

All three Payrexx return URLs (success, failed, cancel) point here on purpose: the
page reads the real status off the Payment Intent rather than inferring it from
which URL the shopper arrived on. That matters because the shopper can land on the
"success" URL while Payrexx has not finished settling, and can land on "cancel"
after a payment that actually went through.

Flow:

1. Look up the Payment Intent.
2. Refresh its status from Payrexx (the redirect can beat the settlement).
3. On ``succeeded`` → finalise the Sales Order through
   ``webshop.controllers.payment_handler`` and redirect to ``/thank-you``.
4. Otherwise render the pending or failed state; the template auto-refreshes while
   pending, so a payment that settles after our polling window is still caught.
"""

from __future__ import annotations

import frappe
from frappe import _

# //// Neoffice — see order_state.py.
from payments.utils.order_state import submitted_sales_order

no_cache = 1

# //// Neoffice — a load used to read the gateway up to six times with `time.sleep(3)` in between, on a
# //// synchronous web worker: one guest with one open intent held a worker for 15 s per GET, and three such
# //// GETs held every worker of the site. A load now reads the gateway once and never sleeps: the waiting is
# //// the template's own reload, every 3 s, which holds nothing on the server.
#: The least time between two reads of the gateway for one intent, just under the
#: template's reload: parallel loads share a read, because every attempt is an API
#: call and Payrexx rate-limits at ~600 requests per five minutes per account.
_READ_GAP_SECONDS = 2

#: How many times the page may reload itself before it stops and says so.
#:
#: A shopper who comes back from Payrexx without paying leaves the gateway in
#: ``waiting`` for good. Reloading forever then means a spinner that never
#: resolves, no way out, and an API call every three seconds until the tab is
#: closed. Roughly 45 s of trying is generous for a settlement that is normally
#: instantaneous; past that the honest thing is to say the payment did not arrive
#: and offer to start again. The webhook still settles it if it lands later.
# //// Neoffice — 15 reloads three seconds apart: the same 45 s as the 5 slower ones it replaces.
_MAX_PENDING_LOADS = 15

_FINAL_STATES = ("succeeded", "failed", "canceled", "refunded")


def _debug(msg: str) -> None:
	"""Emit a debug line to the bench log, not the Error Log doctype.

	Logging debug noise into Error Log floods a doctype operators need for real
	errors — a lesson already learned on the Wallee return page.
	"""
	frappe.logger("payrexx").debug(msg)


def _may_read_gateway(intent_name: str) -> bool:
	"""True at most once per :data:`_READ_GAP_SECONDS` for one intent."""
	key = f"payrexx_return_read:{intent_name}"
	if frappe.cache.get_value(key):
		return False
	frappe.cache.set_value(key, 1, expires_in_sec=_READ_GAP_SECONDS)
	return True


def _refresh_status(intent_doc) -> None:
	"""Read the gateway once and move the Payment Intent to what it says.

	The template refreshes itself a bounded number of times while pending, so a
	slower settlement is still picked up, and the webhook remains the real backstop.
	"""
	from payments.drivers.registry import resolve_driver

	if not intent_doc.provider_intent_id or intent_doc.status in _FINAL_STATES:
		return
	if not _may_read_gateway(intent_doc.name):
		return

	driver = resolve_driver(intent_doc.provider, intent_doc.channel)

	try:
		response = driver.get_status(intent_doc.provider_intent_id)
		if response.status and response.status != intent_doc.status:
			intent_doc.transition_to(
				response.status,
				event_source="redirect",
				payload_excerpt=f"payrexx gateway {intent_doc.provider_intent_id}",
				ignore_invalid=True,
			)
			intent_doc.reload()
	except Exception as exc:  # noqa: BLE001 - never break the page, the next load reads again
		_debug(f"sync error: {exc!r}")
		# //// Neoffice — maintenance#2296: a move that failed half-way left its status written and in memory, and the
		# //// order was concluded on it, then committed with no Payment Event. The page reads the database again:
		# //// nothing was written before the move in this request.
		frappe.db.rollback()
		intent_doc.reload()


def get_context(context):  # noqa: ANN001
	intent_name = frappe.form_dict.get("payment_intent")
	_debug(f"START payment_intent={intent_name}")

	context.intent = None
	context.error = None
	context.status = "pending"

	if not intent_name:
		context.error = _("Missing payment reference")
		return context

	# //// Neoffice — guarded (maintenance#1731): the page showed, refreshed from its PSP and finalised whatever
	# //// intent the URL named, whatever its channel. Now a `payrexx_web` intent only, with the token its return
	# //// URL carries, or a desk user allowed to read it. A refusal reads as "not found", like a missing name.
	from payments.api.intent_access import require_web_access

	access_token = frappe.form_dict.get("access_token")
	try:
		intent_doc = require_web_access(intent_name, access_token, "payrexx_web", "read")
	except frappe.PermissionError:
		frappe.clear_messages()
		context.error = _("Payment reference not found")
		_debug(f"PI not found or not this browser's: {intent_name}")
		return context

	context.intent = intent_doc

	# Short-circuit a double-clicked return URL: if the linked Payment Request is
	# already settled, go straight to the thank-you page.
	if intent_doc.reference_doctype == "Payment Request" and intent_doc.reference_name:
		pr = frappe.db.get_value(
			"Payment Request",
			intent_doc.reference_name,
			["status", "reference_doctype", "reference_name"],
			as_dict=True,
		)
		# //// Neoffice — « on a Sales Order » is not « ordered »: until the money is in, the request points at the
		# //// cart's placeholder order (payments/utils/order_state.py).
		order = pr and submitted_sales_order(pr.reference_doctype, pr.reference_name)
		if pr and pr.status in ("Paid", "Completed") and order:
			_debug(f"PR already paid, redirecting to {order}")
			frappe.local.flags.redirect_location = f"/thank-you?sales_order={order}"
			raise frappe.Redirect

	# Which reload this is. Carried in the URL rather than in the session: the
	# page must be able to give up even for a guest, and a session counter would
	# also make a second tab inherit the first one's countdown.
	try:
		attempt = max(0, int(frappe.form_dict.get("try") or 0))
	except (TypeError, ValueError):
		attempt = 0
	context.attempt = attempt

	_refresh_status(intent_doc)
	_debug(f"after refresh status={intent_doc.status} attempt={attempt}")

	if intent_doc.status == "succeeded":
		context.status = "success"
		if intent_doc.reference_doctype == "Payment Request" and intent_doc.reference_name:
			try:
				from webshop.controllers.payment_handler import handle_payment_success

				result = handle_payment_success(payment_request_id=intent_doc.reference_name)
				_debug(f"handle_payment_success result={result}")

				if result and result.get("status") == "success":
					redirect_url = result.get("redirect_to")
					if redirect_url:
						frappe.local.flags.redirect_location = redirect_url
						raise frappe.Redirect
					pr_doc = frappe.get_doc("Payment Request", intent_doc.reference_name)
					# //// Neoffice — only a real order (see above).
					order = submitted_sales_order(pr_doc.reference_doctype, pr_doc.reference_name)
					if order:
						frappe.local.flags.redirect_location = f"/thank-you?sales_order={order}"
						raise frappe.Redirect
				else:
					context.error = (result or {}).get("message") or _("Error finalising order")
			except frappe.Redirect:
				raise
			except ImportError:
				# Webshop app not installed on this site — nothing to finalise.
				_debug("webshop app not installed; skipping handle_payment_success")
			except Exception as exc:  # noqa: BLE001
				_debug(f"handle_payment_success error: {exc!r}\n{frappe.get_traceback()}")
				context.error = _("Error creating order. Please contact support.")
	elif intent_doc.status in ("failed", "canceled"):
		context.status = "failed"
		context.error = intent_doc.error_message or _("Payment was declined")
	elif attempt >= _MAX_PENDING_LOADS:
		# Nothing came, and nothing is coming while the shopper watches. Say it,
		# instead of spinning for ever on a gateway that is still `waiting`
		# because they returned without paying.
		context.status = "unconfirmed"
	else:
		context.status = "pending"
		# //// Neoffice — the reload keeps the token, or the guard above would refuse it (maintenance#1731).
		from urllib.parse import urlencode

		context.retry_url = "/payrexx/success?" + urlencode(
			{"payment_intent": intent_name, "access_token": access_token or "", "try": attempt + 1}
		)

	# The page is reached by all three return URLs, so its title has to follow the
	# state rather than the route. Leaving it as "Success" printed that word above
	# "Paiement non reçu", which reads as a contradiction to the one person it
	# matters to.
	context.title = {
		"success": _("Payment Successful"),
		"failed": _("Payment Error"),
		"unconfirmed": _("Payment not received"),
	}.get(context.status, _("Payment Processing"))
	if context.error:
		context.title = _("Payment Error")

	_debug(f"END status={context.status} error={context.error}")
	return context
