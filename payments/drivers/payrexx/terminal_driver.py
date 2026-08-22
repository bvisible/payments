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

import time
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

class PayrexxCancelUnconfirmed(Exception):
	"""The terminal never confirmed a cancellation, so it may still take a card.

	Raised — not returned — because the intent API treats *any* driver response as
	a successful cancellation and only preserves the status on an exception.
	"""


#: Raw ECR statuses meaning the reader is no longer holding the payment.
#:
#: Deliberately expressed in the ECR vocabulary rather than as FSM states, because
#: the two answer different questions. "Is the hardware free?" and "did the money
#: move?" are independent here: ``TERMINATED`` settles the first and says nothing
#: about the second — a paid card and a cancelled request end there alike (see
#: NEEDS_HUMAN in _common.py). Mapping through the FSM to decide whether a
#: cancellation landed is what let a paid sale be recorded as cancelled.
_READER_FREE = frozenset(
	{"TERMINATED", "CANCELLED", "CANCELED", "FAILED", "DECLINED", "EXPIRED", "SUCCESS"}
)

#: The one raw status that proves money moved. It is reported only during a short
#: window around the payment itself, then gives way to ``TERMINATED`` for good —
#: which is why a cancellation has to watch for it as it polls rather than read it
#: back afterwards.
_PAID = "SUCCESS"

#: How long to insist before handing the question to reconciliation.
#:
#: A NexGo N86 on 4G took anywhere from 8 s to ~40 s to report a cancellation
#: (measured 2026-08-21), so this window deliberately does NOT cover the worst
#: case: a till request that blocks for 40 s is its own failure. Ten seconds
#: catches the common case; beyond that the driver raises, the intent keeps its
#: current status, and `poll_pending_payrexx_transactions` settles it.
_CANCEL_POLL_ATTEMPTS = 5
_CANCEL_POLL_SECONDS = 2.0

