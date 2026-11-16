# //// Neoffice — added file (no upstream equivalent). A provider's clearing account removed after
# //// give_each_payment_provider_its_own_clearing_account made it (neoffice-maintenance#2747, 16.11): its gateways and
# //// till modes still booked to a name that no longer existed, so a payment through them could not be reconciled and
# //// the invoice stayed unpaid. The account is put back under its label (the first free number) and they book to it
# //// again, said on each document. A till mode named after no provider is left and named. Nothing already booked
# //// moves; a site where every account exists changes nothing. Idempotent.
import frappe


def execute():
	"""Put back each provider's clearing account that no longer exists, and book its gateways and till modes to it."""
	if not frappe.db.exists("DocType", "Payment Gateway Account"):
		return  # payments without ERPNext: no chart of accounts

	from payments.setup import clearing_accounts

	for report in clearing_accounts.put_back():
		print(clearing_accounts.said_put_back(report))
