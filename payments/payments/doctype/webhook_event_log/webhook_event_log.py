# //// Neoffice — added file (no upstream equivalent). `Webhook Event Log` stores every
# //// raw provider webhook, with the provider's `event_id` as the DocType name so that
# //// idempotency is a database constraint and not a code path (ADR-004 §7). Upstream
# //// verifies signatures inline inside each PSP's settings doctype and keeps no
# //// shared, dedupable event trail.
# //// Commits: e32ecf5 2026-05-13 "feat(payments): Phase 1 — unified payment driver layer (Provider × Channel × Driver)"
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
