# //// Neoffice — added file (no upstream equivalent). The TWINT SDK's Client, for the six calls Neoffice makes
# //// (maintenance#1721): enrol the cash register once per process (the SDK's own static cache; the hub's bridge,
# //// one PHP process per call, enrolled before every call), then start, monitor, confirm, cancel or reverse an
# //// order, and the system status.
# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""The calls of TWINT's Merchant service, as the SDK makes them."""

from __future__ import annotations

import threading
import uuid as uuid_module

from payments.twint_engine import messages, transport, values

_ENROLLED: set = set()
_LOCK = threading.Lock()


def forget_enrollments() -> None:
	"""Tests: start again as a fresh process would."""
	with _LOCK:
		_ENROLLED.clear()


class Client:
	"""One merchant's client: its .p12, its store, its cash register, an environment and a service version."""

	def __init__(
		self,
		certificate: bytes,
		password: str,
		store_uuid,
		cash_register_id=None,
		environment: str = "production",
		version: str = messages.DEFAULT_VERSION,
		*,
		service_host: str | None = None,
		ca_file: str | None = None,
	):
		self.version = str(version or messages.DEFAULT_VERSION)
		messages.version_info(self.version)
		self.store_uuid = values.uuid(store_uuid)
		self.cash_register = values.cash_register_id(self.store_uuid, cash_register_id)
		self.environment = transport.environment_of(environment)
		self.host = service_host or transport.HOSTS[self.environment]
		self.ca_file = ca_file
		self._certificate = certificate
		self._password = password
		self._connection = None
		self.calls: list[str] = []

	# -- plumbing ----------------------------------------------------------------------------------------------------

	def close(self) -> None:
		if self._connection is not None:
			self._connection.close()

	def _call(self, operation: str, body: str, *, cancel: bool = False) -> messages.Answer:
		if self._connection is None:
			context = transport.client_context(self._certificate, self._password, self.ca_file)
			self._connection = transport.Connection(context, self.host)
		payload = messages.envelope(self.version, str(uuid_module.uuid4()), body)
		exchange = self._connection.post(
			messages.path(self.version), payload, operation, messages.user_agent(self.version)
		)
		self.calls.append(operation)
		return messages.read(exchange.body, self.version, operation, cancel=cancel)

	def enroll(self) -> None:
		key = (self.environment, self.host, self.cash_register)
		with _LOCK:
			if key in _ENROLLED:
				return
		self._call(
			"EnrollCashRegister", messages.enroll_cash_register(self.store_uuid, self.cash_register)
		)
		with _LOCK:
			_ENROLLED.add(key)

	# -- the calls ---------------------------------------------------------------------------------------------------

	def check_system_status(self) -> str:
		answer = self._call("CheckSystemStatus", messages.check_system_status(self.store_uuid))
		return values.non_empty(answer.text("Status"))

	def start_order(self, reference: str, rappen: int) -> dict:
		self.enroll()
		answer = self._call(
			"StartOrder", messages.start_order(self.store_uuid, self.cash_register, rappen, reference)
		)
		fields = messages.order_fields(answer, nested=False, pairing=True)
		fields["pairing_token"] = str(values.uint(answer.int("Token")))
		values.qr_code(answer.text("QRCode"))
		return fields

	def reverse_order(self, reference: str, order_uuid: str, rappen: int) -> dict:
		self.enroll()
		answer = self._call(
			"StartOrder",
			messages.reverse_order(self.store_uuid, self.cash_register, rappen, reference, order_uuid),
		)
		return messages.order_fields(answer, nested=False, pairing=False)

	def monitor_order(self, order_uuid: str) -> dict:
		self.enroll()
		answer = self._call(
			"MonitorOrder", messages.monitor_order(self.store_uuid, self.cash_register, order_uuid)
		)
		return messages.order_fields(answer, nested=True, pairing=True)

	def confirm_order(self, order_uuid: str, rappen: int) -> dict:
		self.enroll()
		answer = self._call(
			"ConfirmOrder", messages.confirm_order(self.store_uuid, self.cash_register, order_uuid, rappen)
		)
		return messages.order_fields(answer, nested=True, pairing=False)

	def cancel_order(self, order_uuid: str) -> dict:
		self.enroll()
		answer = self._call(
			"CancelOrder",
			messages.cancel_order(self.store_uuid, self.cash_register, order_uuid),
			cancel=True,
		)
		return messages.order_fields(answer, nested=True, pairing=False)
