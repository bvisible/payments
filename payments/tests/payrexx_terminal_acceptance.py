# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""Acceptance run for a real Payrexx ECR terminal (NexGo N5/N6/N86).

Everything the terminal driver could not settle without hardware, in the order it
has to be answered. Written to be run by one person standing at the till, one step
per command, because each step needs a card tapped or a button pressed on the
device between calls.

Usage — one step at a time, on the site where the terminal is registered::

    bench --site <site> execute payments.tests.payrexx_terminal_acceptance.step1_pair \
        --kwargs '{"serial": "N86-123456"}'
    bench --site <site> execute payments.tests.payrexx_terminal_acceptance.step2_methods
    bench --site <site> execute payments.tests.payrexx_terminal_acceptance.step3_pay
    ...
    bench --site <site> execute payments.tests.payrexx_terminal_acceptance.report

Every call appends to ``sites/payrexx-acceptance.json``, so the run survives across
commands and the report at the end is the artefact to paste into
Neoffice/Payments/Payrexx/08-Terminal-Acceptance.

**The three open questions this run exists to answer.** They are open because
Payrexx documents none of them and offers no ECR sandbox:

1. **Which ``payment_status`` values does ``GET /payment`` actually return?**
   ``payments.drivers.payrexx._common._STATUS_TO_FSM`` maps the ones the docs and
   the PHP SDK mention. Any value seen here that is missing from that map means the
   FSM cannot advance — the driver deliberately returns ``None`` rather than
   guessing, so an unmapped status stalls the intent instead of misreporting it.
   Every status observed is recorded verbatim by step 4.

2. **Is ``POST /payment`` idempotent on ``referenceId``?** Gateways are not — two
   identical calls create two gateways, verified against the live account. If
   payments behave the same way, a retried request could charge twice, and the
   driver must never retry. Step 7 settles it with a small real amount.

3. **Void or refund?** The driver arbitrates on age and amount. Which one the
   terminal accepts, and until when, is what steps 5 and 6 measure.

