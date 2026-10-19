# //// Neoffice — added file (no upstream equivalent). The checks the official TWINT PHP SDK (twint-ag/sdk 1.8.4)
# //// makes on what it sends and what it reads, with its own messages and exception names, so that the instance's
# //// engine answers exactly what the hub's PHP bridge answered (maintenance#1721). The till stores the exception
# //// name as the Payment Intent's error code: it keeps the PHP class names on purpose.
# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""What the TWINT SDK accepts, and how it says no."""

from __future__ import annotations

import re

API_FAILURE = "Twint\\Sdk\\Exception\\ApiFailure"
CANCELLATION_FAILED = "Twint\\Sdk\\Exception\\CancellationFailed"
ASSERT_EXCEPTION = "Psl\\Type\\Exception\\AssertException"
INVARIANT_VIOLATION = "Psl\\Exception\\InvariantViolationException"
RUNTIME_EXCEPTION = "RuntimeException"
PHP_ERROR = "Error"

ORDER_STATUSES = ("IN_PROGRESS", "FAILURE", "SUCCESS")
TRANSACTION_STATUSES = (
	"ORDER_OK",
	"ORDER_PARTIAL_OK",
	"ORDER_RECEIVED",
	"ORDER_PENDING",
	"ORDER_CONFIRMATION_PENDING",
	"GENERAL_ERROR",
	"CLIENT_TIMEOUT",
	"MERCHANT_ABORT",
	"CLIENT_ABORT",
)
PAIRING_STATUSES = ("NO_PAIRING", "PAIRING_IN_PROGRESS", "PAIRING_ACTIVE")
CURRENCIES = ("CHF", "XXX")

MAX_REFERENCE_LENGTH = 50
QR_PREFIX = "data:image/png;base64,"
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_PREFIX = re.compile(r"^[a-zA-Z0-9_-]+$")


class SdkError(Exception):
	"""A refusal, named the way the PHP SDK names it (`exception`), with its message (`error`)."""

	def __init__(self, message: str, exception: str = API_FAILURE):
		super().__init__(message)
		self.message = message
		self.exception = exception

	def answer(self) -> dict:
		return {"success": False, "error": self.message, "exception": self.exception}


def _php_type(value) -> str:
	if value is None:
		return "null"
	if isinstance(value, bool):
		return "bool"
	if isinstance(value, int):
		return "int"
	if isinstance(value, float):
		return "float"
	return "string"


def literal(value, allowed: tuple) -> str:
	"""`Type::maybeUnionOfLiterals(...)->assert()`: one of the values the SDK knows, nothing else."""
	if value in allowed:
		return value
	names = "|".join(f'"{name}"' for name in allowed)
	raise SdkError(f'Expected "{names}", got "{_php_type(value)}".', ASSERT_EXCEPTION)


def non_empty(value) -> str:
	if isinstance(value, str) and value != "":
		return value
	raise SdkError(f'Expected "non-empty-string", got "{_php_type(value)}".', ASSERT_EXCEPTION)


def uint(value) -> int:
	if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
		return value
	raise SdkError(f'Expected "uint", got "{_php_type(value)}".', ASSERT_EXCEPTION)


def uuid(value) -> str:
	"""`new Uuid()` behind `OrderId::fromString()` and `StoreUuid::fromString()`: non-empty, 8-4-4-4-12, lowercased."""
	non_empty(value)
	if not _UUID.match(value):
		raise SdkError(
			f'Invalid UUID "{value}". Must be in the format "8-4-4-4-12"', INVARIANT_VIOLATION
		)
	return value.lower()


def reference(value: str) -> str:
	"""A merchant transaction reference: not empty, 50 bytes at most."""
	non_empty(value)
	length = len(value.encode())
	if length > MAX_REFERENCE_LENGTH:
		raise SdkError(
			f'Transaction reference "{value}" is too long. Must be {MAX_REFERENCE_LENGTH} characters or less, got {length}',
			INVARIANT_VIOLATION,
		)
	return value


def qr_code(value) -> str:
	non_empty(value)
	if not value.startswith(QR_PREFIX):
		raise SdkError("Invalid QR code format", INVARIANT_VIOLATION)
	return value


def cash_register_id(store_uuid: str, prefix) -> str:
	"""The cash register TWINT sees: `<prefix>-<store uuid>`, "Unknown-<store uuid>" when the merchant names none.

	The hub's PHP bridge crashes as soon as a prefix is set (it instantiates the SDK's CashRegisterId interface):
	the engine does what the SDK's `PrefixedCashRegisterId` asks instead.
	"""
	if prefix is None or prefix == "":
		return f"Unknown-{store_uuid}"
	prefix = str(prefix)
	limit = MAX_REFERENCE_LENGTH - 36 - 1
	if len(prefix) > limit:
		raise SdkError(f"Prefix must be at most {limit} characters long", INVARIANT_VIOLATION)
	if not _PREFIX.match(prefix):
		raise SdkError(
			"Prefix must contain only letters, digits, dashes, and underscores", INVARIANT_VIOLATION
		)
	return f"{prefix}-{store_uuid}"


def php_int(value) -> int:
	"""PHP's `(int)` cast of what the bridge received in its JSON."""
	if value is None:
		return 0
	if isinstance(value, bool):
		return int(value)
	if isinstance(value, int):
		return value
	if isinstance(value, float):
		return int(value) if value == value and abs(value) < 2**63 else 0
	if isinstance(value, str):
		match = re.match(r"^\s*([+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?)", value)
		return int(float(match.group(1))) if match else 0
	return 0


def php_string(value) -> str:
	"""PHP's `(string)` cast of a JSON scalar."""
	if value is None:
		return ""
	if value is True:
		return "1"
	if value is False:
		return ""
	if isinstance(value, float) and value.is_integer():
		return str(int(value))
	return str(value)


def amount(rappen: int) -> str:
	"""`Money::CHF($amount / 100)` as ext-soap writes it into a decimal: the shortest form, "1" and not "1.0"."""
	value = rappen / 100
	if value.is_integer():
		return str(int(value))
	return repr(value)
