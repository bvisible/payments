# //// Neoffice — added file (no upstream equivalent). See read_the_missing_certificate_expiry.py.
import datetime

import frappe
from frappe.tests.utils import FrappeTestCase

from payments.drivers.twint import local_engine
from payments.patches.v15_15 import read_the_missing_certificate_expiry as patch
from payments.tests import test_audit_correctness as audit

DOCTYPE = "Twint Bridge Settings"
UUID = "patch-v15-15-merchant"


class TestTheMissingCertificateExpiryIsRead(FrappeTestCase):
	"""A deployed certificate with no end date gets the one in the file the instance keeps."""

	def setUp(self):
		frappe.set_user("Administrator")
		self.expires = datetime.date(2028, 3, 31)

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def a_merchant(self, *, password="right", opens_with="right", dated=None, kept=True):
		"""A deployed merchant: its certificate is kept on the instance under `password`, `opens_with` is stored."""
		from frappe.utils.password import set_encrypted_password

		doc = frappe.new_doc(DOCTYPE)
		values = {
			"name": UUID,
			"merchant_uuid": UUID,
			"display_label": "patch merchant",
			"store_uuid": "store",
			"enabled": 1,
			"certificate_deployed": 1,
		}
		if dated:
			values["certificate_expires_on"] = dated
		doc.update(values)
		doc.db_insert()
		set_encrypted_password(DOCTYPE, UUID, opens_with, "p12_password")
		if kept:
			local_engine.store_certificate(
				UUID, audit.TestATwintCertificateDateIsReadOrSaidUnknown.a_certificate(password, self.expires)
			)

	def stored_date(self):
		return frappe.db.get_value(DOCTYPE, UUID, "certificate_expires_on")

	def test_a_deployed_certificate_without_a_date_gets_the_end_date_of_its_file(self):
		self.a_merchant()
		patch.execute()
		self.assertEqual(self.stored_date(), self.expires)

	def test_a_date_that_is_already_there_is_never_overwritten(self):
		self.a_merchant(dated="2027-01-15")
		patch.execute()
		self.assertEqual(self.stored_date(), datetime.date(2027, 1, 15))

	def test_a_certificate_that_the_stored_password_does_not_open_stays_without_a_date(self):
		self.a_merchant(opens_with="a typo")
		patch.execute()
		self.assertIsNone(self.stored_date())

	def test_a_merchant_whose_certificate_is_not_kept_on_the_instance_is_left_alone(self):
		self.a_merchant(kept=False)
		patch.execute()
		self.assertIsNone(self.stored_date())

	def test_running_it_twice_changes_nothing(self):
		self.a_merchant()
		patch.execute()
		first = self.stored_date()
		patch.execute()
		self.assertEqual(self.stored_date(), first)
