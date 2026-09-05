# //// Neoffice — added file (no upstream equivalent). In-memory test double,
# //// registered as the `driver_class` of a mock Payment Provider, so the whole
# //// intent API and FSM can be exercised without any external PSP. Upstream has
# //// no driver contract, hence nothing to fake; it also serves as the smallest
# //// complete example of the contract (see docs/adding-a-new-psp.md).
# //// Commits: e32ecf5 2026-05-13 "feat(payments): Phase 1 — unified payment driver layer (Provider × Channel × Driver)"
# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""In-memory mock driver used by tests and developer sandboxes.

Lets us exercise the full API + FSM without depending on any external service.
Register via :class:`MockDriver` as the ``driver_class`` of a Payment Provider
record named e.g. ``mock``.
"""

from __future__ import annotations

import uuid
from typing import Any

from payments.drivers.base import (
	DriverResponse,
	IntentRequest,
	PaymentChannelBase,
	PaymentDriverBase,
	PaymentProviderBase,
	WebhookResult,
)


class MockProvider(PaymentProviderBase):
	name = "mock"

	def get_credentials(self) -> dict[str, Any]:
		return self.provider_doc.get_credentials()

	def health_check(self) -> dict[str, Any]:
		return {"ok": True, "provider": self.name}

	def list_supported_channels(self) -> list[str]:
		return ["terminal", "web", "qr_bridge"]


class MockChannel(PaymentChannelBase):
	def __init__(self, channel_doc) -> None:  # noqa: ANN001
		self.code = channel_doc.channel_code
		self.capabilities = channel_doc.get_capabilities() if hasattr(channel_doc, "get_capabilities") else {}


class MockDriver(PaymentDriverBase):
	"""Mock driver that succeeds immediately on confirm.

	Useful for testing the API layer and FSM without an external dependency.
	"""

	code = "mock"

	@classmethod
	def from_docs(cls, provider_doc, channel_doc, binding_doc):  # noqa: ANN001
		# Build provider/channel wrappers from the Frappe docs.
		provider = MockProvider(provider_doc)
		channel = MockChannel(channel_doc)
		return cls(provider, channel, settings_doc=binding_doc)

	def create_intent(self, request: IntentRequest) -> DriverResponse:
		fake_id = f"mock_pi_{uuid.uuid4().hex[:16]}"
		return DriverResponse(
			status="requires_action",
			provider_intent_id=fake_id,
			client_secret=f"mock_secret_{uuid.uuid4().hex[:8]}",
			next_action_type="requires_confirmation",
			next_action_payload={"hint": "call confirm_intent to simulate success"},
			raw={"echo": request.__dict__},
		)

	def confirm_intent(self, provider_intent_id: str, **kwargs: Any) -> DriverResponse:
		return DriverResponse(
			status="succeeded",
			provider_intent_id=provider_intent_id,
			raw={"confirmed": True, **kwargs},
		)

	def cancel_intent(self, provider_intent_id: str) -> DriverResponse:
		return DriverResponse(
			status="canceled",
			provider_intent_id=provider_intent_id,
			raw={"canceled": True},
		)

	def refund(self, provider_intent_id: str, amount: int | None = None) -> DriverResponse:
		return DriverResponse(
			status="refunded",
			provider_intent_id=provider_intent_id,
			raw={"refunded": True, "amount": amount},
		)

	def handle_webhook(self, payload: bytes, headers: dict[str, str]) -> WebhookResult:
		# Mock webhook handler: trust everything, no signature verification.
		import json

		try:
			body = json.loads(payload or b"{}")
		except (ValueError, TypeError):
			return WebhookResult(
				event_id="invalid",
				event_type="invalid",
				signature_valid=False,
				error_code="invalid_payload",
				error_message="Body is not valid JSON",
			)
		return WebhookResult(
			event_id=body.get("event_id", uuid.uuid4().hex),
			event_type=body.get("event_type", "mock.event"),
			signature_valid=True,
			intent_name=body.get("intent_name"),
			target_status=body.get("target_status"),
			payload_excerpt=str(body)[:140],
		)
