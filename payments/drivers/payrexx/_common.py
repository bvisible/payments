# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""Shared plumbing for the Payrexx drivers: status mapping and client building.

The HTTP layer lives in the standalone ``payrexx`` library
(https://github.com/neoserviceai/payrexx-python), which encodes the four API
behaviours that fail silently — mandatory ``instance``, PHP indexed-bracket list
encoding, the total absence of idempotency, and the three sharp edges of webhook
signature verification. Nothing here re-implements HTTP; this module only
translates between Payrexx's vocabulary and our unified FSM.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import frappe
from frappe import _

if TYPE_CHECKING:  # pragma: no cover
	from payrexx import PayrexxClient

#: Payrexx transaction status → Payment Intent FSM status.
#:
#: Sixteen source values, because the two authorities disagree: the transaction
#: webhook reference documents thirteen (including ``chargeback``) while the
#: official PHP SDK's constants document fifteen (including ``initiated``,
#: ``insecure`` and ``uncaptured``) but omit ``chargeback``. Both are incomplete,
#: hence :func:`map_status` returning ``None`` rather than guessing.
_STATUS_TO_FSM: dict[str, str] = {
	"waiting": "requires_action",
	"initiated": "requires_action",
	"authorized": "processing",
	"reserved": "processing",
	"refund_pending": "processing",
	"confirmed": "succeeded",
	"cancelled": "canceled",
	"expired": "canceled",
	"declined": "failed",
	"error": "failed",
	"refunded": "refunded",
	"partially-refunded": "refunded",
}

#: Statuses that must NOT drive an automatic FSM transition.
#:
#: ``chargeback`` and ``disputed`` are not refunds — folding them into
#: ``refunded`` would misstate the books, and our FSM has no state for them.
#: ``insecure`` means 3-D Secure was unavailable or bypassed, so the money may
#: have moved while the liability shift did not. All three are logged as a
#: Payment Event and surfaced to an operator instead. ``uncaptured`` is terminal
#: but ambiguous: the hold lapsed without a capture, which is closer to an
#: operational miss than to a customer-facing failure.
NEEDS_HUMAN: frozenset[str] = frozenset({"chargeback", "disputed", "insecure", "uncaptured"})


def map_status(payrexx_status: str | None) -> str | None:
	"""Translate a Payrexx status into an FSM status.

	Returns ``None`` when the status must not drive a transition — either because
	it needs a human (see :data:`NEEDS_HUMAN`) or because Payrexx sent something
	neither documentation nor the PHP SDK knows about. Callers log and leave the
	Payment Intent untouched rather than guessing, because guessing wrong on a
	payment state is worse than lagging behind it.
	"""
	if not payrexx_status:
		return None
	return _STATUS_TO_FSM.get(str(payrexx_status))


def build_client(provider_doc) -> PayrexxClient:  # noqa: ANN001
	"""Build a :class:`payrexx.PayrexxClient` from a Payment Provider record.

	Credentials expected in ``credentials_json``:

	- ``instance`` — the ``<instance>`` in ``https://<instance>.payrexx.com``
	- ``api_secret`` — from *API & Plugins* in the merchant back office
	- ``pos_api_secret`` — optional, for the ``/ecr/*`` calls

	Raises:
	    frappe.ValidationError: A required credential is missing, or the
	        ``payrexx`` library is not installed on this bench.
	"""
	try:
		from payrexx import PayrexxClient
	except ImportError as exc:  # pragma: no cover - environment issue, not logic
		raise frappe.ValidationError(
			_(
				"The 'payrexx' Python library is not installed on this bench. "
				"Install it with: bench pip install "
				"git+https://github.com/neoserviceai/payrexx-python.git"
			)
		) from exc

	creds = provider_doc.get_credentials() or {}
	instance = (creds.get("instance") or "").strip()
	api_secret = (creds.get("api_secret") or "").strip()

	if not instance:
		raise frappe.ValidationError(
			_("Payrexx Payment Provider {0} is missing 'instance' in credentials_json").format(
				provider_doc.name
			)
		)
	if not api_secret:
		raise frappe.ValidationError(
			_("Payrexx Payment Provider {0} is missing 'api_secret' in credentials_json").format(
				provider_doc.name
			)
		)

	return PayrexxClient(
		instance=instance,
		api_secret=api_secret,
		pos_api_secret=(creds.get("pos_api_secret") or "").strip() or None,
		timeout=float(creds.get("timeout") or 30.0),
	)


def error_response(exc: Exception, *, provider_intent_id: str | None = None) -> Any:
	"""Turn any Payrexx exception into a ``failed`` DriverResponse.

	The driver contract forbids raising into the API layer: errors travel as
	``status="failed"`` with an ``error_code``. One case is deliberately singled
	out — a transport failure means the outcome is **unknown**, not failed, which
	matters enormously on a terminal where a blind retry can charge twice. It is
	reported with the distinct code ``transport_error`` so callers can reconcile
	instead of treating it as a clean rejection.
	"""
	from payments.drivers.base import DriverResponse

	code = type(exc).__name__
	try:
		from payrexx.errors import PayrexxTransportError

		if isinstance(exc, PayrexxTransportError):
			code = "transport_error"
	except ImportError:  # pragma: no cover
		pass

	return DriverResponse(
		status="failed",
		provider_intent_id=provider_intent_id,
		error_code=code,
		error_message=str(exc),
	)
