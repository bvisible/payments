# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""Payrexx Terminal driver — card present on a NexGo terminal (ECR).

Shares the ``terminal`` channel with Stripe Terminal and Wallee: the binding in
``Provider Channel Settings`` decides which driver a given POS profile resolves
to, so a till can keep TWINT on the PHP bridge while moving card to Payrexx, or
the reverse.

The ``Payment Device`` record carries the terminal's **serial number** in
``provider_device_id`` — where the Stripe driver stores ``tmr_xxx``.

Two things this driver deliberately does *not* do, both because getting them
wrong charges a customer twice:

.. warning::
   **It never retries a payment request.** Payrexx documents no idempotency
   header, and a live test confirmed the gateway endpoint happily creates
   duplicates for identical requests. The ``payrexx`` library keeps ``POST`` out
   of its retry set; on a transport failure it raises ``PayrexxTransportError``,
   which surfaces here as ``error_code="transport_error"`` — meaning the outcome
   is **unknown**, not failed. The caller must reconcile (poll the payment, or
   wait for the webhook and match on ``paymentReference``) rather than resend.

.. warning::
   **It does not trust the ECR status field.** Payrexx's OpenAPI declares
   ``payment_status`` as a bare string and enumerates no values, so it is recorded
   for the till UI but never mapped onto the FSM. Authoritative state comes from
   the transaction webhook (``type == "POS-Terminal"``), whose statuses are
   documented.

Payrexx documents no ECR sandbox, so a terminal payment cannot be exercised at
all until a NexGo arrives. To unblock that, a ``Payment Device`` whose
``device_type`` starts with ``simulated`` short-circuits every ECR call: the
driver returns a synthetic ``sim_<intent>`` payment id and POSNext's simulator
panel drives the state machine. Guarded on the device type **and** on the
provider being in ``test`` mode — see :meth:`PayrexxTerminalDriver._is_simulator`.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _

from payments.drivers.base import (
	DriverResponse,
	IntentRequest,
	PaymentChannelBase,
	PaymentDriverBase,
	WebhookResult,
)
from payments.drivers.payrexx._common import build_client, error_response, map_status
from payments.drivers.payrexx.provider import PayrexxProvider


class PayrexxTerminalChannel(PaymentChannelBase):
	code = "terminal"
	capabilities = {
		"supports_refund": True,
		"supports_partial_refund": True,
		"supports_tip": True,
		"async": True,
		"requires_device": True,
		"requires_redirect": False,
	}


