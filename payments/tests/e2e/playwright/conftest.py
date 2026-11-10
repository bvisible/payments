# //// Neoffice — added file (no upstream equivalent). Pytest fixtures for the suite. Every
# //// backend call goes over HTTP with an Administrator API key rather than SSH, so the
# //// fixtures need no shell on the server. Needs `E2E_API_KEY` / `E2E_API_SECRET`
# //// and targets `--base-url`: the test clone `prodclone.local` of osiris by default, never a live
# //// site (maintenance #950: every run used to write into osiris's prod.local).
# //// Commits: 187b5c8 2026-05-20 "add Playwright Python suite with 9 webshop scenarios".
"""Pytest fixtures for the webshop E2E suite.

The tests target the Frappe site at ``--base-url`` (default
``http://prodclone.local:8000``: the test clone of osiris, served by the bench's
own web server; run the suite on osiris, see README). All backend calls (state
setup + assertions) go through the HTTP API using an Administrator API key.

A live site is refused before anything runs: ``prod.local`` and every host that
is not a ``*.local`` test site (osiris.neoffice.me serves prod.local). The suite
creates and cancels documents, and a run on prod.local left 117 validated test
invoices in its ledger (maintenance #950).

Required env vars (or override via pytest --base-url):
- ``E2E_API_KEY`` : Administrator API key on the target Frappe site
- ``E2E_API_SECRET`` : matching secret
- ``E2E_BASE_URL`` (optional): defaults to ``--base-url``

For convenience, the keys can also be cached in ``~/.e2e-osiris.env`` ::

    E2E_API_KEY=...
    E2E_API_SECRET=...
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
import requests
from playwright.sync_api import Page

# Ensure tests can import sibling modules (helpers.py).
sys.path.insert(0, str(Path(__file__).parent))


# ---------------------------------------------------------------------------
# Backend helper (HTTPS API)
# ---------------------------------------------------------------------------


def _load_credentials() -> tuple[str, str]:
	"""Try env vars first, then ~/.e2e-osiris.env file."""
	key = os.environ.get("E2E_API_KEY")
	secret = os.environ.get("E2E_API_SECRET")
	if key and secret:
		return key, secret

	env_file = Path.home() / ".e2e-osiris.env"
	if env_file.exists():
		for line in env_file.read_text().splitlines():
			if "=" in line and not line.startswith("#"):
				k, v = line.split("=", 1)
				k = k.strip()
				v = v.strip().strip("'\"")
				if k == "E2E_API_KEY" and not key:
					key = v
				elif k == "E2E_API_SECRET" and not secret:
					secret = v
	if not (key and secret):
		raise pytest.UsageError(
			"Missing E2E_API_KEY / E2E_API_SECRET — set them as env vars or in ~/.e2e-osiris.env"
		)
	return key, secret


class FrappeAPI:
	"""Minimal HTTPS client for Frappe whitelisted methods."""

	def __init__(self, base_url: str, key: str, secret: str) -> None:
		self.base_url = base_url.rstrip("/")
		self.session = requests.Session()
		self.session.headers.update({"Authorization": f"token {key}:{secret}"})

	def call(self, method: str, **kwargs: Any) -> Any:
		"""Call a whitelisted method via POST. Returns the ``message`` payload.

		Retries on timeout / 5xx — osiris is shared and can lag under load.
		"""
		url = f"{self.base_url}/api/method/{method}"
		last_err: Exception | None = None
		for attempt in range(3):
			try:
				resp = self.session.post(url, data=kwargs, timeout=120)
				if resp.status_code >= 500:
					last_err = RuntimeError(f"{method} HTTP {resp.status_code}")
					import time

					time.sleep(3)
					continue
				if not resp.ok:
					raise RuntimeError(f"{method} HTTP {resp.status_code}: {resp.text[:500]}")
				body = resp.json()
				return body.get("message", body)
			except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
				last_err = exc
				import time

				time.sleep(3)
		raise RuntimeError(f"{method} failed after 3 attempts: {last_err}")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


DEFAULT_BASE_URL = "http://prodclone.local:8000"


def refuse_a_live_site(url: str) -> str | None:
	"""Why this URL must not be tested, or None when it is a test site (a ``*.local`` name other than prod.local)."""
	from urllib.parse import urlparse

	host = (urlparse(url).hostname or "").lower()
	if host == "prod.local":
		return "prod.local is osiris's live site"
	if not host.endswith(".local"):
		return f"{host or url!r} is not a test site (*.local): a public host serves a live site"
	return None


@pytest.fixture(scope="session")
def base_url(pytestconfig) -> str:
	url = pytestconfig.getoption("--base-url") or DEFAULT_BASE_URL
	reason = refuse_a_live_site(url)
	if reason:
		pytest.exit(
			f"Refusing to run the e2e suite against {url}: {reason}. Use the test clone (README).",
			returncode=2,
		)
	return url


@pytest.fixture(scope="session")
def backend(base_url: str) -> FrappeAPI:
	key, secret = _load_credentials()
	return FrappeAPI(base_url, key, secret)


@pytest.fixture(scope="session")
def site_config(backend: FrappeAPI) -> dict[str, Any]:
	"""Pull the e2e_* + enable_e2e_simulators keys from site_config.

	We use frappe.client.get_value on a non-existent dummy doctype path
	doesn't work, so instead we expose a tiny whitelisted helper —
	``payments.tests.e2e.fixtures.get_e2e_site_config``.
	"""
	cfg = backend.call("payments.tests.e2e.fixtures.get_e2e_site_config")
	assert cfg.get("e2e_test_user_password"), (
		"Missing e2e_test_user_password in site_config (check via ssh: "
		"`grep e2e_test_user_password sites/<site>/site_config.json`)"
	)
	return cfg


@pytest.fixture(scope="session", autouse=True)
def ensure_customer(backend: FrappeAPI):
	"""Once-per-session: ensure the fixed test customer + login user exist."""
	return backend.call("payments.tests.e2e.fixtures.ensure_test_customer")


@pytest.fixture(autouse=True)
def reset_state(backend: FrappeAPI, ensure_customer):
	"""Cancel/delete test artifacts BEFORE every test for idempotence."""
	stats = backend.call("payments.tests.e2e.fixtures.reset_test_env")
	yield stats


@pytest.fixture
def paying_item(backend: FrappeAPI) -> dict[str, Any]:
	"""A published Website Item with positive price."""
	return backend.call("payments.tests.e2e.fixtures.get_test_item_for_checkout")


@pytest.fixture
def logged_in_page(page: Page, site_config: dict, base_url: str) -> Page:
	"""Playwright Page already authenticated as the fixed test user."""
	email = site_config["e2e_test_user_email"]
	password = site_config["e2e_test_user_password"]

	# Frappe login via the JSON API — sets sid cookie on the browser context.
	# Retry a couple of times: osiris can be slow under test load.
	last_err = None
	for attempt in range(3):
		try:
			page.context.request.post(
				f"{base_url}/api/method/login",
				form={"usr": email, "pwd": password},
				timeout=60_000,
			)
			resp = page.context.request.get(
				f"{base_url}/api/method/frappe.auth.get_logged_user",
				timeout=60_000,
			)
			assert resp.ok, f"get_logged_user not ok: {resp.status} {resp.text()[:200]}"
			data = resp.json()
			assert data.get("message") == email, f"logged user mismatch: {data}"
			break
		except Exception as exc:  # noqa: BLE001
			last_err = exc
			if attempt == 2:
				raise
			page.wait_for_timeout(3_000)

	# Navigate to /cart (lightweight) so subsequent locators have a real page.
	# Avoid the homepage — heavy theme assets push DOMContentLoaded past 30s.
	page.set_default_timeout(60_000)
	page.goto(f"{base_url}/cart", wait_until="domcontentloaded", timeout=60_000)
	return page


# ---------------------------------------------------------------------------
# Re-usable helpers (importable from tests)
# ---------------------------------------------------------------------------


def assert_payment_complete(backend: FrappeAPI, payment_intent: str) -> dict[str, Any]:
	return backend.call(
		"payments.tests.e2e.assertions.assert_payment_complete",
		payment_intent=payment_intent,
	)


def list_recent_test_intents(backend: FrappeAPI, minutes: int = 30) -> list[dict[str, Any]]:
	return backend.call(
		"payments.tests.e2e.assertions.list_recent_test_intents",
		minutes=minutes,
	)


def assert_recent_sales_order(backend: FrappeAPI, minutes: int = 10) -> dict[str, Any]:
	"""PSP-agnostic assertion : a submitted Sales Order exists for the test customer."""
	return backend.call(
		"payments.tests.e2e.assertions.assert_recent_sales_order",
		minutes=minutes,
	)
