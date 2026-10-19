# //// Neoffice — added file (no upstream equivalent). The cases on which the TWINT engine is held to the hub's PHP
# //// bridge (maintenance#1721): every command, the SDK's recorded scenarios, TWINT's faults, answers that are
# //// broken, wrong inputs. One list for the bench (PHP and Python side by side, neoffice-devops
# //// scripts/twint_engine_bench) and for the CI, which replays what PHP answered (vectors-<version>.json).
# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""The cases of the TWINT engine's comparison with the PHP bridge."""

from __future__ import annotations

from dataclasses import dataclass, field

from payments.twint_engine.testing.fake_twint import Answer, fault, system_status

STORE = "a1b2c3d4-0000-4000-8000-000000000001"
ORDER = "0e5f2a90-1111-4222-8333-444455556666"

# How a case is compared. "exact": same answer, same requests. "failure": both refuse (the message is the TLS
# library's or ext-soap's, worded by each side), same requests. "difference": a deliberate change, named in `why`.
EXACT, FAILURE, DIFFERENCE = "exact", "failure", "difference"


@dataclass
class Case:
	label: str
	command: str
	params: dict
	config: dict = field(
		default_factory=dict
	)  # merged over the default merchant (store, password, environment)
	state: dict = field(default_factory=dict)  # WireMock scenario states
	force: list = field(default_factory=list)  # (operation, kind, args): see answer_for
	certificate: str = "merchant"  # merchant | legacy | stranger | none
	compare: str = EXACT
	why: str = ""


def stub_body(fake, version: str, name: str) -> str:
	for stub in fake.stubs[version]:
		if stub.get("name") == name:
			return stub["body"]
	raise KeyError(name)


def answer_for(fake, version: str, kind: str, args) -> Answer:
	if kind == "fault":
		element, code, status, text = args
		return Answer(fault(version, element, code, status, text), 500)
	if kind == "raw":
		status, content_type, body = args
		return Answer(body, status, content_type)
	if kind == "close":
		return Answer("", close=True)
	if kind == "system_status":
		return Answer(system_status(version, args))
	if kind == "edit":  # one of the SDK's recorded answers, changed: (stub name, [(old, new), ...])
		name, replacements = args
		body = stub_body(fake, version, name)
		for old, new in replacements:
			if old not in body:
				raise ValueError(f"{name}: {old!r} not in the recorded answer")
			body = body.replace(old, new)
		return Answer(body)
	raise ValueError(kind)


def apply(case: Case, fake, version: str) -> None:
	fake.reset()
	for scenario, state in case.state.items():
		fake.set_state(scenario, state)
	for operation, kind, args in case.force:
		fake.force(operation, answer_for(fake, version, kind, args))


START = "StartOrder"
MONITOR_OK = "MonitorOrder_SuccessScenario (OK Step)"
MONITOR_AWAIT = "MonitorOrder_SuccessScenario (Step2)"
TOKEN = "{{randomInt lower=0}}</v81:Token>"
QR = "<v81:QRCode>data:image/png;base64,"
SUCCESS_STATUS = '<v81:Status code="0">SUCCESS</v81:Status>'
OK_REASON = '<v81:Reason code="0">ORDER_OK</v81:Reason>'
CHF = "<v82:Currency>CHF</v82:Currency>"
REFERENCE = "<v81:MerchantTransactionReference>111222</v81:MerchantTransactionReference>"


def _pay(label, amount, reference="PI-2026-00001", **kwargs):
	return Case(
		label,
		"register_payment",
		{"amount": amount, "currency": "CHF", "merchant_reference": reference},
		**kwargs,
	)


def _monitor(label, **kwargs):
	return Case(label, "monitor_status", {"order_id": ORDER}, **kwargs)


INVALID_MERCHANT = ("InvalidMerchant", "1100", "INVALID_MERCHANT", "Merchant is not valid")
SYSTEM_ERROR = ("SystemError", "1000", "SYSTEM_ERROR", "System error")


