# //// Neoffice — added file (no upstream equivalent). One clearing account per payment provider (13.11):
# //// gateways and till modes of Payrexx and Wallee were booked to the Stripe account (and Payrexx's terminal
# //// to the generic card account), so a bookkeeper could not tell the three payouts apart. Each company gets
# //// « Compte d'attente Payrexx » / « Compte d'attente Wallee » where it needs one, beside the other clearing
# //// accounts, and only what booked to an account that is not its provider's own moves. An account the merchant
# //// or the bookkeeper gave a provider for itself stays; nothing already booked moves. Idempotent.
import frappe


def execute():
	"""Give each payment provider its own clearing account, company by company, and say what changed."""
	if not frappe.db.exists("DocType", "Payment Gateway Account"):
		return  # payments without ERPNext: no chart of accounts

	from payments.setup import clearing_accounts

	for report in clearing_accounts.separate():
		print(clearing_accounts.said(report))
