# //// Neoffice — added file (no upstream equivalent). What the customer reads when she comes back from a
# //// hosted checkout (Wallee, Payrexx): one view for every state (paid, confirming, taking long, refused, left,
# //// not found, hold run out, refunded, error), composed here from the Payment Intent, so the pages of the two
# //// gateways say the same things in the same words. Until then each page had its own template: a bare « Payment
# //// Successful » with a reference, no currency on the amount, and « Return to Cart » for an appointment.
# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""The view of a payment's return page.

The page controllers (``www/wallee/success.py``, ``www/wallee/failed.py``, ``www/payrexx/success.py``) keep their own logic
(read the gateway, finalise the order, bound the polling) and set the same context variables as before; :func:`present` turns
them into ``context.view``, which ``templates/includes/payment_return.html`` draws.

What the payment was FOR is not this app's business: an app that knows (a booking, a membership) answers the hook
``payment_return_summary`` with a dict (see :func:`summary_of`) and the page tells it: the appointment, the day, what was paid
and what is left. No answer, and the page says only that the payment arrived.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt, fmt_money

#: A view is one of these. ``tone`` is the colour of its mark: ok (green), warn (amber), bad (red), mut (grey).
TONES = {
	"success": "ok",
	"pending": "warn",
	"unconfirmed": "warn",
	"failed": "bad",
	"canceled": "mut",
	"not_found": "mut",
	"released": "warn",
	"refunded": "ok",
	"error": "bad",
}


def money(amount, currency: str | None) -> str:
	"""An amount with its currency, written even where the site hides currency symbols: the customer is reading what she paid."""
	text = fmt_money(flt(amount), precision=2)
	return f"{currency or 'CHF'} {text}"


def summary_of(intent) -> dict | None:
	"""What the payment was for, as an app that knows says it, or None.

	Each function hooked on ``payment_return_summary`` gets the Payment Intent and answers a dict or None; the first dict wins.
	The keys the page reads, all optional: ``title``, ``when``, ``steps`` (a list of ``{"time", "label", "person"}`` or
	``{"pause"}``), ``paid``, ``rest``, ``email``, ``first_name``, ``cancelled`` (what was booked is gone), ``hold_seconds``
	(how long it is still held), ``retry_url`` (where to pay again), ``events`` (for the calendar file). A hook that fails
	never breaks the page.
	"""
	if not intent:
		return None
	for method in frappe.get_hooks("payment_return_summary") or []:
		try:
			found = frappe.get_attr(method)(intent.name)
		except Exception:  # noqa: BLE001 - the page says less, it never fails
			frappe.log_error(
				"Payment return summary failed",
				f"intent={intent.name} hook={method}\n{frappe.get_traceback()}",
			)
			continue
		if found:
			return found
	return None


def kind_of(context) -> str:
	"""The state the controller left in the context (``intent``, ``status``, ``error``), as one of :data:`TONES`."""
	intent = context.get("intent")
	error = context.get("error")
	status = context.get("status")
	if not intent:
		return "not_found"
	if intent.status == "refunded":
		return "refunded"
	if status == "failed" or intent.status in ("failed", "canceled"):
		return "canceled" if intent.status == "canceled" else "failed"
	if error:
		return "error"
	if status == "unconfirmed":
		return "unconfirmed"
	if status == "pending" or intent.status != "succeeded":
		return "pending"
	return "success"


def calendar_url(intent) -> str:
	"""Where the customer downloads her appointments as a calendar file; the token rides along, as it does for every return page."""
	from urllib.parse import urlencode

	query = {"payment_intent": intent.name}
	token = frappe.form_dict.get("access_token")
	if token:
		query["access_token"] = token
	return "/api/method/payments.api.payment_return.calendar?" + urlencode(query)


def present(
	context, *, reload_url: str | None = None, check_url: str | None = None, detail: str | None = None
) -> None:
	"""Turn what a return page's controller decided into ``context.view`` and the page title.

	`reload_url`: where a page that is waiting reloads itself (None: the same address). `check_url`: where « check now » goes.
	`detail`: what the gateway said, when it said something.
	"""
	intent = context.get("intent")
	kind = kind_of(context)
	summary = summary_of(intent) if intent else None
	if (
		summary
		and summary.get("cancelled")
		and kind in ("success", "pending", "unconfirmed", "failed", "canceled", "refunded")
	):
		kind = "released"
	currency = intent.currency if intent else None
	amount = money(flt(intent.amount) / 100, currency) if intent else None
	view = {
		"kind": kind,
		"tone": TONES[kind],
		"amount": amount,
		"reference": intent.name if intent else None,
		"summary": summary,
		"reload_url": reload_url,
		"celebrate": kind == "success",
		"hold_seconds": (summary or {}).get("hold_seconds")
		if kind in ("failed", "canceled", "pending", "unconfirmed")
		else None,
		"detail": detail or (context.get("error") if kind in ("failed", "error") else None),
	}
	view.update(_words(kind, intent, summary, amount, check_url))
	context.view = view
	context.title = view["title"]
	# The page opens on its own composition: the site's title band above it would print the title twice, and the breadcrumb would show
	# a customer « Wallee » (the band honours `show_page_header`, as the 404 does; frappe's breadcrumb, `no_breadcrumbs`).
	context.show_page_header = False
	context.no_breadcrumbs = 1


