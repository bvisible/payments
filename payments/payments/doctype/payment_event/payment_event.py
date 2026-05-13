# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE

from frappe.model.document import Document


class PaymentEvent(Document):
	"""An append-only log of FSM transitions for a Payment Intent.

	Records are immutable (no write permission for any role beyond create).
	"""

	pass
