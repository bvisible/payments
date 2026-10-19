# //// Neoffice — added file (no upstream equivalent). TWINT's own schemas (copied from the official SDK, see
# //// twint_sdk/README.md) against which the TWINT engine's tests check every request and every answer
# //// (maintenance#1721).
# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""Validate TWINT messages against TWINT's schemas."""

from __future__ import annotations

import functools
import os

from lxml import etree

from payments.twint_engine.testing.fake_twint import SDK_DIR, VERSIONS

SOAP_ENV = "http://schemas.xmlsoap.org/soap/envelope/"


@functools.cache
def schema(version: str) -> etree.XMLSchema:
	folder = os.path.join(SDK_DIR, VERSIONS[version])
	name = next(
		entry
		for entry in os.listdir(folder)
		if entry.startswith("twint_merchant_service_") and entry.endswith(".xsd")
	)
	return etree.XMLSchema(etree.parse(os.path.join(folder, name)))


def problems(raw: bytes | str, version: str) -> list[str]:
	"""What is wrong with the body of a SOAP message (request or answer) for TWINT's schema; [] when it is valid.

	A SOAP fault is checked for its envelope only (its detail belongs to the fault schema)."""
	if isinstance(raw, str):
		raw = raw.encode()
	try:
		document = etree.fromstring(raw, parser=etree.XMLParser(resolve_entities=False, no_network=True))
	except etree.XMLSyntaxError as error:
		return [f"not XML: {error}"]
	body = document.find(f"{{{SOAP_ENV}}}Body")
	if body is None or len(body) != 1:
		return ["no single element in the SOAP body"]
	element = body[0]
	if etree.QName(element).localname == "Fault":
		return []
	checker = schema(version)
	if checker.validate(element):
		return []
	return [f"{entry.line}: {entry.message}" for entry in checker.error_log]
