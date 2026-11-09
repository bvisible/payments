# //// Neoffice — added file (no upstream equivalent). The payment records of the apps that
# //// `payments` replaced (the Stripe terminal of `neopay_integration`, the till TWINT of the
# //// first TWINT integration) stayed in their own tables, without a DocType, where nothing could
# //// read them: no refund, no reconciliation, no customer history. They are copied here into
# //// `Payment Intent`, the fact table of every payment, and nothing is ever deleted from the old
# //// tables (neoffice-maintenance#2390, 09.11.2026: the data of a retired app stays).
# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""Copy the payment records of retired apps into ``Payment Intent``.

Sources (each read only when its table is there):

* ``tabStripe Transaction`` (``neopay_integration``, card present at the till): one intent per row,
  provider = the Stripe provider of the site (a disabled ``stripe_legacy`` is created where there is
  none, so the history reads and a refund waits for credentials), channel ``terminal``, the reader as
  ``device`` when ``payments`` knows it, the ``pi_…`` as ``provider_intent_id``, and the till ticket
  as reference: the POS Invoice whose ``stripe_transaction_id`` names the row;
* ``tabTWINT Payment Request`` (the first till TWINT integration): one intent per row, provider = the
  TWINT provider of the site (``twint_legacy``, disabled, where there is none), channel ``qr_bridge``,
  reference = ``payment_against_type`` / ``payment_against``. Its settlement lines,
  ``tabTWINT Transaction`` (same merchant reference; a ``REVERSAL`` carries it with a suffix), are kept
  in the intent's metadata, and a reversal makes the intent ``refunded``;
* ``tabTwint Transaction`` (``twint_integration``, the TWINT QR of the shop): one intent per row,
  channel ``twint_web``, ``order_uuid`` as ``provider_intent_id``, ``reference_doctype`` /
  ``reference_docname`` as reference. Empty on every server measured on 09.11.2026 (one shop lost it
  to the drop of v15_04 on 06.07); here for a site restored from an older image.

Every copied intent says where it comes from (``metadata.legacy``, ``metadata.legacy_key``), carries
the customer and the consolidated invoice of its ticket, and is stamped ``reconciled_at``: the till
booked it at the time, so the reconciliation of ``payments`` must never book it again. Intents are
written with ``db_insert`` (no doc_event: a copy is not a payment), one ``Payment Event`` each
(``system``), in a savepoint per row. A row already copied (same ``legacy_key``) or whose
``provider_intent_id`` an intent already has is left alone: running it again copies nothing twice.

An unfinished legacy attempt (``Draft``, ``Processing``, ``Authorized``, a TWINT pairing still in
progress) becomes ``failed`` with ``error_code = legacy_unfinished``, never ``processing``: the pollers
of ``payments`` would otherwise ask the provider about it, and the reconciliation book what it answers.

    bench --site <site> execute payments.utils.legacy_payment_records.copy_legacy_payment_records \\
        --kwargs "{'dry_run': 1}"          # says what it would copy, writes nothing
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

import frappe
from frappe.model.naming import set_new_name
from frappe.utils import get_datetime, now_datetime
from frappe.utils.data import convert_utc_to_system_timezone

STRIPE = "Stripe Transaction"
TWINT_REQUEST = "TWINT Payment Request"
TWINT_SETTLEMENT = "TWINT Transaction"
TWINT_WEB = "Twint Transaction"
MIGRATED_BY = "payments.utils.legacy_payment_records (neoffice-maintenance#2390)"
REPORT_MARKER = "LEGACY_PAYMENTS_REPORT:"

STRIPE_DRIVER = "payments.drivers.stripe.terminal_driver.StripeTerminalDriver"
TWINT_DRIVER = "payments.drivers.twint.php_bridge_driver.TwintPHPBridgeDriver"

UNFINISHED = "legacy_unfinished"
ABANDONED = "legacy_abandoned"


# ------------------------------------------------------------------------------------------------
# Pure readings of a legacy row (no database)
# ------------------------------------------------------------------------------------------------


