# //// Neoffice — added file (no upstream equivalent). The commands of the hub's TWINT bridge
# //// (neoffice-devops twint-merchant-php/src/TwintBridge.php), line for line: the same parameters read the same
# //// way, the same checks in the same order, the same answers (maintenance#1721). One deliberate difference: a
# //// cash register id set on the merchant crashes the bridge; the engine sends it as the SDK means it.
# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""TwintBridge::execute, in Python."""

from __future__ import annotations

from payments.twint_engine import values
from payments.twint_engine.client import Client
from payments.twint_engine.values import SdkError, php_int, php_string

BRIDGE_VERSION = "0.1.0"  # what the PHP bridge reports in health_check
COMMANDS = (
	"register_payment",
	"monitor_status",
	"confirm_payment",
	"cancel_payment",
	"refund_payment",
	"health_check",
)
ENGINE_ONLY_COMMANDS = (
	"system_status",
)  # TWINT's CheckSystemStatus: no money, no order (proofs, diagnostics)


def execute(command: str, config: dict, params: dict | None = None, **client_options) -> dict:
	"""Run one bridge command. `config`: certificate (the .p12 bytes), certificate_password, store_uuid,
	cash_register_id, environment, soap_version ("8.7" by default, or "10")."""
	params = params if isinstance(params, dict) else {}
	config = config if isinstance(config, dict) else {}
	if command == "health_check":
		return _health_check(config)
	try:
		client = Client(
			config.get("certificate") or b"",
			php_string(config.get("certificate_password")),
			php_string(config.get("store_uuid")),
			config.get("cash_register_id"),
			php_string(config.get("environment") or "production"),
			config.get("soap_version"),
			**client_options,
		)
	except SdkError as error:
		return error.answer()
	if not config.get("certificate"):
		return {
			"success": False,
			"error": "Certificate missing or unreadable",
			"exception": values.RUNTIME_EXCEPTION,
		}
	handler = _HANDLERS.get(command)
	if handler is None:
		return {"success": False, "error": f"Unknown command: {command}"}
	try:
		return handler(client, params)
	except SdkError as error:
		return error.answer()
	finally:
		client.close()


def _health_check(config: dict) -> dict:
	"""Like the bridge's: no call to TWINT, only that a certificate is there."""
	if not config.get("certificate"):
		return {"success": False, "error": "Certificate path missing or unreadable"}
	return {
		"success": True,
		"bridge_version": BRIDGE_VERSION,
		"sdk_loaded": True,
		"cert_file_exists": True,
	}


def _register_payment(client: Client, params: dict) -> dict:
	amount = php_int(params.get("amount"))
	reference = php_string(params.get("merchant_reference"))
	if amount <= 0:
		return {"success": False, "error": "amount must be > 0"}
	if reference == "":
		return {"success": False, "error": "merchant_reference is required"}
	values.reference(reference)
	order = client.start_order(reference, amount)
	return {
		"success": True,
		"order_id": order["order_id"],
		"order_status": order["order_status"],
		"transaction_status": order["transaction_status"],
		"pairing_token": order["pairing_token"],
		"merchant_reference": reference,
	}


def _monitor_status(client: Client, params: dict) -> dict:
	order_id = php_string(params.get("order_id"))
	if order_id == "":
		return {"success": False, "error": "order_id is required"}
	order = client.monitor_order(values.uuid(order_id))
	return {"success": True, **order}


def _confirm_payment(client: Client, params: dict) -> dict:
	order_id = php_string(params.get("order_id"))
	amount = php_int(params.get("amount"))
	if order_id == "" or amount <= 0:
		return {"success": False, "error": "order_id and amount required"}
	order = client.confirm_order(values.uuid(order_id), amount)
	return {"success": True, **order}


def _cancel_payment(client: Client, params: dict) -> dict:
	order_id = php_string(params.get("order_id"))
	if order_id == "":
		return {"success": False, "error": "order_id is required"}
	order = client.cancel_order(values.uuid(order_id))
	return {"success": True, **order}


def _refund_payment(client: Client, params: dict) -> dict:
	order_id = php_string(params.get("order_id"))
	amount = php_int(params.get("amount"))
	reference = php_string(params.get("merchant_reference"))
	if order_id == "" or amount <= 0:
		return {"success": False, "error": "order_id and amount required"}
	reversal_reference = values.reference(
		reference if reference != "" else f"rev_{order_id}_{amount}"
	)
	reversal = client.reverse_order(reversal_reference, values.uuid(order_id), amount)
	return {
		"success": True,
		"order_id": reversal["order_id"],
		"reversal_status": reversal["order_status"],
	}


def _system_status(client: Client, params: dict) -> dict:
	return {"success": True, "system_status": client.check_system_status()}


_HANDLERS = {
	"register_payment": _register_payment,
	"monitor_status": _monitor_status,
	"confirm_payment": _confirm_payment,
	"cancel_payment": _cancel_payment,
	"refund_payment": _refund_payment,
	"system_status": _system_status,
}