def cases(version: str = "8_7") -> list[Case]:
	"""Every case. The recorded answers' prefixes (v81, v82) are the 8.7 fixtures': edits apply there only."""
	edits = version == "8_7"
	out = [
		# -- register_payment: the till's and the shop's QR ----------------------------------------------------------
		_pay("register: CHF 19.95", 1995),
		*[
			_pay(f"register: {amount} rappen", amount) for amount in (1, 10, 33, 50, 100, 1990, 123456789)
		],
		_pay("register: amount as text '12.7'", "12.7"),
		_pay("register: amount missing", None),
		_pay("register: amount 0", 0),
		_pay("register: negative amount", -5),
		_pay("register: reference with markup and accents", 100, "A&B<C>\"'é ü"),
		_pay("register: reference of 50 bytes", 100, "R" * 50),
		_pay("register: reference of 51 bytes", 100, "R" * 51),
		_pay("register: reference of 50 characters, 51 bytes", 100, "é" + "R" * 49),
		_pay("register: reference missing", 100, ""),
		_pay("register: store uuid in capitals", 100, config={"store_uuid": STORE.upper()}),
		_pay("register: store uuid not a uuid", 100, config={"store_uuid": "not-a-uuid"}),
		_pay("register: store uuid missing", 100, config={"store_uuid": ""}),
		_pay("register: test environment", 100, config={"environment": "Sandbox"}),
		_pay("register: TWINT refuses the merchant", 100, force=[(START, "fault", INVALID_MERCHANT)]),
		_pay(
			"register: TWINT refuses the cash register",
			100,
			force=[("EnrollCashRegister", "fault", INVALID_MERCHANT)],
		),
		_pay(
			"register: HTTP 503 with an HTML page",
			100,
			force=[(START, "raw", (503, "text/html", "<html><body>down</body></html>"))],
		),
		_pay(
			"register: HTTP 502 not XML",
			100,
			force=[(START, "raw", (502, "text/html", "<html><body>Bad gateway<br></body></html>"))],
		),
		_pay(
			"register: answer that is not XML", 100, force=[(START, "raw", (200, "text/plain", "hello"))]
		),
		_pay("register: empty answer", 100, force=[(START, "raw", (200, "text/xml", ""))]),
		_pay(
			"register: kept connection dropped, the call goes again", 100, force=[(START, "close", None)]
		),
		_pay(
			"register: cash register named",
			100,
			config={"cash_register_id": "till-1"},
			compare=DIFFERENCE,
			why="the bridge crashes on any cash register id (it instantiates an interface); the engine sends it",
		),
		# -- monitor_status: the SDK's own scenarios -----------------------------------------------------------------
		_monitor("monitor: order received", state={"SuccessScenario": "SetupSuccess"}),
		_monitor("monitor: awaiting the customer", state={"SuccessScenario": "AwaitConfirmation"}),
		_monitor("monitor: paid", state={"SuccessScenario": "ConfirmationOK"}),
		_monitor("monitor: failure scenario, received", state={"FailureScenario": "SetupFailure"}),
		_monitor("monitor: failure scenario, awaiting", state={"FailureScenario": "AwaitConfirmation"}),
		_monitor("monitor: customer aborted", state={"FailureScenario": "SetupFailureClientAborted"}),
		_monitor("monitor: general error", state={"FailureScenario": "SetupFailureGeneralError"}),
		_monitor("monitor: customer timeout", state={"FailureScenario": "SetupFailureClientTimeout"}),
		_monitor("monitor: no recorded answer (HTTP 404)"),
		_monitor(
			"monitor: order unknown to TWINT",
			force=[("MonitorOrder", "fault", ("InvalidOrder", "1700", "INVALID_ORDER", "Order not found"))],
		),
		_monitor("monitor: system error", force=[("MonitorOrder", "fault", SYSTEM_ERROR)]),
		Case(
			"monitor: order id in capitals",
			"monitor_status",
			{"order_id": ORDER.upper()},
			state={"SuccessScenario": "ConfirmationOK"},
		),
		Case("monitor: order id not a uuid", "monitor_status", {"order_id": "PI-2026-00001"}),
		Case("monitor: order id missing", "monitor_status", {}),
		# -- confirm_payment: the capture once the customer said yes ---------------------------------------------------
		Case("confirm: capture", "confirm_payment", {"order_id": ORDER, "amount": 1995}),
		Case("confirm: amount 0", "confirm_payment", {"order_id": ORDER, "amount": 0}),
		Case("confirm: order id missing", "confirm_payment", {"amount": 1995}),
		Case(
			"confirm: refused",
			"confirm_payment",
			{"order_id": ORDER, "amount": 1995},
			force=[
				(
					"ConfirmOrder",
					"fault",
					("StatusTransitionError", "1200", "STATUS_TRANSITION_ERROR", "Status transition not allowed"),
				)
			],
		),
		Case(
			"confirm: the SDK classifies that fault for a cancel only",
			"confirm_payment",
			{"order_id": ORDER, "amount": 1995},
			force=[
				(
					"ConfirmOrder",
					"fault",
					(
						"STATUS_TRANSITION_ERROR",
						"1200",
						"STATUS_TRANSITION_ERROR",
						"Status transition not allowed",
					),
				)
			],
		),
		# -- cancel_payment ----------------------------------------------------------------------------------------------
		Case("cancel: canceled", "cancel_payment", {"order_id": ORDER}),
		Case(
			"cancel: TWINT's StatusTransitionError",
			"cancel_payment",
			{"order_id": ORDER},
			force=[
				(
					"CancelOrder",
					"fault",
					("StatusTransitionError", "1200", "STATUS_TRANSITION_ERROR", "Status transition not allowed"),
				)
			],
		),
		Case(
			"cancel: a fault the SDK classifies",
			"cancel_payment",
			{"order_id": ORDER},
			force=[
				(
					"CancelOrder",
					"fault",
					(
						"STATUS_TRANSITION_ERROR",
						"1200",
						"STATUS_TRANSITION_ERROR",
						"Status transition not allowed",
					),
				)
			],
		),
		Case("cancel: order id missing", "cancel_payment", {}),
		# -- refund_payment: the reversal --------------------------------------------------------------------------------
		Case("refund: full, own reference", "refund_payment", {"order_id": ORDER, "amount": 1995}),
		Case(
			"refund: partial, given reference",
			"refund_payment",
			{"order_id": ORDER, "amount": 500, "merchant_reference": "R-1"},
		),
		Case("refund: amount missing", "refund_payment", {"order_id": ORDER}),
		Case(
			"refund: given reference of 51 bytes",
			"refund_payment",
			{"order_id": ORDER, "amount": 500, "merchant_reference": "R" * 51},
		),
		Case("refund: order id not a uuid", "refund_payment", {"order_id": "abc", "amount": 500}),
		Case(
			"refund: refused",
			"refund_payment",
			{"order_id": ORDER, "amount": 500},
			force=[(START, "fault", ("ReversalError", "1600", "REVERSAL_ERROR", "Reversal not possible"))],
		),
		# -- certificates, health, unknown -----------------------------------------------------------------------------
		_pay(
			"certificate: wrong password",
			100,
			config={"certificate_password": "wrong"},
			compare=EXACT if edits else FAILURE,  # the SDK 2.0 refuses it earlier, in its own words
		),
		_pay("certificate: old 3DES .p12", 100, certificate="legacy"),
		_pay("certificate: one TWINT does not know", 100, certificate="stranger", compare=FAILURE),
		Case("health: certificate present", "health_check", {}),
		Case("health: certificate missing", "health_check", {}, certificate="none"),
		Case("unknown command", "refund_everything", {}),
	]
	if edits:
		out += [
			_pay(
				"register: answer without a token",
				100,
				force=[(START, "edit", ("StartOrder", [("<v81:Token>" + TOKEN, "")]))],
			),
			_pay(
				"register: token that is not a number",
				100,
				force=[(START, "edit", ("StartOrder", [(TOKEN, "abc</v81:Token>")]))],
			),
			_pay(
				"register: QR code that is not an image",
				100,
				force=[(START, "edit", ("StartOrder", [(QR, "<v81:QRCode>https://twint.ch/")]))],
			),
			_pay(
				"register: answer without a QR code",
				100,
				force=[
					(START, "edit", ("StartOrder", [(QR, "<v81:Unused>")] + [("</v81:QRCode>", "</v81:Unused>")]))
				],
			),
			_pay(
				"register: unknown order status",
				100,
				force=[
					(START, "edit", ("StartOrder", [("IN_PROGRESS</v81:Status>", "ON_HOLD</v81:Status>")]))
				],
			),
			_pay(
				"register: unknown reason",
				100,
				force=[
					(
						START,
						"edit",
						("StartOrder", [("ORDER_RECEIVED</v81:Reason>", "ORDER_PARKED</v81:Reason>")]),
					)
				],
			),
			_pay(
				"register: unknown pairing status",
				100,
				force=[(START, "edit", ("StartOrder", [("PAIRING_IN_PROGRESS<", "PAIRING_LOST<")]))],
			),
			_monitor(
				"monitor: confirmation pending (capture needed)",
				force=[
					(
						"MonitorOrder",
						"edit",
						(
							MONITOR_AWAIT,
							[
								(
									'<v81:Reason code="90">ORDER_PENDING</v81:Reason>',
									'<v81:Reason code="85">ORDER_CONFIRMATION_PENDING</v81:Reason>',
								)
							],
						),
					)
				],
			),
			_monitor(
				"monitor: partially confirmed",
				force=[
					(
						"MonitorOrder",
						"edit",
						(MONITOR_OK, [(OK_REASON, '<v81:Reason code="0">ORDER_PARTIAL_OK</v81:Reason>')]),
					)
				],
			),
			_monitor(
				"monitor: merchant aborted",
				force=[
					(
						"MonitorOrder",
						"edit",
						(
							MONITOR_OK,
							[
								(SUCCESS_STATUS, '<v81:Status code="2">FAILURE</v81:Status>'),
								(OK_REASON, '<v81:Reason code="102">MERCHANT_ABORT</v81:Reason>'),
							],
						),
					)
				],
			),
			_monitor(
				"monitor: amount in euros",
				force=[("MonitorOrder", "edit", (MONITOR_OK, [(CHF, "<v82:Currency>EUR</v82:Currency>")]))],
			),
			_monitor(
				"monitor: reference missing", force=[("MonitorOrder", "edit", (MONITOR_OK, [(REFERENCE, "")]))]
			),
			_monitor(
				"monitor: unknown reason",
				force=[
					(
						"MonitorOrder",
						"edit",
						(MONITOR_OK, [(OK_REASON, '<v81:Reason code="7">ORDER_SETTLED</v81:Reason>')]),
					)
				],
			),
			_monitor(
				"monitor: answer of another operation", force=[("MonitorOrder", "edit", ("CancelOrder", []))]
			),
		]
	return out