def to_minor(value) -> int:
	"""Rappen from an amount in francs: ``12.5`` -> ``1250``; a text such as ``"CHF 125.000000000"``
	(TWINT settlement lines) is read too; nothing readable is 0."""
	if value is None:
		return 0
	text = str(value).strip()
	for prefix in ("CHF", "EUR", "USD"):
		if text.upper().startswith(prefix):
			text = text[len(prefix) :].strip()
	try:
		return int((Decimal(text) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
	except (InvalidOperation, ValueError):
		return 0


def from_epoch(value) -> datetime | None:
	"""The system time of a Unix timestamp the TWINT integration stored as text, or None."""
	try:
		seconds = int(str(value).strip())
	except (TypeError, ValueError):
		return None
	if seconds <= 0:
		return None
	return convert_utc_to_system_timezone(datetime.fromtimestamp(seconds, tz=timezone.utc)).replace(
		tzinfo=None
	)


def stripe_status(row: dict) -> tuple[str, str | None]:
	"""(Payment Intent status, error_code) of a ``Stripe Transaction`` row."""
	legacy = (row.get("status") or "").strip()
	refunded = to_minor(row.get("stripe_amount_refunded")) or to_minor(row.get("refund_amount"))
	if legacy == "Refunded" or (legacy in ("Captured", "Succeeded") and refunded):
		return "refunded", None
	if legacy in ("Captured", "Succeeded"):
		return "succeeded", None
	if legacy in ("Cancelled", "Canceled"):
		return "canceled", None
	if legacy == "Failed":
		return "failed", row.get("error_code") or None
	# Draft, Processing, Authorized: the till never saw it end.
	return "failed", UNFINISHED


def twint_status(row: dict, reversed_: bool) -> tuple[str, str | None]:
	"""(Payment Intent status, error_code) of a ``TWINT Payment Request`` row."""
	paid = (row.get("payment_status") or "") == "SUCCESS" or (
		row.get("status_reason") or ""
	) == "ORDER_OK"
	if paid:
		return ("refunded" if reversed_ or to_minor(row.get("refund_amount")) else "succeeded"), None
	started = any(
		(row.get(field) or "")
		for field in ("status", "payment_status", "status_reason", "pairing_status", "payment_initiated")
	)
	return ("failed", UNFINISHED) if started else ("canceled", ABANDONED)


def settlement_lines(
	request_reference: str | None, lines: Iterable[dict]
) -> tuple[list[dict], bool]:
	"""The settlement lines of a TWINT request (same merchant reference; a reversal carries it with a
	``_<suffix>``), and whether one of them is a reversal."""
	if not request_reference:
		return [], False
	mine = [
		line
		for line in lines
		if line.get("reference") == request_reference
		or (line.get("reference") or "").startswith(request_reference + "_")
	]
	return mine, any((line.get("operation") or "").upper() == "REVERSAL" for line in mine)


def twint_web_status(row: dict) -> tuple[str, str | None]:
	"""(Payment Intent status, error_code) of a ``Twint Transaction`` row of the shop."""
	legacy = (row.get("status") or "").strip().upper()
	if legacy == "SUCCESS":
		return "succeeded", None
	if legacy == "FAILURE":
		return "failed", None
	return "failed", UNFINISHED  # PENDING, IN_PROGRESS: the shop never saw it end


def legacy_key(doctype: str, name: str) -> str:
	return f"{doctype}/{name}"


def _text(value) -> str | None:
	return None if value is None else str(value)


def stripe_intent(row: dict, ticket: dict | None, device: str | None) -> dict | None:
	"""The Payment Intent fields of a ``Stripe Transaction`` row (``metadata`` as a dict), or None when it
	holds no amount (nothing to copy)."""
	amount = to_minor(row.get("amount"))
	if amount <= 0:
		return None
	status, error_code = stripe_status(row)
	created = get_datetime(row.get("stripe_created") or row.get("creation"))
	completed = get_datetime(row.get("stripe_captured") or row.get("modified"))
	return {
		"status": status,
		"amount": amount,
		"currency": (row.get("currency") or "CHF").strip().upper(),
		"provider_intent_id": row.get("payment_intent_id") or None,
		"reference_doctype": "POS Invoice" if ticket else None,
		"reference_name": ticket["name"] if ticket else None,
		"device": device,
		"error_code": error_code,
		"error_message": (row.get("error_message") or None) if status == "failed" else None,
		"created_at": created,
		"completed_at": completed,
		"creation": get_datetime(row.get("creation")),
		"metadata": _metadata(
			STRIPE,
			row,
			ticket,
			{
				"app": "neopay_integration",
				"status": row.get("status"),
				"terminal": row.get("terminal"),
				"stripe_settings": row.get("stripe_settings"),
				"payment_method": row.get("payment_method") or row.get("stripe_payment_method"),
				"stripe_fee": _text(row.get("stripe_fee")),
				"net_amount": _text(row.get("net_amount")),
				"receipt_url": row.get("stripe_receipt_url"),
				"authorization_code": row.get("stripe_authorization_code"),
				"amount_refunded": _text(row.get("stripe_amount_refunded")),
				"refund_id": row.get("refund_id"),
				"refund_status": row.get("refund_status") or row.get("stripe_refund_status"),
			},
			completed,
		),
	}


def twint_intent(
	row: dict,
	lines: list[dict],
	reversed_: bool,
	reference: tuple | None,
	consolidated: str | None = None,
) -> dict | None:
	"""The Payment Intent fields of a ``TWINT Payment Request`` row, or None when it holds no amount."""
	amount = to_minor(row.get("amount"))
	if amount <= 0:
		return None
	status, error_code = twint_status(row, reversed_)
	created = from_epoch(row.get("payment_initiated")) or get_datetime(row.get("creation"))
	completed = from_epoch(row.get("payment_successful")) or get_datetime(row.get("modified"))
	ticket = (
		{"name": reference[1], "customer": row.get("customer"), "consolidated_invoice": consolidated}
		if reference
		else None
	)
	return {
		"status": status,
		"amount": amount,
		"currency": "CHF",
		"provider_intent_id": row.get("order_uuid") or None,
		"reference_doctype": reference[0] if reference else None,
		"reference_name": reference[1] if reference else None,
		"device": None,
		"error_code": error_code,
		"error_message": (row.get("status_reason") or None) if status == "failed" else None,
		"created_at": created,
		"completed_at": completed,
		"creation": get_datetime(row.get("creation")),
		"metadata": _metadata(
			TWINT_REQUEST,
			row,
			ticket,
			{
				"app": "the first till TWINT integration",
				"status": row.get("status"),
				"payment_status": row.get("payment_status"),
				"status_reason": row.get("status_reason"),
				"pairing_status": row.get("pairing_status"),
				"merchant_reference": row.get("reference"),
				"pos_profile": row.get("pos_profile"),
				"refund_amount": _text(row.get("refund_amount")),
				"refund_against": row.get("refund_against"),
				"settlement": [
					{
						"name": line.get("name"),
						"amount": _text(line.get("amount")),
						"fee": _text(line.get("fee")),
						"operation": line.get("operation"),
						"at": _text(line.get("creation")),
					}
					for line in lines
				],
			},
			completed,
		),
	}


def twint_web_intent(row: dict, customer: str | None = None) -> dict | None:
	"""The Payment Intent fields of a ``Twint Transaction`` row of the shop, or None without an amount."""
	amount = to_minor(row.get("amount"))
	if amount <= 0:
		return None
	status, error_code = twint_web_status(row)
	doctype, name = row.get("reference_doctype"), row.get("reference_docname")
	ticket = {"name": name, "customer": customer} if doctype and name else None
	return {
		"status": status,
		"amount": amount,
		"currency": (row.get("currency") or "CHF").strip().upper(),
		"provider_intent_id": row.get("order_uuid") or None,
		"reference_doctype": doctype if ticket else None,
		"reference_name": name if ticket else None,
		"device": None,
		"error_code": error_code,
		"error_message": None,
		"created_at": get_datetime(row.get("created_at") or row.get("creation")),
		"completed_at": get_datetime(row.get("modified")),
		"creation": get_datetime(row.get("creation")),
		"metadata": _metadata(
			TWINT_WEB,
			row,
			ticket,
			{
				"app": "twint_integration",
				"status": row.get("status"),
				"title": row.get("title"),
				"payer_name": row.get("payer_name"),
				"payer_email": row.get("payer_email"),
			},
			get_datetime(row.get("modified")),
		),
	}


def _metadata(doctype: str, row: dict, ticket: dict | None, legacy: dict, completed) -> dict:
	legacy = {key: value for key, value in legacy.items() if value not in (None, "", [])}
	legacy.update(
		{"doctype": doctype, "name": row.get("name"), "created": _text(row.get("creation"))}
	)
	metadata = {
		"legacy_key": legacy_key(doctype, row.get("name")),
		"legacy": legacy,
		"migrated_by": MIGRATED_BY,
		# The till booked these at the time (or never did): the reconciliation of payments must not.
		"reconciled_at": _text(completed or row.get("modified") or row.get("creation")),
		"reconciliation": "booked by the till at the time" if ticket else "never booked (no ticket)",
	}
	if ticket:
		if ticket.get("customer"):
			metadata["customer"] = ticket["customer"]
		if ticket.get("consolidated_invoice"):
			metadata["consolidated_invoice"] = ticket["consolidated_invoice"]
	return metadata


# ------------------------------------------------------------------------------------------------
# The database
# ------------------------------------------------------------------------------------------------


def table_exists(doctype: str) -> bool:
	return bool(
		frappe.db.sql(
			"select 1 from information_schema.tables where table_schema = database() "
			"and table_name = binary %s",
			("tab" + doctype,),
		)
	)


def has_column(doctype: str, column: str) -> bool:
	return bool(
		frappe.db.sql(
			"select 1 from information_schema.columns where table_schema = database() "
			"and table_name = binary %s and column_name = %s",
			("tab" + doctype, column),
		)
	)


def _rows(doctype: str) -> list[dict]:
	return frappe.db.sql(f"select * from `tab{doctype}` order by creation, name", as_dict=True)


def _known() -> tuple[set, set]:
	"""(legacy keys already copied, provider_intent_ids payments already has)."""
	keys, provider_ids = set(), set()
	for metadata, provider_id in frappe.db.sql(
		"select metadata_json, provider_intent_id from `tabPayment Intent`"
	):
		if provider_id:
			provider_ids.add(provider_id)
		if metadata and '"legacy_key"' in metadata:
			try:
				keys.add(json.loads(metadata).get("legacy_key"))
			except ValueError:
				pass
	return keys, provider_ids


def _provider(kind: str, dry_run: bool) -> str | None:
	"""The provider a legacy record of this kind is copied under: the site's own (enabled first), else a
	disabled ``<kind>_legacy`` created for the history."""
	driver = STRIPE_DRIVER if kind == "stripe" else TWINT_DRIVER
	rows = frappe.get_all(
		"Payment Provider",
		filters={"driver_class": ["like", f"payments.drivers.{kind}.%"]},
		fields=["name", "enabled", "driver_class"],
		order_by="enabled desc, modified desc",
	)
	rows.sort(key=lambda row: (not row.enabled, row.driver_class != driver))
	if rows:
		return rows[0].name
	name = f"{kind}_legacy"
	if dry_run:
		return name
	frappe.get_doc(
		{
			"doctype": "Payment Provider",
			"provider_name": name,
			"display_label": "Stripe" if kind == "stripe" else "TWINT",
			"driver_class": driver,
			"enabled": 0,
			"mode": "live",
			"description": "History of a retired integration (neoffice-maintenance#2390). Give it its "
			"credentials and enable it to refund one of these payments.",
		}
	).insert(ignore_permissions=True)
	return name


def _channel_settings(provider: str, channel: str) -> str | None:
	return frappe.db.get_value(
		"Provider Channel Settings", {"provider": provider, "channel": channel}, "name"
	)


def _stripe_tickets() -> dict:
	"""{Stripe Transaction name: its till ticket} from ``POS Invoice.stripe_transaction_id``."""
	if not table_exists("POS Invoice") or not has_column("POS Invoice", "stripe_transaction_id"):
		return {}
	tickets = {}
	for row in frappe.db.sql(
		"select name, stripe_transaction_id, customer, consolidated_invoice, docstatus "
		"from `tabPOS Invoice` where ifnull(stripe_transaction_id, '') != '' and docstatus < 2 "
		"order by docstatus desc, creation",
		as_dict=True,
	):
		tickets.setdefault(row.stripe_transaction_id, row)
	return tickets


def _stripe_devices() -> dict:
	"""{legacy Stripe Terminal name: Payment Device} through the reader id both carry."""
	if not table_exists("Stripe Terminal") or not has_column("Stripe Terminal", "stripe_terminal_id"):
		return {}
	devices = {
		row.provider_device_id: row.name
		for row in frappe.get_all("Payment Device", fields=["name", "provider_device_id"])
		if row.provider_device_id
	}
	return {
		name: devices[reader]
		for name, reader in frappe.db.sql("select name, stripe_terminal_id from `tabStripe Terminal`")
		if reader in devices
	}


def _twint_reference(row: dict, by_payment_row: dict) -> tuple | None:
	doctype, name = row.get("payment_against_type"), row.get("payment_against")
	if doctype and name and table_exists(doctype) and frappe.db.exists(doctype, name):
		return doctype, name
	return by_payment_row.get(row.get("name"))


def _twint_payment_rows() -> dict:
	"""{TWINT Payment Request name: (parenttype, parent)} from ``Sales Invoice Payment.twint_payment_request``."""
	if not has_column("Sales Invoice Payment", "twint_payment_request"):
		return {}
	return {
		request: (parenttype, parent)
		for request, parenttype, parent in frappe.db.sql(
			"select twint_payment_request, parenttype, parent from `tabSales Invoice Payment` "
			"where ifnull(twint_payment_request, '') != '' order by creation"
		)
	}


def _insert(values: dict, provider: str, channel: str, settings: str | None) -> str:
	metadata = values.pop("metadata")
	creation = values.pop("creation")
	doc = frappe.new_doc("Payment Intent")
	doc.update(values)
	doc.update(
		{
			"provider": provider,
			"channel": channel,
			"provider_channel_settings": settings,
			"next_action_type": "none",
			"metadata_json": json.dumps(metadata, sort_keys=True, default=str),
			"owner": "Administrator",
			"modified_by": "Administrator",
			"creation": creation,
			"modified": now_datetime(),
		}
	)
	doc.validate()  # amount, currency, JSON: the same rules as any intent
	set_new_name(doc)
	doc.db_insert()  # no doc_event: a copy is not a payment, nothing is booked
	event = frappe.new_doc("Payment Event")
	event.update(
		{
			"intent": doc.name,
			"from_status": None,
			"to_status": doc.status,
			"event_source": "system",
			"payload_excerpt": f"copied from {metadata['legacy_key']} (#2390)"[:140],
			"owner": "Administrator",
			"modified_by": "Administrator",
			"creation": now_datetime(),
			"modified": now_datetime(),
		}
	)
	set_new_name(event)
	event.db_insert()
	return doc.name


def _copy(source: str, items: list, provider: str, channel: str, settings, known, dry_run, report):
	keys, provider_ids = known
	part = report["sources"].setdefault(
		source, {"rows": 0, "copied": 0, "already": 0, "skipped": {}, "errors": 0, "copied_rappen": {}}
	)
	for row, values in items:
		part["rows"] += 1
		if values is None:
			part["skipped"]["no_amount"] = part["skipped"].get("no_amount", 0) + 1
			continue
		key = values["metadata"]["legacy_key"]
		if key in keys or (
			values.get("provider_intent_id") and values["provider_intent_id"] in provider_ids
		):
			part["already"] += 1
			continue
		if dry_run:
			name = "(dry run)"
		else:
			frappe.db.savepoint("legacy_payment_row")
			try:
				name = _insert(dict(values), provider, channel, settings)
			except Exception as exc:  # noqa: BLE001 — one row never stops the others; it stays in its table
				frappe.db.rollback(save_point="legacy_payment_row")
				part["errors"] += 1
				frappe.log_error(
					"Legacy payment record not copied",
					f"{key}: {exc!r}\n\n{frappe.get_traceback()}",
				)
				continue
		keys.add(key)
		if values.get("provider_intent_id"):
			provider_ids.add(values["provider_intent_id"])
		part["copied"] += 1
		status = values["status"]
		part["copied_rappen"][status] = part["copied_rappen"].get(status, 0) + values["amount"]
		if len(part.setdefault("sample", [])) < 3:
			part["sample"].append(
				{"intent": name, "legacy": key, "status": status, "reference": values.get("reference_name")}
			)


def copy_legacy_payment_records(dry_run: int = 0) -> dict:
	"""Copy every legacy payment record of the site into Payment Intent; returns (and prints) the report."""
	dry_run = bool(int(dry_run or 0))
	report = {"dry_run": dry_run, "sources": {}, "kept_unmatched_settlement": []}
	if not table_exists("Payment Intent"):
		report["skipped"] = "payments has no Payment Intent here yet"
		return _say(report)
	from payments.setup.payment_channels import provision_payment_channels

	known = _known()

	if table_exists(STRIPE):
		rows = _rows(STRIPE)
		if rows:
			if not dry_run:
				provision_payment_channels(codes=("terminal",))
			provider = _provider("stripe", dry_run)
			tickets, devices = _stripe_tickets(), _stripe_devices()
			items = [
				(row, stripe_intent(row, tickets.get(row.name), devices.get(row.get("terminal"))))
				for row in rows
			]
			_copy(
				STRIPE,
				items,
				provider,
				"terminal",
				_channel_settings(provider, "terminal"),
				known,
				dry_run,
				report,
			)
			report["sources"][STRIPE]["provider"] = provider

	if table_exists(TWINT_REQUEST):
		rows = _rows(TWINT_REQUEST)
		if rows:
			if not dry_run:
				provision_payment_channels(codes=("qr_bridge",))
			provider = _provider("twint", dry_run)
			lines = _rows(TWINT_SETTLEMENT) if table_exists(TWINT_SETTLEMENT) else []
			by_payment_row = _twint_payment_rows()
			used, items = set(), []
			for row in rows:
				mine, reversed_ = settlement_lines(row.get("reference"), lines)
				used.update(line.name for line in mine)
				reference = _twint_reference(row, by_payment_row)
				consolidated = None
				if reference and reference[0] == "POS Invoice":
					consolidated = frappe.db.get_value("POS Invoice", reference[1], "consolidated_invoice")
				items.append((row, twint_intent(row, mine, reversed_, reference, consolidated)))
			_copy(
				TWINT_REQUEST,
				items,
				provider,
				"qr_bridge",
				_channel_settings(provider, "qr_bridge"),
				known,
				dry_run,
				report,
			)
			report["sources"][TWINT_REQUEST]["provider"] = provider
			report["kept_unmatched_settlement"] = [line.name for line in lines if line.name not in used]

	if table_exists(TWINT_WEB):
		rows = _rows(TWINT_WEB)
		if rows:
			if not dry_run:
				provision_payment_channels(codes=("twint_web",))
			provider = _provider("twint", dry_run)
			items = []
			for row in rows:
				customer = None
				doctype, name = row.get("reference_doctype"), row.get("reference_docname")
				if doctype and name and table_exists(doctype) and has_column(doctype, "customer"):
					customer = frappe.db.get_value(doctype, name, "customer")
				items.append((row, twint_web_intent(row, customer)))
			_copy(
				TWINT_WEB,
				items,
				provider,
				"twint_web",
				_channel_settings(provider, "twint_web"),
				known,
				dry_run,
				report,
			)
			report["sources"][TWINT_WEB]["provider"] = provider

	if not dry_run:
		frappe.db.commit()
	return _say(report)


def _say(report: dict) -> dict:
	print(REPORT_MARKER + json.dumps(report, sort_keys=True, default=str))
	return report