class PayrexxTerminalDriver(PaymentDriverBase):
	"""Drives a paired NexGo terminal over the ECR API."""

	code = "payrexx.terminal"

	@classmethod
	def from_docs(cls, provider_doc, channel_doc, binding_doc):  # noqa: ANN001
		return cls(
			PayrexxProvider(provider_doc), PayrexxTerminalChannel(), settings_doc=binding_doc
		)

	# ------------------------------------------------------------------------
	# Internals
	# ------------------------------------------------------------------------

	@property
	def _payrexx_provider(self) -> PayrexxProvider:
		assert isinstance(self.provider, PayrexxProvider)
		return self.provider

	def _client(self):  # noqa: ANN202
		return build_client(self._payrexx_provider.provider_doc)

	def _device_row(self, device_id: str | None) -> dict[str, Any] | None:
		"""Resolve a Payment Device from either its record name or its serial.

		``payments.api.intent.create_intent`` hands the driver the device's
		``provider_device_id`` rather than the record name, while a caller reaching
		the driver directly usually holds the name. Both must work: assuming the
		name silently broke simulator detection for every payment coming through the
		till, since the lookup found no record and fell through to a real ECR call.
		"""
		if not device_id:
			return None
		fields = ["name", "device_type", "serial_number", "provider_device_id"]
		row = frappe.db.get_value("Payment Device", device_id, fields, as_dict=True)
		if row:
			return row
		for field in ("provider_device_id", "serial_number"):
			row = frappe.db.get_value("Payment Device", {field: device_id}, fields, as_dict=True)
			if row:
				return row
		return None

	def _is_simulator(self, device_id: str | None) -> bool:
		"""Whether this Payment Device stands in for hardware we do not have yet.

		Payrexx documents no ECR sandbox — unlike Stripe, which hands out
		``simulated-wpe`` readers — so a terminal payment cannot be exercised at all
		until a NexGo arrives. A device whose ``device_type`` starts with
		``simulated`` therefore short-circuits the ECR call: the driver returns a
		synthetic payment id and the till's simulator panel (POSNext's
		``pos_simulate_terminal_outcome``) drives the FSM from there.

		Guarded twice over, because a simulator that leaked into production would
		mark real invoices paid without any money moving:

		- the device must be explicitly typed ``simulated*``
		- the Payment Provider must be in ``test`` mode

		Both must hold; a ``simulated`` device on a live provider is refused.
		"""
		row = self._device_row(device_id)
		if not row:
			return False
		if not str(row.get("device_type") or "").lower().startswith("simulated"):
			return False

		mode = frappe.db.get_value(
			"Payment Provider", self._payrexx_provider.provider_doc.name, "mode"
		)
		if mode != "test":
			raise frappe.ValidationError(
				_(
					"Payment Device {0} is a simulator but Payment Provider {1} is in "
					"'{2}' mode. Simulated terminals are only allowed in test mode."
				).format(device_id, self._payrexx_provider.provider_doc.name, mode)
			)
		return True

	def _serial(self, device_id: str | None) -> str:
		"""Resolve the terminal serial number from a Payment Device id.

		``Payment Device`` carries both a dedicated ``serial_number`` field and the
		generic ``provider_device_id`` (where the Stripe driver stores ``tmr_xxx``).
		``serial_number`` wins because that is what it is for; ``provider_device_id``
		is the fallback so a device enrolled through the generic wizard still works.

		A value that matches no record is returned as-is, so a caller holding only
		the serial — a till reading it off the hardware — needs no round trip.
		"""
		if not device_id:
			raise frappe.ValidationError(
				_("Payrexx terminal payments require a Payment Device (terminal serial number)")
			)
		row = self._device_row(device_id)
		if not row:
			# Not a known device: assume the caller handed us the serial itself, which
			# a till reading it off the hardware legitimately does.
			return str(device_id)
		return str(row.get("serial_number") or row.get("provider_device_id") or device_id)

	# ------------------------------------------------------------------------
	# Driver contract
	# ------------------------------------------------------------------------

	def create_intent(self, request: IntentRequest) -> DriverResponse:
		"""Send a payment request to the paired terminal.

		Returns ``requires_action`` with ``next_action_type="display_card_present_modal"``,
		which is what POSNext's ``CardPresentDialog`` already renders — no frontend
		change is needed to accept a Payrexx terminal.
		"""
		try:
			serial = self._serial(request.device_id)
		except frappe.ValidationError as exc:
			return DriverResponse(
				status="failed", error_code="missing_device", error_message=str(exc)
			)

		metadata = request.metadata or {}
		tip = metadata.get("tip_amount")

		# Simulated device: skip the ECR call entirely and hand the till something
		# it can act on. Without this the payment dies on 404 Terminal not found,
		# because Payrexx offers no sandbox terminal to talk to.
		try:
			if self._is_simulator(request.device_id):
				return DriverResponse(
					status="requires_action",
					provider_intent_id=f"sim_{request.intent_name}",
					next_action_type="display_card_present_modal",
					next_action_payload={
						"device_id": request.device_id,
						"serial_number": serial,
						"terminal_status": "simulated_waiting",
						"simulated": True,
						"slip": [],
					},
					raw={"simulated": True, "amount": request.amount, "currency": request.currency},
				)
		except frappe.ValidationError as exc:
			# A simulator on a live provider — refuse loudly rather than charge nothing.
			return DriverResponse(
				status="failed", error_code="simulator_not_allowed", error_message=str(exc)
			)

		try:
			with self._client() as client:
				payment = client.ecr.create_payment(
					serial,
					amount=request.amount,
					currency=request.currency,
					payment_method=metadata.get("payment_method"),
					# The only thread linking this terminal payment back to the
					# order once the webhook arrives. Always sent.
					payment_reference=request.intent_name,
					purpose=metadata.get("purpose"),
					print_slip=metadata.get("print_slip"),
					tip_amount=int(tip) if tip else None,
					shop_items=metadata.get("shop_items") or None,
				)
		except Exception as exc:  # noqa: BLE001 - contract: never raise upward
			return error_response(exc)

		return DriverResponse(
			status="requires_action",
			provider_intent_id=payment.payment_id,
			next_action_type="display_card_present_modal",
			next_action_payload={
				"device_id": request.device_id,
				"serial_number": serial,
				# Reported for the till UI only — see the module warning.
				"terminal_status": payment.status,
				"slip": list(payment.slip),
			},
			raw=payment.raw,
		)

	def confirm_intent(self, provider_intent_id: str, **kwargs: Any) -> DriverResponse:
		"""Not applicable — the terminal confirms on its own once the card is read."""
		return self.get_status(provider_intent_id, device_id=kwargs.get("device_id"))

	def get_status(
		self, provider_intent_id: str, device_id: str | None = None
	) -> DriverResponse:
		"""Read a terminal payment back.

		The returned FSM status stays ``processing`` regardless of what the terminal
		reports, because ``payment_status`` has no documented vocabulary. The raw
		value travels in ``next_action_payload`` for the till to display.
		"""
		# A simulated payment has no ECR counterpart to read; the till's simulator
		# panel owns its state. Reported as still processing so the dialog keeps
		# waiting for the operator to accept or decline.
		if str(provider_intent_id).startswith("sim_"):
			return DriverResponse(
				status="processing",
				provider_intent_id=provider_intent_id,
				next_action_type="display_card_present_modal",
				next_action_payload={"terminal_status": "simulated_waiting", "simulated": True},
				raw={"simulated": True},
			)

		try:
			serial = self._serial(device_id or self._device_for_intent(provider_intent_id))
			with self._client() as client:
				payment = client.ecr.get_payment(serial, provider_intent_id)
		except Exception as exc:  # noqa: BLE001
			return error_response(exc, provider_intent_id=provider_intent_id)

		return DriverResponse(
			status="processing",
			provider_intent_id=provider_intent_id,
			next_action_type="display_card_present_modal",
			next_action_payload={
				"terminal_status": payment.status,
				"slip": list(payment.slip),
			},
			raw=payment.raw,
		)

	def cancel_intent(self, provider_intent_id: str) -> DriverResponse:
		"""Cancel a payment still in progress on the terminal."""
		if str(provider_intent_id).startswith("sim_"):
			return DriverResponse(
				status="canceled",
				provider_intent_id=provider_intent_id,
				next_action_payload={"terminal_status": "simulated_cancelled", "simulated": True},
				raw={"simulated": True},
			)

		try:
			serial = self._serial(self._device_for_intent(provider_intent_id))
			with self._client() as client:
				payment = client.ecr.cancel_payment(serial, provider_intent_id)
		except Exception as exc:  # noqa: BLE001
			return error_response(exc, provider_intent_id=provider_intent_id)

		return DriverResponse(
			status="canceled",
			provider_intent_id=provider_intent_id,
			next_action_payload={"terminal_status": payment.status},
			raw=payment.raw,
		)

	def refund(self, provider_intent_id: str, amount: int | None = None) -> DriverResponse:
		"""Return money for a terminal payment, preferring a void, falling back to refund.

		Payrexx offers two gestures that are not interchangeable, and their limits were
		confirmed by Payrexx support on 2026-08-18:

		- **void** — all-or-nothing, guaranteed for **three months**, with one exception:
		  on TWINT it only holds while the customer still has the same app and phone.
		- **refund** — partial or full, but ``POST /ecr/{sn}/payment/{id}/refund``
		  answers **501 Not Implemented on NexGo**, so it goes through the merchant
		  transaction API instead.

		Hence: a full return inside the window **tries** the void and falls back to a
		refund if the terminal refuses. Trying and falling back beats predicting, because
		the one documented exception — a TWINT customer who changed phones — is invisible
		from here. The earlier rule ("same day") was our guess before the answer, and it
		sent perfectly voidable payments down the refund path.
		"""
		if str(provider_intent_id).startswith("sim_"):
			# No Payrexx transaction exists behind a simulated payment, so there is
			# nothing to void or refund provider-side. Reported as refunded so the
			# till's return flow can be exercised end to end.
			return DriverResponse(
				status="refunded",
				provider_intent_id=provider_intent_id,
				next_action_payload={"method": "simulated", "simulated": True},
				raw={"simulated": True, "amount": amount},
			)

		intent = frappe.db.get_value(
			"Payment Intent",
			{"provider_intent_id": provider_intent_id},
			["name", "amount", "creation", "device"],
			as_dict=True,
		)
		is_full = amount is None or (intent and amount >= (intent.amount or 0))
		# Three months is what Payrexx guarantees; 89 days keeps a day of slack rather
		# than racing the boundary on the last afternoon.
		within_void_window = bool(intent) and frappe.utils.date_diff(
			frappe.utils.nowdate(), frappe.utils.getdate(intent.creation)
		) <= 89
		void_error: str | None = None

		try:
			with self._client() as client:
				if is_full and within_void_window:
					try:
						serial = self._serial(intent.get("device") if intent else None)
						payment = client.ecr.void_payment(serial, provider_intent_id)
						return DriverResponse(
							status="refunded",
							provider_intent_id=provider_intent_id,
							next_action_payload={"method": "void", "terminal_status": payment.status},
							raw=payment.raw,
						)
					except Exception as exc:  # noqa: BLE001 - a refused void is not the end
						# The money still has to come back. Recorded rather than swallowed:
						# a void that starts failing systematically means the window or the
						# TWINT rule changed, and that should be visible.
						void_error = repr(exc)
						frappe.log_error(
							"Payrexx void refused, falling back to refund",
							f"intent={intent['name'] if intent else '?'} "
							f"payment={provider_intent_id}: {exc!r}",
						)

				transactions = (
					client.transaction.find_by_reference(intent["name"]) if intent else []
				)
				if not transactions:
					return DriverResponse(
						status="failed",
						provider_intent_id=provider_intent_id,
						error_code="transaction_not_found",
						error_message=_(
							"No Payrexx transaction found for reference {0}"
						).format(intent["name"] if intent else provider_intent_id),
					)
				target = max(transactions, key=lambda t: t.id or 0)
				refunded = client.transaction.refund(target.id, amount=amount)
		except Exception as exc:  # noqa: BLE001
			return error_response(exc, provider_intent_id=provider_intent_id)

		status = map_status(str(refunded.status) if refunded.status else None)
		return DriverResponse(
			status=status or "processing",
			provider_intent_id=provider_intent_id,
			next_action_payload={"method": "refund", "void_error": void_error},
			raw=refunded.raw,
		)

	def handle_webhook(self, payload: bytes, headers: dict[str, str]) -> WebhookResult:
		"""Verify and parse a transaction webhook. Shared with the web driver."""
		from payments.api.webhook_payrexx import parse_delivery

		return parse_delivery(
			payload, headers, signing_key=self._payrexx_provider.webhook_signing_key
		)

	# ------------------------------------------------------------------------
	# Terminal management
	# ------------------------------------------------------------------------

	def pair_terminal(
		self, serial_number: str, pairing_code: str, *, cashier_name: str | None = None
	) -> dict[str, Any]:
		"""Pair a terminal with the account and return its reported configuration.

		The pairing code comes from the device: hamburger menu (☰, top left) →
		*Connect to cash register*. It is **short-lived and regenerates if the
		operator leaves that screen**, so the enrolment UI must call this
		immediately after the code is read.

		The response carries the terminal's own configuration — currency, language,
		point-of-sale name, timezone and whether tipping is enabled. Reading those
		off the device beats hard-coding an assumption per client, and
		``has_tipping`` is worth checking before sending a tip amount.
		"""
		with self._client() as client:
			client.ecr.pair(serial_number, pairing_code, cashier_name=cashier_name)
			pairing = client.ecr.get_pairing(serial_number)

		return {
			"serial_number": pairing.serial_number,
			"paired": pairing.paired,
			"cashier_name": pairing.cashier_name,
			"currency": pairing.currency,
			"language": pairing.language,
			"point_of_sale_name": pairing.point_of_sale_name,
			"timezone": pairing.timezone,
			"has_tipping": pairing.has_tipping,
		}

	def unpair_terminal(self, serial_number: str) -> None:
		"""Release a terminal from the account."""
		with self._client() as client:
			client.ecr.unpair(serial_number)

	def terminal_payment_methods(self, serial_number: str) -> Any:
		"""Ask the terminal which payment methods it accepts."""
		with self._client() as client:
			return client.ecr.payment_methods(serial_number)

	# ------------------------------------------------------------------------
	# Helpers
	# ------------------------------------------------------------------------

	def _device_for_intent(self, provider_intent_id: str) -> str | None:
		return frappe.db.get_value(
			"Payment Intent", {"provider_intent_id": provider_intent_id}, "device"
		)
