# //// Neoffice — added file (no upstream equivalent). A TWINT merchant set up before
# //// `certificate_expires_on` existed is deployed with no end date: the instance's own expiry reminder skips
# //// it, and so does the hub's `twint-certificate-expiry` check, which stays on a Warning (« no end date stored »)
# //// while the till pays fine, and which will say nothing the day TWINT stops accepting the certificate.
# //// The date is in the P12 the instance keeps and the password that opens it is in `__Auth`:
# //// it is read HERE, on the instance, so neither leaves the server. Idempotent: only the rows without a date
# //// are touched, a file the stored password does not open is left as it is (the check keeps flagging it, which
# //// is true), and nothing is written but the date.
import frappe

DOCTYPE = "Twint Bridge Settings"


def execute():
	"""Store the end date of every deployed TWINT certificate that has none, read from the copy the instance keeps."""
	if not frappe.db.exists("DocType", DOCTYPE):
		return

	from payments.drivers.twint import local_engine

	rows = frappe.get_all(
		DOCTYPE,
		filters={"certificate_deployed": 1, "certificate_expires_on": ["is", "not set"]},
		fields=["name", "merchant_uuid"],
	)
	for row in rows:
		raw = local_engine.stored_certificate(row.merchant_uuid or row.name)
		if not raw:
			continue  # the instance holds no copy (only the hub does): nothing to read here
		expiry = frappe.get_doc(DOCTYPE, row.name)._read_cert_expiry(raw)
		if not expiry:
			continue
		frappe.db.set_value(DOCTYPE, row.name, "certificate_expires_on", expiry, update_modified=False)
		print(f"{DOCTYPE} {row.name}: certificate_expires_on = {expiry}")
