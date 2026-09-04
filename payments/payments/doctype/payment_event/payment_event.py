#//// Neoffice — added file (no upstream equivalent). `Payment Event` is the
#//// append-only log of the Payment Intent state machine (ADR-004 §6): one row per
#//// transition, with its source (api / webhook / poll / manual) and the
#//// `Webhook Event Log` that caused it. It is what makes a disputed payment
#//// reconstructible; upstream keeps no transition history at all.
#//// Commits: e32ecf5 2026-05-13 "feat(payments): Phase 1 — unified payment driver layer (Provider × Channel × Driver)"
# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE

from frappe.model.document import Document


class PaymentEvent(Document):
	"""An append-only log of FSM transitions for a Payment Intent.

	Records are immutable (no write permission for any role beyond create).
	"""

	pass
