# //// Neoffice — added file (no upstream equivalent). How the instance reaches TWINT's Merchant service itself
# //// (maintenance#1721): HTTPS to service.twint.ch (service-pat.twint.ch for TWINT's test environment), the
# //// merchant's .p12 as the TLS client certificate (TWINT's only authentication), the server's certificate checked
# //// against the system's authorities, no redirect, bounded times. The private key never touches the disk in clear:
# //// it is written encrypted with a one-time password for the SSL context to load, and removed at once.
# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""HTTPS with the merchant's client certificate."""

from __future__ import annotations

import http.client
import os
import secrets
import shutil
import socket
import ssl
import tempfile

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.serialization import pkcs12

from payments.twint_engine import values
from payments.twint_engine.values import SdkError

HOSTS = {"production": "service.twint.ch", "testing": "service-pat.twint.ch"}
CONNECT_TIMEOUT = 15  # seconds to reach TWINT and finish the TLS handshake
EXCHANGE_TIMEOUT = (
	40  # seconds for one answer; a command makes two calls at most, under the till's 70 s
)


def environment_of(environment) -> str:
	"""As the bridge reads it: "sandbox" or "testing" (any case) is TWINT's test environment, anything else production."""
	return (
		"testing"
		if str(environment or "production").strip().lower() in ("sandbox", "testing")
		else "production"
	)


def read_certificate(p12: bytes, password: str):
	"""The merchant's key and certificates from its .p12, refused the way the SDK refuses them."""
	if not p12:
		raise SdkError("Certificate missing or unreadable", values.RUNTIME_EXCEPTION)
	try:
		key, certificate, chain = pkcs12.load_key_and_certificates(
			p12, (password or "").encode() or None
		)
	except Exception:  # noqa: BLE001 — a wrong password and a broken file read alike, as in the SDK
		raise SdkError("Reading PKCS12 file failed") from None
	if key is None or certificate is None:
		raise SdkError("Reading PKCS12 file failed")
	return key, certificate, list(chain or [])


def client_context(p12: bytes, password: str, ca_file: str | None = None) -> ssl.SSLContext:
	key, certificate, chain = read_certificate(p12, password)
	context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=ca_file)
	context.minimum_version = ssl.TLSVersion.TLSv1_2
	one_time = secrets.token_urlsafe(32)
	folder = tempfile.mkdtemp(prefix="twint-")  # 0700
	try:
		cert_path = os.path.join(folder, "cert.pem")
		key_path = os.path.join(folder, "key.pem")
		with open(os.open(cert_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as handle:
			for item in [certificate, *chain]:
				handle.write(item.public_bytes(serialization.Encoding.PEM))
		with open(os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as handle:
			handle.write(
				key.private_bytes(
					serialization.Encoding.PEM,
					serialization.PrivateFormat.PKCS8,
					serialization.BestAvailableEncryption(one_time.encode()),
				)
			)
		context.load_cert_chain(cert_path, key_path, password=one_time)
	except ssl.SSLError:
		raise SdkError("Reading PKCS12 file failed") from None
	finally:
		shutil.rmtree(folder, ignore_errors=True)
	return context


class Exchange:
	"""One POST to the Merchant service and what came back."""

	def __init__(self, status: int, content_type: str, body: bytes):
		self.status = status
		self.content_type = content_type
		self.body = body


# The far end closed a connection kept from an earlier call before answering: nothing was read, the call can go again.
_DROPPED = (http.client.RemoteDisconnected, ConnectionResetError, BrokenPipeError)


class Connection:
	"""One HTTPS connection to the Merchant service, kept for the calls of one command (enrolment, then the call).

	Like the SDK's curl: when a kept connection turns out closed before any answer, the call is sent once more on a
	new one; a fresh connection that fails is an error.
	"""

	def __init__(
		self,
		context: ssl.SSLContext,
		host: str,
		*,
		connect_timeout: float = CONNECT_TIMEOUT,
		timeout: float = EXCHANGE_TIMEOUT,
	):
		self.context = context
		self.name, _, port = host.partition(":")
		self.port = int(port or 443)
		self.connect_timeout = connect_timeout
		self.timeout = timeout
		self.connection = None

	def close(self) -> None:
		if self.connection is not None:
			self.connection.close()
			self.connection = None

	def post(self, path: str, body: bytes, action: str, user_agent: str) -> Exchange:
		kept = self.connection is not None
		try:
			return self._post(path, body, action, user_agent)
		except _DROPPED:
			self.close()
			if not kept:
				raise SdkError(
					f"Could not reach {self.name}: the connection closed without an answer"
				) from None
		except (socket.timeout, TimeoutError):
			self.close()
			raise SdkError(f"Operation timed out reaching {self.name}") from None
		except ssl.SSLError as exc:
			self.close()
			raise SdkError(f"TLS error reaching {self.name}: {exc.reason or exc}") from None
		except (OSError, http.client.HTTPException) as exc:
			self.close()
			raise SdkError(f"Could not reach {self.name}: {exc}") from None
		try:
			return self._post(path, body, action, user_agent)
		except (socket.timeout, TimeoutError):
			self.close()
			raise SdkError(f"Operation timed out reaching {self.name}") from None
		except ssl.SSLError as exc:
			self.close()
			raise SdkError(f"TLS error reaching {self.name}: {exc.reason or exc}") from None
		except (OSError, http.client.HTTPException) as exc:
			self.close()
			raise SdkError(f"Could not reach {self.name}: {exc}") from None

	def _post(self, path: str, body: bytes, action: str, user_agent: str) -> Exchange:
		if self.connection is None:
			self.connection = http.client.HTTPSConnection(
				self.name, self.port, context=self.context, timeout=self.connect_timeout
			)
			self.connection.connect()
			self.connection.sock.settimeout(self.timeout)
		connection = self.connection
		connection.putrequest("POST", path, skip_accept_encoding=True)
		for header, value in (
			("Content-Length", str(len(body))),
			("User-Agent", user_agent),
			("SOAPAction", f'"{action}"'),
			("Content-Type", 'text/xml; charset="utf-8"'),
		):
			connection.putheader(header, value)
		connection.endheaders(body)
		response = connection.getresponse()
		answer = Exchange(response.status, response.getheader("Content-Type") or "", response.read())
		if response.will_close:
			self.close()
		return answer
