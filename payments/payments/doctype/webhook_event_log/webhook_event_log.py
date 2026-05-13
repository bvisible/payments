# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE

from frappe.model.document import Document
from frappe.utils import now_datetime


class WebhookEventLog(Document):
	"""Records every raw webhook event received from a Payment Provider.

	The DB uniqueness on `event_id` (the DocType name) provides idempotent dedup:
	a second insertion with the same event_id will fail at the database level.
	"""

	def before_insert(self):
		if not self.received_at:
			self.received_at = now_datetime()
