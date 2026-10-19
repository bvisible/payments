# //// Neoffice — added file (no upstream equivalent). The SOAP messages of TWINT's Merchant service, written the
# //// way the official PHP SDK's ext-soap writes them (byte for byte, but for the message id), and read the way it
# //// reads the answers, faults and broken answers included (maintenance#1721). Versions 8.7 (SDK 1.8.4, what the
# //// hub's bridge speaks) and 10 (SDK 2.0.0's default).
# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""TWINT Merchant service messages: requests as the SDK writes them, answers as it reads them."""

from __future__ import annotations

from xml.sax.saxutils import escape

from lxml import etree

from payments.twint_engine import values
from payments.twint_engine.values import SdkError

SOAP_ENV = "http://schemas.xmlsoap.org/soap/envelope/"
SOAP12_ENV = "http://www.w3.org/2003/05/soap-envelope"

# What each SOAP version is, and what the SDK that speaks it calls itself (request header and user agent): the
# engine announces itself as the SDK whose messages it writes, so that TWINT sees nothing change.
VERSIONS = {
	"8.7": {"suffix": "8_7", "software": "TWINT PHP SDK", "software_version": "v1.8.4"},
	"10": {"suffix": "10", "software": "TWINT PHP SDK", "software_version": "2.0.0"},
}
DEFAULT_VERSION = "8.7"


def version_info(version: str) -> dict:
	info = VERSIONS.get(str(version or DEFAULT_VERSION))
	if info is None:
		raise SdkError(f"Unsupported TWINT service version: {version}", values.RUNTIME_EXCEPTION)
	return info


def namespace(version: str, kind: str) -> str:
	return f"http://service.twint.ch/{kind}/types/v{version_info(version)['suffix']}"


def path(version: str) -> str:
	return f"/merchant/service/TWINTMerchantServiceV{version_info(version)['suffix']}"


def user_agent(version: str) -> str:
	info = version_info(version)
	return f"{info['software']}/{info['software_version']}"


# ----------------------------------------------------------------------------------------------------------------------
# Requests
# ----------------------------------------------------------------------------------------------------------------------


def _text(value) -> str:
	return escape(str(value))


def envelope(version: str, message_id: str, body: str) -> bytes:
	"""The envelope ext-soap writes, its odd second prefix for the header included."""
	info = version_info(version)
	suffix = info["suffix"]
	return (
		'<?xml version="1.0" encoding="UTF-8"?>\n'
		f'<SOAP-ENV:Envelope xmlns:SOAP-ENV="{SOAP_ENV}" xmlns:ns1="http://service.twint.ch/base/types/v{suffix}" '
		f'xmlns:ns2="http://service.twint.ch/merchant/types/v{suffix}">'
		f'<soap:Header xmlns:soap="{SOAP_ENV}" xmlns:default="http://service.twint.ch/header/types/v{suffix}">'
		f'<default:RequestHeaderElement xmlns="http://service.twint.ch/header/types/v{suffix}">'
		f"<MessageId>{_text(message_id)}</MessageId><ClientSoftwareName>{_text(info['software'])}</ClientSoftwareName>"
		f"<ClientSoftwareVersion>{_text(info['software_version'])}</ClientSoftwareVersion>"
		"</default:RequestHeaderElement></soap:Header>"
		f"<SOAP-ENV:Body>{body}</SOAP-ENV:Body></SOAP-ENV:Envelope>\n"
	).encode()


def _merchant(store_uuid: str, cash_register: str) -> str:
	return (
		f"<ns2:MerchantInformation><ns1:MerchantUuid>{_text(store_uuid)}</ns1:MerchantUuid>"
		f"<ns1:CashRegisterId>{_text(cash_register)}</ns1:CashRegisterId></ns2:MerchantInformation>"
	)


def _amount(rappen: int) -> str:
	return f"<ns1:Amount>{values.amount(rappen)}</ns1:Amount><ns1:Currency>CHF</ns1:Currency>"


def enroll_cash_register(store_uuid: str, cash_register: str) -> str:
	return (
		"<ns2:EnrollCashRegisterRequestElement>"
		f"{_merchant(store_uuid, cash_register)}<ns2:CashRegisterType>EPOS</ns2:CashRegisterType>"
		"</ns2:EnrollCashRegisterRequestElement>"
	)


