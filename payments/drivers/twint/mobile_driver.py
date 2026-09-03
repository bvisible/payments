# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""TWINT on the operator's phone — the same QR as the shop, shown on site.

The customer scans a merchant-presented QR with their TWINT app; the bridge
registers the payment and the pollers in :mod:`payments.api.twint` settle it. That
is :class:`TwintWebDriver` to the letter, which is why this driver adds no behaviour
of its own. It exists as a **channel**: a payment taken at the customer's door is not
a webshop order, and reports, fees and the shop's own settlement hook
(``_finalize_webshop_sales_order`` only runs on ``twint_web``) all want them apart.
"""

from __future__ import annotations

from payments.drivers.base import PaymentChannelBase
from payments.drivers.twint.web_driver import TwintProvider, TwintWebDriver

CHANNEL = "twint_mobile"


class TwintMobileChannel(PaymentChannelBase):
	code = CHANNEL
	capabilities = {
		"supports_refund": True,
		"supports_partial_refund": True,
		"supports_tip": False,
		"async": True,
		# The phone draws the QR; the customer's own TWINT app does the rest.
		"requires_qr_scan": True,
		"requires_device": False,
		"requires_redirect": False,
	}


class TwintMobileDriver(TwintWebDriver):
	"""Merchant-presented QR, rendered by the mobile app instead of a browser."""

	code = CHANNEL

	@classmethod
	def from_docs(cls, provider_doc, channel_doc, binding_doc):  # noqa: ANN001
		return cls(TwintProvider(provider_doc), TwintMobileChannel(), settings_doc=binding_doc)
