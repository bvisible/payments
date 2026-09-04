#//// Neoffice — added file (no upstream equivalent). The Single the mobile app reads
#//// to know whether an intervention can be paid for at the customer's door, and with
#//// what: a card tapped on the phone (Stripe Tap to Pay) or a QR for the customer's
#//// TWINT app. Saving keeps the matching `Provider Channel Settings` bindings in
#//// step, so the driver registry resolves without anyone editing a binding by hand.
#//// The `in_install` guard exists because `frappe.installer.init_singles` saves
#//// every Single before after_install, when no Payment Channel exists yet (7a0f7ca).
#//// Upstream is a web-checkout hub: no terminal, no tap-to-pay, no QR bridge.
#//// Commits: d06eb26 2026-09-03 "feat(mobile): encaisser sur place par Stripe Tap to Pay et par QR TWINT, réglés en un seul endroit"
#////          7a0f7ca 2026-09-03 "fix(install): provision the shipped Payment Channels on a fresh site"
# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""Mobile Payment Settings — the one switch for collecting on site.

The mobile app asks the server whether an intervention can be paid for at the
customer's door, and with what. Two toggles answer, and nothing else does: a card
tapped on the phone (Stripe Terminal, Tap to Pay) and a QR for the customer's TWINT
app. Each needs an account — a Payment Provider — and the card one needs the Stripe
Terminal Location the phones attach to.

Saving keeps the Provider Channel Settings bindings in step, so the driver registry
resolves the right driver without anyone having to know that a binding exists. The
bindings are still the technical truth: a merchant who disables a toggle here has the
binding disabled too, and the app stops offering the method on the next refresh.
"""

from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.model.document import Document

CARD_CHANNEL = "stripe_tap_to_pay"
TWINT_CHANNEL = "twint_mobile"
CARD_DRIVER = "payments.drivers.stripe.tap_to_pay_driver.StripeTapToPayDriver"
TWINT_DRIVER = "payments.drivers.twint.mobile_driver.TwintMobileDriver"
STRIPE_WEBHOOK = "/api/method/payments.api.webhook_stripe.handle"


class MobilePaymentSettings(Document):
	def validate(self) -> None:
		if self.enable_tap_to_pay:
			_require_provider(self.tap_to_pay_provider, "stripe", _("Tap to Pay needs a Stripe Payment Provider"))
			if not str(self.stripe_location or "").strip().startswith("tml_"):
				frappe.throw(_("The Stripe Terminal Location id starts with tml_"))
		if self.enable_twint:
			_require_provider(self.twint_provider, "twint", _("TWINT on the phone needs a TWINT Payment Provider"))

	def on_update(self) -> None:
		if frappe.flags.in_install:
			# ``frappe.installer.init_singles`` saves every Single once during
			# ``bench install-app`` — before after_install and before any patch, so no
			# Payment Channel exists yet and nobody has chosen a provider: there is no
			# binding to keep in step. The channels are provisioned right after, from
			# after_install (payments.setup.payment_channels).
			return
		_sync_binding(
			provider=self.tap_to_pay_provider,
			channel=CARD_CHANNEL,
			driver_class=CARD_DRIVER,
			enabled=bool(self.enable_tap_to_pay),
			config={"location_id": (self.stripe_location or "").strip()},
			webhook_endpoint=STRIPE_WEBHOOK,
		)
		_sync_binding(
			provider=self.twint_provider,
			channel=TWINT_CHANNEL,
			driver_class=TWINT_DRIVER,
			enabled=bool(self.enable_twint),
		)


def _require_provider(name: str | None, needle: str, message: str) -> None:
	"""The chosen provider must exist, be enabled, and be of the expected kind.

	The kind is read off ``driver_class`` rather than a type field: it is the one
	thing every provider record carries, and it names the family unambiguously.
	"""
	if not name:
		frappe.throw(message)
	row = frappe.db.get_value("Payment Provider", name, ["enabled", "driver_class"], as_dict=True)
	if not row:
		frappe.throw(_("Payment Provider {0} does not exist").format(name))
	if not row.enabled:
		frappe.throw(_("Payment Provider {0} is disabled").format(name))
	if needle not in (row.driver_class or "").lower():
		frappe.throw(message)


def _sync_binding(
	*,
	provider: str | None,
	channel: str,
	driver_class: str,
	enabled: bool,
	config: dict | None = None,
	webhook_endpoint: str | None = None,
) -> None:
	"""One enabled binding per channel, on the chosen provider — or none.

	Other providers' bindings on the same channel are disabled rather than deleted:
	an intent created under them still points at a record, and switching accounts
	back later costs a click instead of a re-creation.
	"""
	if not frappe.db.exists("Payment Channel", channel):
		# The provisioning patch has not run on this site yet. Refusing loudly beats
		# a binding that points at nothing.
		frappe.throw(_("Payment Channel {0} is missing — run bench migrate").format(channel))

	for row in frappe.get_all(
		"Provider Channel Settings", filters={"channel": channel}, fields=["name", "provider", "enabled"]
	):
		if row.provider != provider and row.enabled:
			frappe.db.set_value("Provider Channel Settings", row.name, "enabled", 0)

	if not provider:
		return

	name = frappe.db.get_value("Provider Channel Settings", {"provider": provider, "channel": channel}, "name")
	values = {
		"enabled": 1 if enabled else 0,
		"driver_class": driver_class,
		"config_json": json.dumps(config or {}, indent=2),
	}
	if webhook_endpoint:
		values["webhook_endpoint"] = webhook_endpoint

	if name:
		doc = frappe.get_doc("Provider Channel Settings", name)
		doc.update(values)
		doc.flags.ignore_permissions = True
		doc.save()
	else:
		frappe.get_doc({"doctype": "Provider Channel Settings", "provider": provider, "channel": channel, **values}).insert(
			ignore_permissions=True
		)
