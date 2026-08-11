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
   ``webshop.controllers.payment_handler`` and redirect to ``/thank_you``.
4. Otherwise render the pending or failed state; the template auto-refreshes while
   pending, so a payment that settles after our polling window is still caught.
"""

from __future__ import annotations

import time

import frappe
from frappe import _

no_cache = 1


def _debug(msg: str) -> None:
	"""Emit a debug line to the bench log, not the Error Log doctype.

	Logging debug noise into Error Log floods a doctype operators need for real
	errors — a lesson already learned on the Wallee return page.
	"""
	frappe.logger("payrexx").debug(msg)


def _refresh_status(intent_doc, max_retries: int = 6, retry_delay: int = 3) -> None:
	"""Poll the gateway until the Payment Intent is final, or retries run out.

	About an 18-second window (6 × 3 s). Shorter than the Wallee equivalent because
	Payrexx settles faster in practice, and because its rate limit (~600 requests /
	5 minutes per account) makes a long per-shopper poll expensive across a fleet.
	The template refreshes itself while pending, so a slower settlement is still
	picked up on the next page load, and the webhook remains the real backstop.
	"""
	from payments.drivers.registry import resolve_driver

	if not intent_doc.provider_intent_id:
		return

	driver = resolve_driver(intent_doc.provider, intent_doc.channel)

	for attempt in range(max_retries):
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
			if intent_doc.status in ("succeeded", "failed", "canceled", "refunded"):
				return
		except Exception as exc:  # noqa: BLE001 - keep polling, never break the page
			_debug(f"sync error (attempt {attempt + 1}): {exc!r}")

		if attempt < max_retries - 1:
			time.sleep(retry_delay)


def get_context(context):  # noqa: ANN001
	intent_name = frappe.form_dict.get("payment_intent")
	_debug(f"START payment_intent={intent_name}")

	context.intent = None
	context.error = None
	context.status = "pending"

	if not intent_name:
		context.error = _("Missing payment reference")
		return context

	if not frappe.db.exists("Payment Intent", intent_name):
		context.error = _("Payment reference not found")
		_debug(f"PI not found: {intent_name}")
		return context

	intent_doc = frappe.get_doc("Payment Intent", intent_name)
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
		if pr and pr.status in ("Paid", "Completed") and pr.reference_doctype == "Sales Order":
			_debug(f"PR already paid, redirecting to {pr.reference_name}")
			frappe.local.flags.redirect_location = f"/thank_you?sales_order={pr.reference_name}"
			raise frappe.Redirect

	_refresh_status(intent_doc)
	_debug(f"after refresh status={intent_doc.status}")

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
					if pr_doc.reference_doctype == "Sales Order":
						frappe.local.flags.redirect_location = (
							f"/thank_you?sales_order={pr_doc.reference_name}"
						)
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
	else:
		context.status = "pending"

	_debug(f"END status={context.status} error={context.error}")
	return context
