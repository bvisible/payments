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
	# --- ECR / terminal `payment_status`, confirmed by Payrexx on 2026-08-18 -------
	# A different vocabulary from the transaction statuses above, and in upper case on
	# the wire — map_status folds the case. Two of the nine deliberately have no entry
	# and live in NEEDS_HUMAN instead.
	"in_progress": "processing",
	"success": "succeeded",
	"terminated": "canceled",
	"reverted": "refunded",
	# `declined`, `expired` and `failed` already appear above with the same meaning.
	"failed": "failed",
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
#: ``underpaid`` and ``unknown`` join them from the ECR vocabulary. Underpaid means
#: money did arrive but not enough — neither a success nor a failure, and settling it
#: is a commercial decision, not a state transition. ``unknown`` is what it says: the
#: terminal could not tell us, so inventing an outcome would be the one thing worse
#: than stalling.
NEEDS_HUMAN: frozenset[str] = frozenset(
	{"chargeback", "disputed", "insecure", "uncaptured", "underpaid", "unknown"}
)


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
	# Folded to lower case because the two vocabularies disagree on it: transaction
	# statuses arrive lower case ("confirmed"), ECR payment statuses upper case
	# ("SUCCESS"). Without this, every terminal payment would look unmappable.
	return _STATUS_TO_FSM.get(str(payrexx_status).strip().lower())


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


def resolve_provider_name(channel: str | None = None) -> str | None:
	"""Pick which Payrexx ``Payment Provider`` to act as, deterministically.

	Detected by driver class rather than record name, so ``payrexx_test`` and
	``payrexx_live`` can cohabit on one site.

	The order matters, and the previous ``order_by="modified desc"`` was a real
	hazard: it made the answer depend on which record was touched last, so merely
	opening and saving the test provider would silently route live customer payments
	to the test account — and running the base smoke, which provisions its own
	``payrexx_smoke`` provider, did exactly that on osiris.

	The rules, in order:

	1. Only providers that are enabled.
	2. When a channel is named, only those with an enabled binding for it — a
	   provider wired for the terminal alone cannot serve the web checkout.
	3. ``live`` beats ``test``. If both exist, the site takes real money; picking the
	   test account would accept a payment that never settles.
	4. Then by name, so the answer is stable and reproducible rather than incidental.

	Ambiguity past that is logged: with two live providers on one channel, whichever
	is chosen is a guess, and someone should say which one is meant.
	"""
	candidates = frappe.get_all(
		"Payment Provider",
		filters={"driver_class": ["like", "payments.drivers.payrexx.%"], "enabled": 1},
		fields=["name", "mode"],
		order_by="name asc",
	)
	if not candidates:
		return None

	if channel:
		bound = {
			row.provider
			for row in frappe.get_all(
				"Provider Channel Settings",
				filters={
					"provider": ["in", [c.name for c in candidates]],
					"channel": channel,
					"enabled": 1,
				},
				fields=["provider"],
			)
		}
		# Only narrow if it leaves something: a site with no binding rows at all
		# should still resolve, rather than break every payment over configuration
		# that used to be optional.
		if bound:
			candidates = [c for c in candidates if c.name in bound]

	live = [c for c in candidates if (c.mode or "").lower() == "live"]
	pool = live or candidates

	if len(pool) > 1:
		frappe.log_error(
			"Payrexx provider is ambiguous",
			f"channel={channel or '-'} candidates={[c.name for c in pool]} — using "
			f"{pool[0].name}. Disable the ones this site must not use, or pass the "
			f"provider explicitly; leaving several enabled makes the choice a guess.",
		)

	return pool[0].name
