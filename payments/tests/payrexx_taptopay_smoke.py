# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""Smoke the Tap to Pay server lot — everything that works without a phone.

Run::

    bench --site <site> execute payments.tests.payrexx_taptopay_smoke.run

What it can prove without hardware: the channel exists, the driver resolves, an
intent is created with the handoff payload the phone needs, the new
``native_app_handoff`` value is actually accepted by the doctype (it is a Select —
writing outside its options leaves a field silently unset), cancelling an untapped
intent is allowed, and a refund with nothing to refund fails with a clear reason
rather than an exception.

What it cannot prove, and does not claim to: that a real Tap to Pay payment reaches
this channel, and that ``order_reference`` comes back as ``referenceId``. That is
``payrexx_taptopay_probe`` plus a real phone — see
Neoffice/Payments/Payrexx/03-Tap-To-Pay-Mobile §9bis.

Creates one Payment Intent and deletes it again. No money moves: this channel cannot
initiate a payment by construction.
"""

from __future__ import annotations

import frappe

CHANNEL = "payrexx_tap_to_pay"


def run() -> bool:
	results: list[tuple[str, bool, str]] = []

	def check(label: str, ok: bool, detail: str = "") -> None:
		results.append((label, bool(ok), detail))
		print(f"{'  OK  ' if ok else ' FAIL '} {label}" + (f" — {detail}" if detail else ""))

	# 1. The channel the patch creates.
	channel = frappe.db.get_value(
		"Payment Channel", CHANNEL, ["ui_kind", "display_label", "capabilities_json"], as_dict=True
	)
	check("1. channel exists", bool(channel), str(channel and channel.display_label))
	if not channel:
		print("\n  Run `bench --site <site> migrate` to apply the provisioning patch.")
		print(f"\n=== 0/{len(results)} checks passed ===")
		return False

	caps = frappe.parse_json(channel.capabilities_json or "{}")
	check(
		"2. capabilities say tip yes, device no",
		caps.get("supports_tip") is True and caps.get("requires_device") is False,
		f"tip={caps.get('supports_tip')} device={caps.get('requires_device')}",
	)

	# 3. The doctype must accept the new next_action_type. A Select silently drops a
	#    value outside its options, which is exactly how a webhook status bug hid
	#    earlier in this app — so assert on the stored value, not on the call.
	options = (frappe.get_meta("Payment Intent").get_field("next_action_type").options or "").split("\n")
	check("3. native_app_handoff is a valid next_action_type", "native_app_handoff" in options,
	      f"{len(options)} options")

	provider = frappe.db.get_value(
		"Payment Provider",
		{"driver_class": ["like", "payments.drivers.payrexx.%"], "enabled": 1},
		"name",
		order_by="name asc",
	)
	if not provider:
		check("4. a Payrexx provider is enabled", False, "none found — configure one first")
		print(f"\n=== {sum(1 for _, ok, _ in results if ok)}/{len(results)} checks passed ===")
		return False
	check("4. a Payrexx provider is enabled", True, provider)

	# 5. The binding. Created here rather than by the patch on purpose: enabling Tap
	#    to Pay for a client is a commercial act, billed separately.
	binding = frappe.db.get_value(
		"Provider Channel Settings", {"provider": provider, "channel": CHANNEL}, "name"
	)
	created_binding = False
	if not binding:
		doc = frappe.get_doc(
			{
				"doctype": "Provider Channel Settings",
				"provider": provider,
				"channel": CHANNEL,
				"enabled": 1,
				"driver_class": "payments.drivers.payrexx.tap_to_pay_driver.PayrexxTapToPayDriver",
			}
		).insert(ignore_permissions=True)
		binding, created_binding = doc.name, True
		frappe.db.commit()
	check("5. provider × channel binding", bool(binding),
	      f"{binding}{' (created by this smoke)' if created_binding else ''}")

	# 6. Driver resolution.
	from payments.drivers.registry import resolve_driver

	try:
		driver = resolve_driver(provider, CHANNEL)
		check("6. registry resolves the driver", type(driver).__name__ == "PayrexxTapToPayDriver",
		      type(driver).__name__)
	except Exception as exc:  # noqa: BLE001
		check("6. registry resolves the driver", False, repr(exc))
		print(f"\n=== {sum(1 for _, ok, _ in results if ok)}/{len(results)} checks passed ===")
		return False

	# 7-9. An intent, and the payload the phone actually needs.
	from payments.api.intent import create_intent

	intent = create_intent(provider=provider, channel=CHANNEL, amount=1500, currency="CHF")
	name = intent.get("intent_name")
	check("7. create_intent works with no provider call", bool(name),
	      f"{name} status={intent.get('status')}")
	check("8. next_action is the native handoff",
	      intent.get("next_action_type") == "native_app_handoff",
	      str(intent.get("next_action_type")))

	payload = intent.get("next_action_payload") or {}
	needed = {"handoff", "android_intent", "order_reference", "amount", "currency"}
	check("9. payload carries what the SDK's Sale needs", needed.issubset(payload.keys()),
	      f"order_reference={payload.get('order_reference')} amount={payload.get('amount')}")

	# 10. The value survived the Select — read it back from the database, not the
	#     response dict, because that is where a rejected option would show.
	stored = frappe.db.get_value(
		"Payment Intent", name, ["next_action_type", "provider_intent_id", "status"], as_dict=True
	)
	check("10. the stored next_action_type is not empty",
	      stored.next_action_type == "native_app_handoff", str(stored.next_action_type))
	check("11. provider_intent_id is empty until the webhook",
	      not stored.provider_intent_id,
	      "correct — no Payrexx transaction exists yet")

	# 12. Cancelling before any tap is legitimate: nothing was charged.
	cancelled = driver.cancel_intent(name)
	check("12. cancelling an untapped intent is allowed", cancelled.status == "canceled",
	      f"{cancelled.status} {cancelled.error_code or ''}")

	# 13. A refund with no transaction must explain itself, not raise.
	refund = driver.refund(name)
	check("13. refund with nothing to refund fails cleanly",
	      refund.status == "failed" and refund.error_code == "transaction_not_found",
	      f"{refund.error_code}: {(refund.error_message or '')[:60]}")

	# 14. An unsupported method is refused rather than silently dropped.
	from payments.drivers.base import IntentRequest

	bad = driver.create_intent(
		IntentRequest(intent_name="PI-DRY-RUN", amount=1500, currency="CHF",
		              metadata={"payment_method": "BITCOIN"})
	)
	check("14. an unknown payment method is refused",
	      bad.status == "failed" and bad.error_code == "unsupported_payment_method",
	      str(bad.error_code))

	frappe.delete_doc("Payment Intent", name, force=True, ignore_permissions=True)
	frappe.db.commit()
	check("15. test intent cleaned up", not frappe.db.exists("Payment Intent", name))

	passed = sum(1 for _, ok, _ in results if ok)
	print(f"\n=== {passed}/{len(results)} checks passed ===")
	print("\nNot covered here, and it is the part that matters: a real Tap to Pay payment")
	print("reaching this channel, and order_reference coming back as referenceId.")
	print("That needs a paired Android phone — payments.tests.payrexx_taptopay_probe.")
	return passed == len(results)
