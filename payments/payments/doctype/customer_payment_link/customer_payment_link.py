# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE

import frappe
from frappe import _
from frappe.model.document import Document


class CustomerPaymentLink(Document):
	"""Maps an ERPNext Customer to its identity at a Payment Provider.

	Used for saved payment methods (Stripe Customer object, etc.).
	"""

	def validate(self):
		self._enforce_single_default_per_provider()

	def _enforce_single_default_per_provider(self):
		# Only one default payment method per (customer, provider).
		if not self.is_default_payment_method:
			return
		existing = frappe.db.exists(
			"Customer Payment Link",
			{
				"customer": self.customer,
				"provider": self.provider,
				"is_default_payment_method": 1,
				"name": ("!=", self.name),
			},
		)
		if existing:
			frappe.throw(
				_("Customer {0} already has a default payment method for provider {1}: {2}").format(
					self.customer, self.provider, existing
				)
			)