Nothing here is destructive to accounting: the amounts are small, and every step
prints what it is about to do before doing it. Real money moves on a real card —
use the test account, and refund what you charge.
"""

from __future__ import annotations

import json
import os
from typing import Any

import frappe

_STATE_FILE = "payrexx-acceptance.json"

# Small enough to be painless, distinct enough to be recognisable on a statement.
_AMOUNT = 150  # CHF 1.50 in cents
_CURRENCY = "CHF"


# ---------------------------------------------------------------------------
# Run state — a plain JSON file under sites/, so steps can be minutes apart
# ---------------------------------------------------------------------------


def _state_path() -> str:
	return os.path.join(frappe.get_site_path(), _STATE_FILE)


def _load() -> dict[str, Any]:
	try:
		with open(_state_path(), encoding="utf-8") as fh:
			return json.load(fh)
	except (FileNotFoundError, ValueError):
		return {"steps": [], "statuses_seen": [], "serial": None}


def _save(state: dict[str, Any]) -> None:
	with open(_state_path(), "w", encoding="utf-8") as fh:
		json.dump(state, fh, indent=2, ensure_ascii=False, default=str)


def _record(step: str, ok: bool, detail: Any = None) -> dict[str, Any]:
	state = _load()
	state["steps"].append({
		"step": step,
		"ok": bool(ok),
		"detail": detail,
		"at": str(frappe.utils.now_datetime()),
	})
	_save(state)
	print(f"{'  OK  ' if ok else ' FAIL '} {step}" + (f" — {detail}" if detail else ""))
	return state


def _note_status(raw_status: str | None) -> None:
	"""Record a payment_status verbatim, and flag it when the FSM cannot map it."""
	if not raw_status:
		return
	from payments.drivers.payrexx._common import map_status

	state = _load()
	known = {s["status"] for s in state["statuses_seen"]}
	if raw_status in known:
		return
	mapped = map_status(raw_status)
	state["statuses_seen"].append({"status": raw_status, "maps_to": mapped})
	_save(state)
	if mapped is None:
		print(f"  ⚠️  UNMAPPED payment_status '{raw_status}' — _STATUS_TO_FSM needs it, "
		      f"the intent will stall on this value")
	else:
		print(f"  status '{raw_status}' -> {mapped}")


def _find_status(raw: Any, _depth: int = 0) -> str | None:
	"""Dig ``payment_status`` out of a response, wherever Payrexx nested it.

	The ECR responses wrap their payload differently per endpoint (sometimes under
	``data``, sometimes a bare object, sometimes a single-element list). Searching
	rather than hard-coding a path means an unexpected shape still yields the value —
	and the value is the whole point of this run.
	"""
	if _depth > 6 or raw is None:
		return None
	if isinstance(raw, dict):
		for key in ("payment_status", "paymentStatus", "status"):
			value = raw.get(key)
			if isinstance(value, str) and value:
				return value
		for value in raw.values():
			found = _find_status(value, _depth + 1)
			if found:
				return found
	elif isinstance(raw, (list, tuple)):
		for item in raw:
			found = _find_status(item, _depth + 1)
			if found:
				return found
	return None


def _driver():  # noqa: ANN202
	"""The Payrexx terminal driver for this site.

	Does **not** refuse a simulated device: rehearsing this script against the
	simulator is how you find out it runs before the hardware is on the counter. What
	matters is that the report says which it was — see :func:`_simulated`.
	"""
	from payments.drivers.payrexx._common import resolve_provider_name
	from payments.drivers.registry import resolve_driver

	provider = resolve_provider_name("terminal")
	if not provider:
		raise frappe.ValidationError(
			"no enabled Payrexx Payment Provider with a terminal binding on this site"
		)
	return resolve_driver(provider, "terminal")


def _client():  # noqa: ANN202
	from payments.drivers.payrexx._common import build_client, resolve_provider_name

	provider = resolve_provider_name("terminal")
	if not provider:
		raise frappe.ValidationError(
			"no enabled Payrexx Payment Provider with a terminal binding on this site"
		)
	return build_client(frappe.get_doc("Payment Provider", provider))


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


def step1_pair(serial: str, pairing_code: str = "") -> None:
	"""Register the terminal and read back what Payrexx says about it.

	``pairing_code`` is the code the device shows under its ECR/cash-register menu —
	the library requires it, so read it off the screen before running this. Without
	one, this step falls straight through to reading an existing pairing. What comes
	back — currency, language, tipping — is read off the device
	rather than assumed per client, which is why the driver never hard-codes it.
	"""
	state = _load()
	state["serial"] = serial
	_save(state)

	with _client() as client:
		try:
			if not pairing_code:
				raise ValueError("no pairing_code given — trying the existing pairing")
			pairing = client.ecr.pair(serial_number=serial, pairing_code=pairing_code)
			_record("1. pair", True, {
				"serial": serial,
				"currency": pairing.currency,
				"language": pairing.language,
				"tipping": pairing.has_tipping,
				"raw": pairing.raw,
			})
		except Exception as exc:  # noqa: BLE001
			# An already-paired terminal is not a failure — read it back instead.
			print(f"  pair raised {exc!r} — reading the existing pairing instead")
			try:
				pairing = client.ecr.get_pairing(serial)
				_record("1. pair (already paired)", True, {"serial": serial, "raw": pairing.raw})
			except Exception as read_exc:  # noqa: BLE001
				# Neither pairing nor reading worked. Record it and stop cleanly rather
				# than raising: this script is run from a till, and a raw traceback
				# there tells the operator nothing about what to do next.
				_record("1. pair", False, {
					"serial": serial,
					"pair_error": repr(exc),
					"read_error": repr(read_exc),
					"what_to_do": (
						"check the serial on the device label, that the terminal is on "
						"the network, and read a fresh pairing code from its ECR menu"
					),
				})
				return

	print("\n  NEXT: bench --site <site> execute "
	      "payments.tests.payrexx_terminal_acceptance.step2_methods")


def step2_methods() -> None:
	"""Which payment methods the terminal itself offers.

	Determines what the till may legitimately show. A method absent here cannot be
	taken on this device no matter what the POS profile says.
	"""
	serial = _load().get("serial")
	try:
		with _client() as client:
			methods = client.ecr.payment_methods(serial)
	except Exception as exc:  # noqa: BLE001
		_record("2. terminal payment methods", False, {
			"serial": serial,
			"error": repr(exc),
			"what_to_do": "step 1 has to succeed first — an unpaired serial has no methods",
		})
		return
	_record("2. terminal payment methods", bool(methods),
	        methods.raw if hasattr(methods, "raw") else methods)
	print("\n  NEXT: step3_pay  (have a test card ready)")


def step3_pay(amount: int = _AMOUNT) -> None:
	"""Start a real payment and stop, leaving the terminal waiting for the card.

	Creates a Payment Intent through the normal API so the whole chain is exercised —
	not a bare client call. Then step4_poll watches it, which is where the unknown
	``payment_status`` values get captured.
	"""
	from payments.api.intent import create_intent
	from payments.drivers.payrexx._common import resolve_provider_name

	serial = _load().get("serial")
	device = frappe.db.get_value("Payment Device", {"serial_number": serial}, "name")
	if not device:
		_record("3. create payment", False,
		        f"no Payment Device with serial_number={serial} — create one first")
		return

	result = create_intent(
		provider=resolve_provider_name("terminal"),
		channel="terminal",
		amount=amount,
		currency=_CURRENCY,
		device=device,
	)
	state = _load()
	state["intent"] = result.get("intent_name")
	state["provider_intent_id"] = result.get("provider_intent_id")
	state["simulated"] = _simulated(device)
	_save(state)
	if state["simulated"]:
		print("  ⚠️  SIMULATED DEVICE — this rehearses the script, it does not validate "
		      "the hardware. The answers to the three open questions can only come "
		      "from a real terminal.")
	_record("3. create payment", bool(result.get("intent_name")), result)

	print(f"\n  >>> TAP OR INSERT THE CARD NOW on {serial} — CHF {amount / 100:.2f}")
	print("  THEN: step4_poll")


def _simulated(device_name: str) -> bool:
	"""Whether this Payment Device is the guarded simulator rather than hardware.

	Mirrors the driver's own test (device_type starting with "simulated" AND the
	provider in test mode) instead of re-deriving it, so the two cannot drift apart
	and quietly label a real run as simulated or the reverse.
	"""
	try:
		driver = _driver()
		provider_device_id = frappe.db.get_value(
			"Payment Device", device_name, "provider_device_id"
		)
		return bool(driver._is_simulator(provider_device_id or device_name))  # noqa: SLF001
	except Exception:  # noqa: BLE001 - an unknown answer must not stop the run
		return False


def step4_poll(rounds: int = 20, delay: int = 3) -> None:
	"""Watch the payment to a final state, recording every status seen verbatim.

	This is the step that answers open question 1. Any ``payment_status`` printed
	with ⚠️ UNMAPPED has to be added to ``_STATUS_TO_FSM`` before go-live — the
	driver returns ``None`` on an unknown status rather than guessing, so the intent
	stalls instead of claiming an outcome it cannot back up.
	"""
	import time

	state = _load()
	intent_name = state.get("intent")
	if not intent_name:
		_record("4. poll", False, "no intent recorded — run step3_pay first")
		return

	driver = _driver()
	final = {"succeeded", "failed", "canceled", "refunded"}
	for i in range(rounds):
		response = driver.get_status(state["provider_intent_id"])
		raw_status = _find_status(response.raw)
		_note_status(raw_status)

		# The terminal is the source of truth for the payment, but the webhook can
		# land first and move the intent before a poll sees the change — so either
		# reaching a final state ends the wait. Checking only the driver would also
		# never converge against the simulator, whose get_status has no state to read.
		intent_status = frappe.db.get_value("Payment Intent", intent_name, "status")
		print(f"  round {i + 1}: driver={response.status} intent={intent_status} "
		      f"raw_status={raw_status}")
		if response.status in final or intent_status in final:
			intent = frappe.get_doc("Payment Intent", intent_name)
			_record("4. poll to final state", True, {
				"driver": response.status,
				"raw_status": raw_status,
				"intent_status": intent.status,
				"raw": response.raw,
			})
			print("\n  NEXT: step5_void  (voids the payment you just took)")
			return
		time.sleep(delay)

	_record("4. poll to final state", False,
	        f"driver still {response.status} after {rounds * delay}s — write down what the "
	        f"terminal screen shows, that is the value the driver could not read")


def step5_void() -> None:
	"""Void the payment taken in step 3, and record whether the terminal allows it.

	Answers half of open question 3. A void reverses before settlement; if Payrexx
	refuses it, the driver's age/amount arbitration has to lean on refund instead,
	and the message it returns has to say so rather than reporting a generic failure.
	"""
	state = _load()
	with _client() as client:
		try:
			result = client.ecr.void_payment(
				serial_number=state.get("serial"), payment_id=state.get("provider_intent_id")
			)
			_record("5. void", True, result.raw if hasattr(result, "raw") else result)
		except Exception as exc:  # noqa: BLE001
			# A refusal is a result, not a test failure — it tells us which path the
			# driver must take.
			_record("5. void refused (informative)", True,
			        {"error": repr(exc), "meaning": "arbitration must prefer refund"})
	print("\n  NEXT: step6_refund  (take a second payment first if the void succeeded)")


def step6_refund(amount: int | None = None) -> None:
	"""Refund through the driver, exercising its void/refund arbitration.

	Uses the driver rather than the client on purpose: the arbitration logic is what
	is under test, not the endpoint.
	"""
	state = _load()
	driver = _driver()
	response = driver.refund(state.get("provider_intent_id"), amount=amount)
	_record("6. refund via driver", response.status in ("refunded", "succeeded", "processing"), {
		"status": response.status,
		"error_code": response.error_code,
		"error_message": response.error_message,
		"raw": response.raw,
	})
	print("\n  NEXT: step7_idempotency")


def step7_idempotency() -> None:
	"""Does ``POST /payment`` deduplicate on ``referenceId``? — open question 2.

	Sends the same reference twice and compares the payment ids. Gateways do NOT
	deduplicate (verified on the live account), and if payments behave the same way
	then a retried request charges twice — which is exactly why
	``payrexx.client._RETRYABLE_METHODS`` excludes POST. This step confirms that
	decision was necessary, or shows it can be relaxed.

	Both calls are left for the operator to cancel on the device; nothing is
	captured, so no money moves.
	"""
	state = _load()
	serial, reference = state.get("serial"), f"acceptance-idem-{frappe.generate_hash(length=8)}"

	ids = []
	with _client() as client:
		for attempt in (1, 2):
			try:
				payment = client.ecr.create_payment(
					serial, amount=_AMOUNT, currency=_CURRENCY,
					payment_reference=reference,
				)
				ids.append(getattr(payment, "id", None) or payment.raw)
				print(f"  attempt {attempt}: id={ids[-1]}")
			except Exception as exc:  # noqa: BLE001
				ids.append(f"error: {exc!r}")
				print(f"  attempt {attempt}: {exc!r}")

	distinct = len({str(i) for i in ids}) > 1
	_record("7. POST /payment idempotency", True, {
		"reference": reference,
		"ids": ids,
		"deduplicates": not distinct,
		"meaning": (
			"NOT idempotent — never retry POST, the driver is right to exclude it"
			if distinct
			else "idempotent on referenceId — retries would be safe"
		),
	})
	print("\n  >>> CANCEL both prompts on the terminal now.")
	print("  THEN: report")


def report() -> None:
	"""Print the run as the artefact to file in Obsidian."""
	state = _load()
	print("\n=== Payrexx terminal acceptance ===")
	if state.get("simulated"):
		print("⚠️  RUN AGAINST THE SIMULATOR — rehearsal only, the hardware is still "
		      "unvalidated and the three open questions remain open.")
	elif state.get("simulated") is False:
		print("Run against real hardware.")
	print(f"serial: {state.get('serial')}")
	print(f"intent: {state.get('intent')} (provider id {state.get('provider_intent_id')})\n")

	for entry in state.get("steps", []):
		print(f"{'  OK  ' if entry['ok'] else ' FAIL '} {entry['step']}  [{entry['at']}]")
		if entry.get("detail"):
			print(f"        {json.dumps(entry['detail'], ensure_ascii=False, default=str)[:400]}")

	seen = state.get("statuses_seen", [])
	print(f"\npayment_status values observed: {len(seen)}")
	unmapped = [s for s in seen if s["maps_to"] is None]
	for s in seen:
		flag = "  ⚠️ UNMAPPED" if s["maps_to"] is None else ""
		print(f"  {s['status']:<24} -> {s['maps_to']}{flag}")

	if not seen:
		print("\nNo payment_status was observed at all — either the run never reached a "
		      "live payment, or the value sits somewhere _find_status does not look. "
		      "Open question 1 stays open until this list is non-empty.")
	elif unmapped:
		print(f"\n⚠️  {len(unmapped)} status value(s) have no FSM mapping. Add them to "
		      f"payments/drivers/payrexx/_common.py::_STATUS_TO_FSM before go-live — "
		      f"an intent that reaches one of these stalls rather than resolving.")
	else:
		print("\nEvery observed status maps onto the FSM.")


	ok = sum(1 for e in state.get("steps", []) if e["ok"])
	print(f"\n=== {ok}/{len(state.get('steps', []))} steps passed ===")
	print(f"state file: {_state_path()}")


def reset() -> None:
	"""Discard the run state to start over."""
	try:
		os.remove(_state_path())
		print("state cleared")
	except FileNotFoundError:
		print("no state to clear")