def check_system_status(store_uuid: str) -> str:
	return f"<ns2:CheckSystemStatusRequestElement>{_merchant(store_uuid, '')}</ns2:CheckSystemStatusRequestElement>"


def start_order(store_uuid: str, cash_register: str, rappen: int, reference: str) -> str:
	return (
		f"<ns2:StartOrderRequestElement>{_merchant(store_uuid, cash_register)}"
		'<ns2:Order type="PAYMENT_IMMEDIATE" confirmationNeeded="true"><ns2:PostingType>GOODS</ns2:PostingType>'
		f"<ns2:RequestedAmount>{_amount(rappen)}</ns2:RequestedAmount>"
		f"<ns2:MerchantTransactionReference>{_text(reference)}</ns2:MerchantTransactionReference></ns2:Order>"
		"<ns2:UnidentifiedCustomer>true</ns2:UnidentifiedCustomer><ns2:QRCodeRendering>true</ns2:QRCodeRendering>"
		"</ns2:StartOrderRequestElement>"
	)


def reverse_order(
	store_uuid: str, cash_register: str, rappen: int, reference: str, order_uuid: str
) -> str:
	return (
		f"<ns2:StartOrderRequestElement>{_merchant(store_uuid, cash_register)}"
		'<ns2:Order type="REVERSAL" confirmationNeeded="false"><ns2:PostingType>GOODS</ns2:PostingType>'
		f"<ns2:RequestedAmount>{_amount(rappen)}</ns2:RequestedAmount>"
		f"<ns2:MerchantTransactionReference>{_text(reference)}</ns2:MerchantTransactionReference>"
		f"<ns2:Link><ns2:OrderUuid>{_text(order_uuid)}</ns2:OrderUuid></ns2:Link></ns2:Order>"
		"<ns2:UnidentifiedCustomer>true</ns2:UnidentifiedCustomer></ns2:StartOrderRequestElement>"
	)


def monitor_order(store_uuid: str, cash_register: str, order_uuid: str) -> str:
	return (
		f"<ns2:MonitorOrderRequestElement>{_merchant(store_uuid, cash_register)}"
		f"<ns2:OrderUuid>{_text(order_uuid)}</ns2:OrderUuid><ns2:WaitForResponse>false</ns2:WaitForResponse>"
		"</ns2:MonitorOrderRequestElement>"
	)


def confirm_order(store_uuid: str, cash_register: str, order_uuid: str, rappen: int) -> str:
	return (
		f"<ns2:ConfirmOrderRequestElement>{_merchant(store_uuid, cash_register)}"
		f"<ns2:OrderUuid>{_text(order_uuid)}</ns2:OrderUuid><ns2:RequestedAmount>{_amount(rappen)}</ns2:RequestedAmount>"
		"<ns2:PartialConfirmation>false</ns2:PartialConfirmation></ns2:ConfirmOrderRequestElement>"
	)


def cancel_order(store_uuid: str, cash_register: str, order_uuid: str) -> str:
	return (
		f"<ns2:CancelOrderRequestElement>{_merchant(store_uuid, cash_register)}"
		f"<ns2:OrderUuid>{_text(order_uuid)}</ns2:OrderUuid></ns2:CancelOrderRequestElement>"
	)


# ----------------------------------------------------------------------------------------------------------------------
# Answers
# ----------------------------------------------------------------------------------------------------------------------

_PARSER = etree.XMLParser(
	resolve_entities=False, no_network=True, huge_tree=False, remove_comments=True
)
STATUS_TRANSITION_ERROR = "STATUS_TRANSITION_ERROR"


class Answer:
	"""The response element of one operation, read by local name in the merchant namespace as ext-soap does."""

	def __init__(self, element, version: str):
		self.element = element
		self.ns = namespace(version, "merchant")
		self.base = namespace(version, "base")

	def child(self, *names: str, base_last: bool = False):
		node = self.element
		for index, name in enumerate(names):
			if node is None:
				return None
			ns = self.base if base_last and index == len(names) - 1 else self.ns
			node = node.find(f"{{{ns}}}{name}")
		return node

	def text(self, *names: str, base_last: bool = False):
		node = self.child(*names, base_last=base_last)
		if node is None:
			return None
		return node.text or ""

	def int(self, *names: str):
		raw = self.text(*names)
		if raw is None:
			return None
		try:
			return int(raw.strip())
		except ValueError:
			raise SdkError("SOAP-ERROR: Encoding: Violation of encoding rules") from None