def _words(
	kind: str, intent, summary: dict | None, amount: str | None, check_url: str | None
) -> dict:
	"""The title, the sentences and the actions of one state."""
	back = {"label": _("Back to the site"), "url": "/"}
	write = {"label": _("Write to us"), "url": "/contact"}
	retry = (summary or {}).get("retry_url") or "/checkout"

	if kind == "success":
		if summary:
			first = summary.get("first_name")
			words = {
				"title": _("Your appointment is confirmed"),
				"text": _("Thank you {0}, your payment has arrived.").format(first)
				if first
				else _("Thank you, your payment has arrived."),
				"primary": {"label": _("Add to my calendar"), "url": calendar_url(intent)}
				if summary.get("events")
				else back,
				"secondary": back if summary.get("events") else None,
				"foot": _(
					"Something in the way? Reply to the confirmation email: the salon moves or cancels your appointment."
				),
			}
		else:
			words = {
				"title": _("Payment received"),
				"text": _("Thank you, your payment of {0} has arrived.").format(amount)
				if amount
				else _("Thank you, your payment has arrived."),
				"primary": {"label": _("Continue"), "url": "/"},
			}
		return words

	if kind == "pending":
		return {
			"title": _("We are confirming your payment"),
			"text": _("Your bank has answered. All that is left is to record it: it takes a few seconds."),
			"note": _("Do not close this page: it updates by itself."),
			"slow_title": _("This is taking longer than expected"),
			"slow_text": _(
				"You can close this page. As soon as the payment is confirmed you receive an email."
			),
			"slow_note": _("If you have already paid, do not pay a second time: wait for that email."),
		}

	if kind == "unconfirmed":
		return {
			"title": _("This is taking longer than expected"),
			"text": _("You can close this page. As soon as the payment is confirmed you receive an email."),
			"note": _("If you have already paid, do not pay a second time: wait for that email."),
			"primary": {"label": _("Check now"), "url": check_url} if check_url else None,
			"secondary": {"label": _("Try paying again"), "url": retry},
		}

	if kind == "failed":
		return {
			"title": _("The payment did not go through"),
			"text": _("The payment was not completed. Nothing has been charged."),
			"primary": {"label": _("Try paying again"), "url": retry},
			"secondary": back,
		}

	if kind == "canceled":
		return {
			"title": _("You left the payment"),
			"text": _("No amount has been taken.")
			+ (" " + _("Your appointment is not confirmed yet.") if summary else ""),
			"primary": {"label": _("Resume the payment"), "url": retry},
			"secondary": back,
		}

	if kind == "released":
		given = intent is not None and intent.status == "refunded"
		return {
			"title": _("This time is no longer held"),
			"text": _("The time to pay ran out and the time slot was given back."),
			"note": _("Your payment has been given back to you.")
			if given
			else _(
				"If your payment arrived after that, it is given back to you automatically: you receive an email."
			),
			"primary": {"label": _("Choose another time"), "url": (summary or {}).get("retry_url") or "/"},
		}

	if kind == "refunded":
		return {
			"title": _("Payment refunded"),
			"text": _("Your payment of {0} has been given back to you.").format(amount)
			if amount
			else _("Your payment has been given back to you."),
			"note": _(
				"It goes back to the card or the account that paid, and can take a few days to show at your bank."
			),
			"primary": back,
		}

	if kind == "error":
		return {
			"title": _("We could not finish your order"),
			"text": _(
				"Something went wrong after the payment. Please write to us with the reference below: we will find your payment."
			),
			"primary": write,
			"secondary": back,
		}

	# not_found
	return {
		"title": _("We cannot find this payment here"),
		"text": _(
			"This link only works in the browser where the payment was made. That does not mean nothing was paid."
		),
		"todo": [
			{
				"lead": _("Did you pay?"),
				"text": _("Look in your inbox: a confirmation email arrives. Check your junk mail too."),
			},
			{
				"lead": _("Nothing after ten minutes?"),
				"text": _(
					"Write to us with the time of the payment: we will find your payment and your appointment."
				),
			},
			{
				"lead": _("Did you not pay?"),
				"text": _("Nothing has been charged. Start your booking again."),
			},
		],
		"primary": write,
		"secondary": back,
	}


def calendar_file(summary: dict) -> str:
	"""The summary's ``events`` as an iCalendar file: one event each, in the salon's own time (no zone: the calendar reads it where she is)."""
	from frappe.utils import get_datetime, now_datetime

	def text(value) -> str:
		return (
			str(value or "")
			.replace("\\", "\\\\")
			.replace(";", "\\;")
			.replace(",", "\\,")
			.replace("\n", "\\n")
		)

	def stamp(value) -> str:
		return get_datetime(value).strftime("%Y%m%dT%H%M%S")

	lines = [
		"BEGIN:VCALENDAR",
		"VERSION:2.0",
		"PRODID:-//Neoffice//Payment return//FR",
		"CALSCALE:GREGORIAN",
		"METHOD:PUBLISH",
	]
	now = now_datetime().strftime("%Y%m%dT%H%M%S")
	for index, event in enumerate(summary.get("events") or []):
		lines += [
			"BEGIN:VEVENT",
			f"UID:{event.get('uid') or index}-{stamp(event['start'])}@payment-return",
			f"DTSTAMP:{now}",
			f"DTSTART:{stamp(event['start'])}",
			f"DTEND:{stamp(event['end'])}",
			f"SUMMARY:{text(event.get('title'))}",
		]
		if event.get("description"):
			lines.append(f"DESCRIPTION:{text(event['description'])}")
		if event.get("place"):
			lines.append(f"LOCATION:{text(event['place'])}")
		lines.append("END:VEVENT")
	lines.append("END:VCALENDAR")
	return "\r\n".join(lines) + "\r\n"
