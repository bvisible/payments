# //// Neoffice — added file (no upstream equivalent). What Stripe said about a card charge, kept where the shop reads it.
"""The receipt of a Stripe card charge: the server's own record of what Stripe answered.

The shop's card tile charges through `Stripe Settings.create_request`: the browser sends a card token, the SERVER asks
Stripe for the charge and reads the answer. That answer is the only proof, on the server, that money arrived: the
shop concludes a card payment on it, never on who asks (maintenance#1807).

The receipt is the Integration Request of the charge (it was already its log): once Stripe says the charge is
captured, the amount, the currency and the charge id Stripe itself reported are written to its `output`, and the
request is bound to the Payment Request it pays (`reference_doctype` / `reference_docname`). Nothing the browser sent
is read back as proof: `covers` compares what STRIPE reported with what the Payment Request asks.
"""

import json

import frappe
from frappe.utils import cint, flt

RECEIPT_SERVICE = "Stripe"


def stripe_minor_units(amount) -> int:
	"""An amount as `Stripe Settings.create_charge_on_stripe` hands it to Stripe: truncated, in cents."""
	return cint(flt(amount) * 100)


def write(integration_request, charge, payment_request=None) -> None:
	"""Record on the charge's Integration Request what Stripe answered, and bind it to the request it pays.

	`charge` is Stripe's answer, never the browser's data. `payment_request` is only a claim of the caller (the tile
	names the request it opened): it is bound only if it exists, and `covers` checks the amount in any case.
	"""
	integration_request.db_set(
		"output",
		json.dumps(
			{
				"charge": charge.id,
				"amount": charge.amount,
				"currency": (charge.currency or "").lower(),
				"captured": bool(charge.captured),
				"paid": bool(getattr(charge, "paid", False)),
			}
		),
		update_modified=False,
	)
	if payment_request and frappe.db.exists("Payment Request", payment_request):
		integration_request.db_set(
			{"reference_doctype": "Payment Request", "reference_docname": payment_request},
			update_modified=False,
		)


def covers(payment_request_name: str) -> bool:
	"""True when Stripe captured a charge for this very Payment Request, for the amount and the currency it asks.

	One charge must cover it all: two small charges do not add up to a request.
	"""
	if not payment_request_name:
		return False
	request = frappe.db.get_value(
		"Payment Request", payment_request_name, ["grand_total", "currency"], as_dict=True
	)
	if not request:
		return False
	expected = {stripe_minor_units(request.grand_total), int(round(flt(request.grand_total) * 100))}
	for output in frappe.get_all(
		"Integration Request",
		filters={
			"integration_request_service": RECEIPT_SERVICE,
			"status": "Completed",
			"reference_doctype": "Payment Request",
			"reference_docname": payment_request_name,
		},
		pluck="output",
	):
		try:
			receipt = json.loads(output or "{}")
		except ValueError:
			continue
		if (
			receipt.get("charge")
			and receipt.get("captured")
			and cint(receipt.get("amount")) in expected
			and (receipt.get("currency") or "").lower() == (request.currency or "").lower()
		):
			return True
	return False