def read(raw: bytes, version: str, operation: str, *, cancel: bool = False) -> Answer:
	"""The response element of `operation`, or the SDK's refusal: a fault, or an answer that is no SOAP at all."""
	if not raw:
		raise _no_result(operation)
	try:
		document = etree.fromstring(raw, parser=_PARSER) if raw.strip() else None
	except etree.XMLSyntaxError:
		document = None
	if document is None:
		raise SdkError("looks like we got no XML document")
	if etree.QName(document).localname != "Envelope" or etree.QName(document).namespace not in (
		SOAP_ENV,
		SOAP12_ENV,
	):
		raise SdkError("Wrong Version")
	soap = etree.QName(document).namespace
	body = document.find(f"{{{soap}}}Body")
	if body is None:
		raise SdkError("Body must be present in a SOAP envelope")
	fault = body.find(f"{{{soap}}}Fault")
	if fault is not None:
		raise _fault(fault, cancel)
	element = body.find(f"{{{namespace(version, 'merchant')}}}{operation}ResponseElement")
	if element is None:
		raise _no_result(operation)
	return Answer(element, version)


def _no_result(operation: str) -> SdkError:
	"""ext-soap returns nothing for an empty answer or one without the operation's element, and the SDK's
	generated client refuses that nothing."""
	return SdkError(
		f'Expected "Twint\\Sdk\\Generated\\Type\\{operation}ResponseType", got "Phpro\\SoapClient\\Type\\MixedResult".',
		values.ASSERT_EXCEPTION,
	)


def _fault(fault, cancel: bool) -> SdkError:
	message = (fault.findtext("faultstring") or "").strip()
	exception = values.API_FAILURE
	if cancel and _is_status_transition_error(fault.find("detail")):
		exception = values.CANCELLATION_FAILED
	return SdkError(message, exception)


def _is_status_transition_error(detail) -> bool:
	"""The SDK's own test (ExtSoapErrorClassifier): a detail element NAMED `STATUS_TRANSITION_ERROR`, code 1200.

	TWINT's schema names that fault `StatusTransitionError`, so the SDK 1.8.4 never sees it and the bridge answers
	ApiFailure: the engine applies the very same test, to answer what the bridge answered.
	"""
	if detail is None:
		return False
	for child in detail:
		if not isinstance(child.tag, str) or etree.QName(child).localname != STATUS_TRANSITION_ERROR:
			continue
		code = next(
			(
				node
				for node in child
				if isinstance(node.tag, str) and etree.QName(node).localname == "ErrorCode"
			),
			None,
		)
		if code is None:
			continue
		fields = {
			etree.QName(node).localname: (node.text or "") for node in code if isinstance(node.tag, str)
		}
		if fields.get("Status") == STATUS_TRANSITION_ERROR and fields.get("Code") == "1200":
			return True
	return False


def order_fields(answer: Answer, *, nested: bool, pairing: bool) -> dict:
	"""`new Order(...)` as the SDK builds it from an answer, checks in the SDK's order, and what the bridge prints."""
	prefix = ("Order",) if nested else ()
	status_parent = ("Order", "Status") if nested else ("OrderStatus",)
	uuid_name = "Uuid" if nested else "OrderUuid"
	order_id = values.uuid(answer.text(*prefix, uuid_name))
	if nested:
		values.non_empty(answer.text("Order", "MerchantTransactionReference"))
	order_status = values.literal(answer.text(*status_parent, "Status"), values.ORDER_STATUSES)
	transaction_status = values.literal(
		answer.text(*status_parent, "Reason"), values.TRANSACTION_STATUSES
	)
	if nested:
		currency = values.non_empty(answer.text("Order", "RequestedAmount", "Currency", base_last=True))
		values.literal(currency, values.CURRENCIES)
	if pairing:
		values.literal(answer.text("PairingStatus"), values.PAIRING_STATUSES)
	return {
		"order_id": order_id,
		"order_status": order_status,
		"transaction_status": transaction_status,
	}
