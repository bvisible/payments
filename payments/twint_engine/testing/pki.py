# //// Neoffice — added file (no upstream equivalent). Throwaway certificates for the TWINT engine's tests and for
# //// the bench that runs the engine and the PHP SDK side by side (maintenance#1721): a CA, the fake TWINT server's
# //// certificate, and a merchant's .p12 in the format TWINT issues (PBES2, AES-256-CBC, PBKDF2 10 000 rounds, MAC
# //// SHA-256, as the three .p12 of the fleet read on 19.10.2026), or in the old 3DES format.
# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""Throwaway PKI for the TWINT engine's tests: nothing here is ever used against TWINT."""

from __future__ import annotations

import datetime
import ipaddress
import os
from dataclasses import dataclass

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID

MERCHANT_PASSWORD = "merchant-test-password"


@dataclass
class TestPki:
	folder: str
	ca_pem: str  # path of the CA that signed both the server and the merchant
	server_cert: str
	server_key: str
	merchant_p12: bytes  # the merchant's .p12, TWINT's format
	merchant_p12_legacy: bytes  # the same key and certificate, 3DES
	stranger_p12: bytes  # a .p12 from a CA the server does not trust
	password: str = MERCHANT_PASSWORD

	def write(self, name: str, data: bytes) -> str:
		path = os.path.join(self.folder, name)
		with open(path, "wb") as handle:
			handle.write(data)
		os.chmod(path, 0o600)
		return path


def _key():
	return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _name(common_name: str) -> x509.Name:
	return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _certificate(subject, issuer, public_key, signing_key, *, ca=False, server=False):
	now = datetime.datetime.now(datetime.timezone.utc)
	builder = (
		x509.CertificateBuilder()
		.subject_name(subject)
		.issuer_name(issuer)
		.public_key(public_key)
		.serial_number(x509.random_serial_number())
		.not_valid_before(now - datetime.timedelta(days=1))
		.not_valid_after(now + datetime.timedelta(days=30))
		.add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
	)
	if server:
		builder = builder.add_extension(
			x509.SubjectAlternativeName(
				[x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
			),
			critical=False,
		)
	return builder.sign(signing_key, hashes.SHA256())


def _p12(key, cert, password: str, legacy: bool) -> bytes:
	if legacy:
		encryption = (
			serialization.PrivateFormat.PKCS12.encryption_builder()
			.kdf_rounds(2048)
			.key_cert_algorithm(pkcs12.PBES.PBESv1SHA1And3KeyTripleDESCBC)
			.hmac_hash(hashes.SHA1())
			.build(password.encode())
		)
	else:
		encryption = (
			serialization.PrivateFormat.PKCS12.encryption_builder()
			.kdf_rounds(10000)
			.key_cert_algorithm(pkcs12.PBES.PBESv2SHA256AndAES256CBC)
			.hmac_hash(hashes.SHA256())
			.build(password.encode())
		)
	return pkcs12.serialize_key_and_certificates(b"merchant", key, cert, None, encryption)


def make_pki(folder: str) -> TestPki:
	"""Write a CA and the fake server's certificate into `folder` and return the merchant's .p12 bytes."""
	os.makedirs(folder, exist_ok=True)
	ca_key = _key()
	ca_cert = _certificate(
		_name("TWINT engine test CA"),
		_name("TWINT engine test CA"),
		ca_key.public_key(),
		ca_key,
		ca=True,
	)
	server_key = _key()
	server_cert = _certificate(
		_name("localhost"), ca_cert.subject, server_key.public_key(), ca_key, server=True
	)
	merchant_key = _key()
	merchant_cert = _certificate(
		_name("Test merchant"), ca_cert.subject, merchant_key.public_key(), ca_key
	)
	stranger_ca = _key()
	stranger_key = _key()
	stranger_cert = _certificate(
		_name("Stranger"), _name("Other CA"), stranger_key.public_key(), stranger_ca
	)

	pki = TestPki(
		folder=folder,
		ca_pem="",
		server_cert="",
		server_key="",
		merchant_p12=_p12(merchant_key, merchant_cert, MERCHANT_PASSWORD, legacy=False),
		merchant_p12_legacy=_p12(merchant_key, merchant_cert, MERCHANT_PASSWORD, legacy=True),
		stranger_p12=_p12(stranger_key, stranger_cert, MERCHANT_PASSWORD, legacy=False),
	)
	pki.ca_pem = pki.write("ca.pem", ca_cert.public_bytes(serialization.Encoding.PEM))
	pki.server_cert = pki.write("server.pem", server_cert.public_bytes(serialization.Encoding.PEM))
	pki.server_key = pki.write(
		"server.key",
		server_key.private_bytes(
			serialization.Encoding.PEM,
			serialization.PrivateFormat.PKCS8,
			serialization.NoEncryption(),
		),
	)
	return pki
