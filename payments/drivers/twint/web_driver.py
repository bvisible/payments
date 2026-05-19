# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""TWINT consumer-initiated driver (webshop QR checkout).

Same underlying TWINT SDK calls as the POS terminal driver
(:class:`TwintPHPBridgeDriver`) — both flows ultimately call
``Twint\\Sdk\\Client::startOrder`` and ``monitorOrder``. The difference is who
displays the pairing artifact:

- POS terminal (channel ``qr_bridge``) : the cashier shows the QR / pairing
  token on the cash register screen; the bridge driver is the caller of record.
- Webshop consumer (channel ``twint_web`` — this driver) : the QR is rendered
  on the buyer's checkout page (browser); the buyer scans with their own TWINT
  app. The same ``register_payment`` bridge command returns a ``pairing_token``,
  which we wrap into an inline SVG QR for the frontend.

We subclass :class:`TwintPHPBridgeDriver` to reuse ``_call_bridge``,
``_resolve_merchant_context`` and the status mapping table.
"""

from __future__ import annotations

import json
from io import BytesIO
from typing import Any

import frappe
from frappe import _

from payments.drivers.base import (
	DriverResponse,
	IntentRequest,
	PaymentChannelBase,
)
from payments.drivers.twint.php_bridge_driver import TwintPHPBridgeDriver
from payments.drivers.twint.provider import TwintProvider


class TwintWebChannel(PaymentChannelBase):
	code = "twint_web"
	capabilities = {
		"supports_refund": True,  # SDK ``reverseOrder`` works for both flows
		"supports_partial_refund": True,
		"supports_tip": False,
		"async": True,
		"requires_qr_scan": True,  # browser-side: QR for the buyer's app
		"requires_device": False,
	}


def _pairing_token_to_svg(pairing_token: str) -> str:
	"""Encode the TWINT pairing token as an inline SVG QR (no <?xml?> header).

	Frappe ships ``qrcode`` with ``SvgPathImage`` — a single ``<path>`` element
	rather than namespaced ``<svg:rect>`` tags (which browsers won't render
	when injected via ``innerHTML``). The output is also smaller and uses a
	clean ``viewBox`` so the CSS ``width: 200px`` styling actually works.
	"""
	import qrcode
	from qrcode.image.svg import SvgPathImage

	q = qrcode.QRCode(
		version=None,
		error_correction=qrcode.constants.ERROR_CORRECT_M,
		box_size=10,
		border=2,
	)
	q.add_data(pairing_token or "")
	q.make(fit=True)

	buf = BytesIO()
	q.make_image(image_factory=SvgPathImage).save(buf)
	svg = buf.getvalue().decode("utf-8", errors="replace")

	# Strip ``<?xml ... ?>`` prolog so callers can drop the SVG directly into HTML.
	if svg.startswith("<?xml"):
		svg = svg.split("?>", 1)[1].lstrip()
	return svg


class TwintWebDriver(TwintPHPBridgeDriver):
	"""TWINT consumer flow — same bridge, same SDK call, QR rendered for the browser."""

	code = "twint_web"

	@classmethod
	def from_docs(cls, provider_doc, channel_doc, binding_doc):  # noqa: ANN001
		provider = TwintProvider(provider_doc)
		channel = TwintWebChannel()
		return cls(provider, channel, settings_doc=binding_doc)

	# ------------------------------------------------------------------------
	# Driver contract
	# ------------------------------------------------------------------------

	def create_intent(self, request: IntentRequest) -> DriverResponse:
		"""Call ``register_payment`` on the bridge, wrap pairing_token as SVG QR.

		The buyer's frontend will:
		1. Display the SVG QR (next_action_payload.qr_svg).
		2. Subscribe to ``payment.intent.<intent_name>.updated`` via SocketIO.
		3. Redirect to ``/thank_you`` on the realtime ``succeeded`` event.

		Status sync is driven by :func:`payments.api.twint.fast_poll_intent`
		(enqueued here) plus the existing per-minute
		:func:`poll_pending_twint_transactions` as a safety net.
		"""
		merchant_uuid = self._resolve_merchant_uuid(request.metadata)
		if not merchant_uuid:
			return DriverResponse(
				status="failed",
				error_code="no_merchant_uuid",
				error_message="No TWINT merchant_uuid resolved (metadata, binding, or provider default missing)",
			)

		result = self._call_bridge(
			"register_payment",
			merchant_uuid,
			{
				"amount": int(request.amount),
				"currency": request.currency.upper(),
				"merchant_reference": request.intent_name,
			},
		)
		if not (result or {}).get("success"):
			return DriverResponse(
				status="failed",
				error_code=(result or {}).get("exception") or "twint_register_failed",
				error_message=(result or {}).get("error") or "TWINT register_payment failed",
				raw=result or {},
			)

		order_id = result.get("order_id")
		pairing_token = result.get("pairing_token") or ""

		# Stash merchant_uuid on the PI so cancel_intent / refund / poll can
		# find it later without re-resolving the binding. Use db_set (no
		# timestamp check) because api.intent.create_intent will save the doc
		# again right after the driver returns — saving here would race.
		try:
			existing_md_json = frappe.db.get_value("Payment Intent", request.intent_name, "metadata_json") or "{}"
			existing_md = json.loads(existing_md_json)
			if not isinstance(existing_md, dict):
				existing_md = {}
			existing_md["twint_merchant_uuid"] = merchant_uuid
			frappe.db.set_value(
				"Payment Intent",
				request.intent_name,
				"metadata_json",
				json.dumps(existing_md),
				update_modified=False,
			)
		except frappe.DoesNotExistError:
			# Intent record not persisted yet — caller is responsible for it.
			pass

		# Enqueue the fast-poll loop so the buyer gets sub-5s feedback after
		# they confirm in the TWINT app (the per-minute cron is a safety net).
		try:
			frappe.enqueue(
				"payments.api.twint.fast_poll_intent",
				intent_name=request.intent_name,
				queue="short",
				job_id=f"twint-fast-poll-{request.intent_name}",
				deduplicate=True,
				enqueue_after_commit=True,
			)
		except Exception as exc:  # noqa: BLE001 — non-fatal, cron will catch
			frappe.log_error("twint fast_poll_intent enqueue failed", f"intent={request.intent_name}: {exc!r}")

		try:
			qr_svg = _pairing_token_to_svg(pairing_token)
		except Exception as exc:  # noqa: BLE001 — fall back to client-side QR if libs ever break
			frappe.log_error("twint QR SVG generation failed", repr(exc))
			qr_svg = ""

		return DriverResponse(
			status="requires_action",
			provider_intent_id=order_id,
			next_action_type="display_qr_payload",
			next_action_payload={
				"twint_order_id": order_id,
				"merchant_reference": request.intent_name,
				"pairing_token": pairing_token,
				"qr_svg": qr_svg,
			},
			raw=result,
		)
