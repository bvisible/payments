# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE

import frappe
from frappe import _
from frappe.model.document import Document


class TwintBridgeSettings(Document):
	"""Per-merchant TWINT configuration.

	One record per TWINT merchant on the calling site. The actual P12
	certificate file is NOT stored here — it lives on neoservice in
	``/home/frappe/twint-certs/<merchant_uuid>.p12``. Only the unlock password
	is stored here (encrypted via Frappe Password field).
	"""

	def validate(self):
		merchant = (self.merchant_uuid or "").strip()
		if not merchant:
			frappe.throw(_("Merchant UUID is required"))
		if "/" in merchant or ".." in merchant or "\\" in merchant:
			frappe.throw(
				_("Merchant UUID must not contain path separators (got: {0})").format(merchant)
			)
		self.merchant_uuid = merchant
