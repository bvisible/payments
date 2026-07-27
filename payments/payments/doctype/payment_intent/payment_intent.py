# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE

import json

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime

# Allowed FSM transitions. Maps current_status -> set of acceptable next statuses.
# Terminal states (succeeded, canceled, refunded) cannot transition further except:
#   succeeded -> refunded (after a successful refund call)
#   failed -> succeeded/processing (PSP retried on the same intent, see below)
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
	"requires_action": {"processing", "succeeded", "failed", "canceled"},
	"processing": {"succeeded", "failed", "canceled"},
	"succeeded": {"refunded"},
	# `failed` is NOT final. Card-present declines are often SOFT: a reader that
	# answers `online_or_offline_pin_required` re-prompts the cardholder for the
	# PIN and the PSP retries on the SAME provider intent. Stripe then emits
	# `payment_intent.succeeded` AFTER `payment_intent.payment_failed`. While
	# `failed` was terminal we dropped that second event: the card was charged,
	# the cashier saw "declined" and rang the sale again -> double charge, and
	# the captured amount never reached the books.
	"failed": {"processing", "succeeded", "canceled"},
	# `canceled` stays final: cancelling is merchant/cardholder initiated and the
	# PSP will not capture afterwards.
	"canceled": set(),
	"refunded": set(),
}

TERMINAL_STATUSES: set[str] = {"succeeded", "failed", "canceled", "refunded"}


class PaymentIntent(Document):
	"""A unified fact-table row representing one payment attempt.

	Created on `create_intent()`, updated by webhook handlers or polls.
	"""

	def before_insert(self):
		# Stamp created_at if missing.
		if not self.created_at:
			self.created_at = now_datetime()

	def validate(self):
		self._validate_amount()
		self._validate_currency()
		self._validate_metadata_json()
		self._validate_next_action_payload()

	def _validate_amount(self):
		if self.amount is None or self.amount <= 0:
			frappe.throw(_("Amount must be a positive integer (smallest currency unit)"))

	def _validate_currency(self):
		if not self.currency:
			frappe.throw(_("Currency is required"))
		code = self.currency.strip().upper()
		if len(code) != 3 or not code.isalpha():
			frappe.throw(_("Currency must be an ISO 4217 3-letter code (got: {0})").format(self.currency))
		self.currency = code

	def _validate_metadata_json(self):
		if not self.metadata_json:
			return
		try:
			value = json.loads(self.metadata_json)
		except (ValueError, TypeError) as exc:
			frappe.throw(_("Metadata JSON is invalid: {0}").format(str(exc)))
		if not isinstance(value, dict):
			frappe.throw(_("Metadata JSON must be a JSON object (dict)"))

	def _validate_next_action_payload(self):
		if not self.next_action_payload:
			return
		try:
			json.loads(self.next_action_payload)
		except (ValueError, TypeError) as exc:
			frappe.throw(_("Next Action Payload JSON is invalid: {0}").format(str(exc)))

	# --------------------------------------------------------------------------------
	# FSM helpers
	# --------------------------------------------------------------------------------

	def transition_to(
		self,
		new_status: str,
		*,
		event_source: str = "api",
		webhook_event_log: str | None = None,
		payload_excerpt: str | None = None,
		error_code: str | None = None,
		error_message: str | None = None,
		ignore_invalid: bool = False,
	) -> bool:
		"""Atomically move this Intent to `new_status`, logging a Payment Event.

		Returns True if the transition happened, False if it was rejected by the FSM
		and `ignore_invalid=True`. Raises if `ignore_invalid=False` and the transition
		is not allowed.
		"""
		current = self.status
		if new_status == current:
			# Idempotent — webhook replays should not fail.
			return False

		allowed = ALLOWED_TRANSITIONS.get(current, set())
		if new_status not in allowed:
			if ignore_invalid:
				# Never drop a rejected transition silently: a swallowed
				# `succeeded` means money captured by the PSP that nobody
				# booked. Callers pass ignore_invalid to survive replays, not
				# to hide state loss.
				frappe.log_error(
					"Payment Intent: rejected FSM transition",
					f"intent={self.name} {current} -> {new_status} "
					f"(source={event_source}, webhook_event_log={webhook_event_log})\n"
					f"payload={payload_excerpt or ''}",
				)
				return False
			frappe.throw(
				_("Invalid FSM transition for Payment Intent {0}: {1} -> {2}").format(
					self.name, current, new_status
				)
			)

		# Update fields atomically.
		self.status = new_status
		if error_code is not None:
			self.error_code = error_code
		if error_message is not None:
			self.error_message = error_message
		if new_status in TERMINAL_STATUSES:
			# Always re-stamp: a `failed` intent that the PSP later settles must
			# carry the time of the SETTLEMENT, not of the earlier decline.
			self.completed_at = now_datetime()
		self.save(ignore_permissions=True)

		# Log the FSM event. Imported lazily to avoid circular at module load time.
		event = frappe.get_doc(
			{
				"doctype": "Payment Event",
				"intent": self.name,
				"from_status": current,
				"to_status": new_status,
				"event_source": event_source,
				"webhook_event_log": webhook_event_log,
				"payload_excerpt": (payload_excerpt or "")[:140],
			}
		)
		event.insert(ignore_permissions=True)
		return True

	def get_metadata(self) -> dict:
		"""Return parsed metadata dict."""
		if not self.metadata_json:
			return {}
		try:
			return json.loads(self.metadata_json)
		except (ValueError, TypeError):
			return {}
