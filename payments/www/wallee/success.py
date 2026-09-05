# //// Neoffice — added file (no upstream equivalent). Controller of `/wallee/success`:
# //// find the Payment Intent (or the legacy `payment_request`), refresh its status from
# //// Wallee through the driver — Wallee can take a few seconds to confirm after the
# //// redirect — then on `succeeded` finalise the Sales Order via
# //// `webshop.controllers.payment_handler.handle_payment_success` and redirect to
# //// /thank_you. Renamed from `www/wallee_success.py`: Frappe's www resolver maps
# //// `/wallee/success` to the path hierarchy, so the underscore file was a 404 for every
# //// returning buyer even though the payment had succeeded server-side.
# //// Commits: ce478ca 2026-05-28 "fix(wallee-web): /wallee/success and /wallee/failed routes (404 → working)"
# ////          7f412d0 2026-06-08 "fix(wallee): route success debug to the bench logger instead of the Error Log doctype"
# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""``/wallee/success`` — landing page after a Wallee web checkout.

//// Neoffice — added file. Its docstring carried a French "ou" and French spaced
//// colons until 2026-09-04 (RULE #00): everything inside a source file is English.
URL: ``/wallee/success?payment_intent=PI-2026-XXXXX`` (preferred)
   or ``/wallee/success?payment_request=ACC-PRQ-XXX`` (legacy compat — looks up
      the Payment Intent through ``reference_name``).
Flow:
1. Lookup the Payment Intent.
2. Refresh its status from Wallee via the driver (handles the race where Wallee
   may take a few seconds to confirm after the redirect).
3. If status reached ``succeeded`` → call
   ``webshop.controllers.payment_handler.handle_payment_success`` to finalise
   the Sales Order, redirect to ``/thank_you``.
4. Otherwise show the pending / error template with appropriate context.
"""

from __future__ import annotations

import time

import frappe
from frappe import _

no_cache = 1


def _debug(msg: str) -> None:
	"""Emit a short Wallee debug line to the bench log (not the Error Log doctype).

	Previously this inserted an Error Log record per call, flooding the doctype with
	non-error debug noise. Routed to the logger so it no longer pollutes Error Log.
	"""
	frappe.logger("wallee").debug(msg)


def _find_intent_name(form_dict) -> str | None:
	"""Find the Payment Intent name from URL params."""
	intent_name = form_dict.get("payment_intent")
	if intent_name:
		return intent_name

	# Legacy compat: caller passed payment_request — look up the PI by reference.
	payment_request = form_dict.get("payment_request")
	if payment_request:
		return frappe.db.get_value(
			"Payment Intent",
			{"reference_doctype": "Payment Request", "reference_name": payment_request},
			"name",
		)
	return None


def _refresh_status(intent_doc, max_retries: int = 9, retry_delay: int = 3) -> None:
	"""Poll the Wallee transaction state until terminal or retries exhausted.

	~27s window (9 × 3s) — Wallee can take 10-20s to move a freshly-paid
	transaction from PROCESSING to FULFILL, especially under load. The page
	also auto-refreshes every 3s while pending (see wallee_success.html), so a
	transaction that settles after this window is still caught on the next
	page load.
	"""
	from payments.drivers.registry import resolve_driver
	from payments.drivers.wallee.terminal_driver import _state_value, _TX_STATE_TO_STATUS

	if not intent_doc.provider_intent_id:
		return

	driver = resolve_driver(intent_doc.provider, intent_doc.channel)
	tx_service, *_rest = driver._services()  # type: ignore[attr-defined]

	for attempt in range(max_retries):
		try:
			tx = tx_service.get_payment_transactions_id(
				int(intent_doc.provider_intent_id), driver._wallee_provider.space_id  # type: ignore[attr-defined]
			)
			state_value = _state_value(tx.state)
			target = _TX_STATE_TO_STATUS.get(state_value or "")
			if target and intent_doc.status != target:
				intent_doc.transition_to(
					target,
					event_source="redirect",
					payload_excerpt=f"wallee tx {intent_doc.provider_intent_id} state={state_value}",
					ignore_invalid=True,
				)
				intent_doc.reload()
			# Stop polling once we reach a terminal state.
			if intent_doc.status in ("succeeded", "failed", "canceled", "refunded"):
				return
		except Exception as exc:  # noqa: BLE001 — keep polling
			_debug(f"sync error (attempt {attempt + 1}): {exc!r}")

		if attempt < max_retries - 1:
			time.sleep(retry_delay)


def get_context(context):  # noqa: ANN001
	intent_name = _find_intent_name(frappe.form_dict)
	_debug(f"START payment_intent={intent_name}")

	# Initialise template variables.
	context.intent = None
	context.payment_request = None
	context.sales_order = None
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

	# If the Payment Intent links a Payment Request and that request is already
	# paid, short-circuit straight to /thank_you (handles double-clicks on the
	# return URL).
	if intent_doc.reference_doctype == "Payment Request" and intent_doc.reference_name:
		pr_status = frappe.db.get_value(
			"Payment Request", intent_doc.reference_name, ["status", "reference_doctype", "reference_name"]
		)
		if pr_status:
			status, pr_ref_dt, pr_ref_name = pr_status
			if status in ("Paid", "Completed") and pr_ref_dt == "Sales Order":
				_debug(f"PR already paid, redirecting to {pr_ref_name}")
				frappe.local.flags.redirect_location = f"/thank_you?sales_order={pr_ref_name}"
				raise frappe.Redirect

	# Refresh status from Wallee with retry (Wallee may lag by a few seconds).
	_refresh_status(intent_doc)
	_debug(f"after refresh status={intent_doc.status}")

	if intent_doc.status == "succeeded":
		context.status = "success"
		# Finalize Sales Order via webshop's payment handler (legacy path
		# preserved — handler is webshop's responsibility, we just trigger it).
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
					# Fallback: lookup the Sales Order from the Payment Request.
					pr = frappe.get_doc("Payment Request", intent_doc.reference_name)
					if pr.reference_doctype == "Sales Order":
						frappe.local.flags.redirect_location = (
							f"/thank_you?sales_order={pr.reference_name}"
						)
						raise frappe.Redirect
				else:
					context.error = (result or {}).get("message") or _(
						"Error finalising order"
					)
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
		# Still pending (requires_action / processing) — render the pending UI.
		context.status = "pending"

	_debug(f"END status={context.status} error={context.error}")
	return context
