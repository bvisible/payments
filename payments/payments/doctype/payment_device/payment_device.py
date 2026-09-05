# //// Neoffice — added file (no upstream equivalent). `Payment Device` is a physical
# //// card terminal (BBPOS WisePOS E, Stripe Reader S700, Worldline T630) attached to
# //// one `Provider Channel Settings` binding — ADR-004 §4. Upstream has no notion of
# //// a reader whatsoever. The check only warns instead of refusing, so a channel
# //// added later is not locked out.
# //// Commits: e32ecf5 2026-05-13 "feat(payments): Phase 1 — unified payment driver layer (Provider × Channel × Driver)"
# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE

import frappe
from frappe import _
from frappe.model.document import Document

from payments.setup.payment_channels import CHANNELS

# //// Neoffice — a Payment Device is a card reader, so it belongs on a channel that
# //// presents a card, and `ui_kind` is what says so in the channel registry. Until
# //// 2026-09-04 the guard read `channel in ("web", "billing")`, two codes this app has
# //// never provisioned — `payments/setup/payment_channels.py` ships `terminal`,
# //// `twint_web`, `wallee_web`, `payrexx_web`, `payrexx_tap_to_pay`,
# //// `stripe_tap_to_pay` and `twint_mobile` — so the warning could not fire once and a
# //// reader bound to a hosted-redirect channel went through in silence. Derived from
# //// CHANNELS rather than listed here so a channel added there is covered by writing it
# //// once.
_CARD_PRESENT_UI_KIND = "card_present_modal"


def _channel_codes_without_devices() -> set[str]:
	"""Shipped channel codes that take no physical device.

	A redirect to a hosted page and a QR the customer scans with their phone have no
	reader to address. Anything not shipped by this app is deliberately absent from
	the set: an unknown channel is tolerated rather than warned about, so a merchant
	adding their own is not nagged.
	"""
	return {
		spec["channel_code"] for spec in CHANNELS if spec.get("ui_kind") != _CARD_PRESENT_UI_KIND
	}


class PaymentDevice(Document):
	"""A physical payment device (terminal/reader) attached to a Provider×Channel.

	Examples: Stripe BBPOS WisePOS E, Stripe Reader S700, Worldline T630.
	"""

	def validate(self):
		self._validate_channel_supports_devices()

	def _validate_channel_supports_devices(self):
		# //// Neoffice — only card-present channels make sense for a device. We don't
		# //// hard-fail (extensibility): an unknown channel passes, a shipped web or
		# //// QR one warns. Read `("web", "billing")` until 2026-09-04 — two codes
		# //// this app has never provisioned, so the warning could not fire once.
		if not self.provider_channel_settings:
			return
		channel = frappe.db.get_value(
			"Provider Channel Settings", self.provider_channel_settings, "channel"
		)
		# //// Neoffice — the set is derived from the shipped registry, not listed here.
		if channel in _channel_codes_without_devices():
			frappe.msgprint(
				_(
					"Channel '{0}' is not typically used with physical devices. "
					"This is allowed but may indicate a configuration mistake."
				).format(channel),
				indicator="orange",
				alert=True,
			)