#: Poll index after which the cancel is sent a second time — late enough that the
#: terminal is past the window where it swallows one, early enough to still be
#: confirmed inside the loop.
_CANCEL_RETRY_AFTER = 1


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
		"""Read a terminal payment back and map its status onto the FSM.

		This used to return ``processing`` no matter what the terminal reported,
		because ``payment_status`` had no documented vocabulary and guessing at one
		would have been worse than stalling. Payrexx confirmed the nine values in
		writing on 2026-08-18, so that caution is now debt rather than prudence: with
		it in place, a payment the terminal had already ended stayed ``requires_action``
		forever and the till waited on nothing. Observed on the real N86 — a cancelled
		payment reported ``TERMINATED`` while the driver still said ``processing``.

		An **unmapped** value still yields ``processing``: that part was right, and is
		what keeps an unexpected status from being reported as an outcome we cannot
		back up. The raw value always travels in ``next_action_payload`` for the till.
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

		target = map_status(str(payment.status) if payment.status else None)
		# TERMINATED is unmapped on purpose, not by omission: it is the end state of
		# a paid payment *and* of a cancelled one, so it cannot drive a transition.
		# The till keeps waiting and the webhook decides.
		known_ambiguous = str(payment.status or "").strip().lower() == "terminated"
		if target is None and payment.status and not known_ambiguous:
			# Worth knowing about: either Payrexx added a value, or the firmware
			# reports one their support did not list. Either way the payment stalls
			# until someone maps it, and a stalled payment nobody hears about is how
			# a till ends up waiting on a screen that will never change.
			frappe.log_error(
				"Payrexx terminal: unmapped payment_status",
				f"payment={provider_intent_id} status={payment.status!r} — add it to "
				f"_STATUS_TO_FSM in payments/drivers/payrexx/_common.py",
			)

		return DriverResponse(
			status=target or "processing",
			provider_intent_id=provider_intent_id,
			# Once the payment is over, the till has nothing left to display.
			next_action_type=(
				"none" if target in ("succeeded", "failed", "canceled", "refunded")
				else "display_card_present_modal"
			),
			next_action_payload={
				"terminal_status": payment.status,
				"slip": list(payment.slip),
				# The same slip as named fields, so the till can print its own receipt
				# rather than the terminal's. The device's paper carries the acceptance
				# platform's branding; ours does not, and everything legally required
				# on a card receipt is in here — amount, date, masked PAN,
				# authorisation, terminal and merchant.
				"receipt": payment.receipt,
			},
			raw=payment.raw,
		)

	def cancel_intent(self, provider_intent_id: str) -> DriverResponse:
		"""Cancel a payment still in progress on the terminal, and prove it took.

		``POST .../cancel`` answers with the payment's **current** state, not the
		outcome of the request — it returns ``IN_PROGRESS`` whether the terminal
		accepted the cancellation or ignored it. Worse, a cancel sent within a
		second or so of the payment request is silently swallowed: verified on a
		NexGo N86 on 2026-08-21, where the first cancel left the terminal waiting
		for a card and only a second attempt terminated it.

		Reporting ``canceled`` on the strength of that reply is how a till ends up
		believing an order was abandoned while the terminal is still live in front
		of the customer — who taps, pays, and is charged for a sale nobody
		recorded. So the cancellation is confirmed by polling, retried once, and
		only reported as ``canceled`` when the terminal actually says so.
		"""
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
				payment, saw_payment = self._await_cancellation(
					client, serial, provider_intent_id, payment
				)
		except Exception as exc:  # noqa: BLE001
			return error_response(exc, provider_intent_id=provider_intent_id)

		raw_status = str(payment.status or "").strip().upper()

		if saw_payment or raw_status == _PAID:
			# The customer tapped while we were cancelling. The money moved, so this
			# is a sale, not a cancellation — and saying otherwise would book a paid
			# transaction as abandoned.
			return DriverResponse(
				status="succeeded",
				provider_intent_id=provider_intent_id,
				next_action_payload={"terminal_status": payment.status, "raced_payment": True},
				raw=payment.raw,
			)

		if raw_status not in _READER_FREE:
			# Raise rather than return: `payments.api.intent.cancel_intent` marks the
			# intent `canceled` on any response at all, and only leaves the status
			# untouched when the driver raises. Returning a "failed" DriverResponse
			# here would be recorded as a successful cancellation — the exact outcome
			# this method exists to prevent.
			raise PayrexxCancelUnconfirmed(
				f"terminal {serial} did not confirm the cancellation of "
				f"{provider_intent_id} (still {payment.status!r}) — it may still take "
				"a card, so the intent must not be marked canceled"
			)

		return DriverResponse(
			status="canceled",
			provider_intent_id=provider_intent_id,
			next_action_payload={"terminal_status": payment.status},
			raw=payment.raw,
		)

	def _await_cancellation(self, client, serial, payment_id, payment):  # noqa: ANN001
		"""Poll until the terminal settles, retrying the cancel once mid-way.

		The retry exists because of the swallowed-first-cancel behaviour described
		in :meth:`cancel_intent`. Repeating it is safe in the way that matters — a
		cancel never takes money, unlike the payment request, which is why *that*
		one is never retried. It is not, however, a no-op: once the payment has
		settled the endpoint answers ``400 "Payment abort impossible, payment not
		in progress"``. That is a normal outcome of the race, not a failure, so it
		is swallowed and the poll below decides.
		"""
		saw_payment = False
		for attempt in range(_CANCEL_POLL_ATTEMPTS):
			raw_status = str(payment.status or "").strip().upper()
			if raw_status == _PAID:
				# Caught the narrow window in which the reader admits it took money.
				# Missing it means reading TERMINATED later and being unable to tell
				# a sale from an abandoned request, so it is remembered here.
				saw_payment = True
			if raw_status in _READER_FREE:
				return payment, saw_payment
			time.sleep(_CANCEL_POLL_SECONDS)
			if attempt == _CANCEL_RETRY_AFTER:
				try:
					client.ecr.cancel_payment(serial, payment_id)
				except Exception:  # noqa: BLE001 - the poll below is the real check
					pass
			payment = client.ecr.get_payment(serial, payment_id)
		return payment, saw_payment

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
						# A void that did nothing still answers 200 with the untouched
						# payment — verified on a NexGo N86, where the call returned
						# status SUCCESS, type CHARGE and no reversalStatus while the
						# money stayed exactly where it was. "No exception" is not
						# evidence, and reporting a refund that never happened is worse
						# than failing: the till would mark the sale returned while the
						# customer got nothing back.
						reversed_ok = bool(payment.reversal_status) or (
							str(payment.type or "").upper() not in ("", "CHARGE")
						)
						if not reversed_ok:
							raise ValueError(
								f"void left the payment untouched "
								f"(status={payment.status!r} type={payment.type!r} "
								f"reversalStatus={payment.reversal_status!r})"
							)
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

				target = self._locate_transaction(client, intent, provider_intent_id)
				if target is None:
					return DriverResponse(
						status="failed",
						provider_intent_id=provider_intent_id,
						error_code="transaction_not_found",
						error_message=_(
							"No Payrexx transaction found for terminal payment {0}"
						).format(provider_intent_id),
					)
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

	def _locate_transaction(self, client, intent, provider_intent_id):  # noqa: ANN001, ANN202
		"""Find the Payrexx transaction behind a terminal payment.

		Looking it up by ``referenceId`` is the obvious way and does not work here:
		a POS-Terminal delivery comes back with ``referenceId`` **and**
		``invoice.purpose`` both empty — our ``paymentReference`` is not echoed
		anywhere in the payload. Verified on a NexGo N86 on 2026-08-20, contradicting
		what Payrexx support wrote two days earlier.

		So the transaction is matched on what *is* present: the terminal's serial and
		the amount, taking the most recent. That is sound in practice — one device,
		one amount, within the lifetime of a single intent — but it is a heuristic,
		and it is why the reference question is worth pressing with Payrexx.
		"""
		if intent:
			by_reference = client.transaction.find_by_reference(intent["name"])
			if by_reference:
				return max(by_reference, key=lambda t: t.id or 0)

		serial = self._serial(intent.get("device") if intent else None)
		amount = (intent or {}).get("amount")
		candidates = [
			t
			for t in client.transaction.list(limit=50)
			if t.pos_serial_number == serial and (amount is None or t.amount == amount)
		]
		if not candidates:
			return None
		return max(candidates, key=lambda t: t.id or 0)

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
