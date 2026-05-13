# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE

import frappe
from frappe import _
from frappe.model.document import Document


class PaymentDevice(Document):
	"""A physical payment device (terminal/reader) attached to a Provider×Channel.

	Examples: Stripe BBPOS WisePOS E, Stripe Reader S700, Worldline T630.
	"""

	def validate(self):
		self._validate_channel_supports_devices()

	def _validate_channel_supports_devices(self):
		# Only certain Channels make sense for devices (terminal, qr_bridge).
		# We don't hard-fail unknown channels (extensibility), but warn on web/billing.
		if not self.provider_channel_settings:
			return
		channel = frappe.db.get_value(
			"Provider Channel Settings", self.provider_channel_settings, "channel"
		)
		if channel in ("web", "billing"):
			frappe.msgprint(
				_(
					"Channel '{0}' is not typically used with physical devices. "
					"This is allowed but may indicate a configuration mistake."
				).format(channel),
				indicator="orange",
				alert=True,
			)
