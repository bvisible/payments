"""Pytest fixtures for the webshop E2E suite.

The tests target the live Frappe site at ``--base-url`` (default
``osiris.neoffice.me``). All backend calls (state setup + assertions) go
through HTTPS API using an Administrator API key — no SSH dependency, so
the suite runs from any laptop or CI.

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
				k = k.strip(); v = v.strip().strip("'\"")
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
		"""Call a whitelisted method via POST. Returns the ``message`` payload."""
		url = f"{self.base_url}/api/method/{method}"
		resp = self.session.post(url, data=kwargs, timeout=60)
		if not resp.ok:
			raise RuntimeError(f"{method} HTTP {resp.status_code}: {resp.text[:500]}")
		body = resp.json()
		# Frappe whitelisted methods wrap return in "message"
		return body.get("message", body)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def base_url(pytestconfig) -> str:
	return pytestconfig.getoption("--base-url") or "https://osiris.neoffice.me"


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
	page.context.request.post(
		f"{base_url}/api/method/login",
		form={"usr": email, "pwd": password},
	)

	# Sanity-check session via the same context's request client.
	resp = page.context.request.get(f"{base_url}/api/method/frappe.auth.get_logged_user")
	assert resp.ok, f"get_logged_user not ok: {resp.status} {resp.text()[:200]}"
	data = resp.json()
	assert data.get("message") == email, f"logged user mismatch: {data}"

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
