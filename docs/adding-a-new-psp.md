<!-- //// Neoffice — added file (no upstream equivalent). The contract a new PSP has to satisfy:
     //// one `PaymentProviderBase` subclass, one `PaymentDriverBase` per (provider,
     //// channel) couple, the records to provision, the tests to write. It is the
     //// companion of `payments/drivers/template/*.template`, and the reason the driver
     //// layer exists at all — upstream would need a whole new `<psp>_settings` DocType.
     //// Commits: 7cfe7fa 2026-05-13 "docs(payments): Phase 7 runbooks + Phase 8 PSP
     //// template". -->
# Adding a new Payment Service Provider (template)

This guide walks through adding a new PSP (e.g. Worldline, Saferpay, Adyen,
PostFinance Pay) to the unified payments app. Expected effort: **~1 week**
including tests + a smoke run on Osiris.

## Mental model

Three concepts to add:

| | What | Where |
|---|---|---|
| 1 | **`PaymentProviderBase` subclass** | `payments/drivers/<psp>/provider.py` |
| 2 | **`PaymentDriverBase` subclass(es)** — one per `(provider, channel)` couple you want | `payments/drivers/<psp>/<channel>_driver.py` |
| 3 | **Frappe records** — one `Payment Provider`, one `Provider Channel Settings` | Inserted at install time or by an admin |

You do **not** need a new Settings DocType per PSP. Provider credentials live in
`Payment Provider.credentials_json`; per-channel knobs live in
`Provider Channel Settings.config_json`.

## Step-by-step

### 1. Create the driver module

```
payments/drivers/<psp>/
├── __init__.py
├── provider.py
├── <channel>_driver.py
└── test_<channel>_driver.py
```

### 2. Implement `<Psp>Provider(PaymentProviderBase)`

Minimum contract:
```python
from payments.drivers.base import PaymentProviderBase

class WorldlineProvider(PaymentProviderBase):
    name = "worldline"

    def get_credentials(self) -> dict:
        creds = self.provider_doc.get_credentials()
        if not creds.get("api_key"):
            raise frappe.ValidationError("Worldline missing api_key")
        return creds

    def health_check(self) -> dict:
        # Lightweight API call
        return {"ok": True, "provider": self.provider_doc.name}

    def list_supported_channels(self) -> list[str]:
        return ["terminal", "web"]
```

### 3. Implement `<Psp><Channel>Driver(PaymentDriverBase)`

```python
from payments.drivers.base import (
    DriverResponse, IntentRequest, PaymentChannelBase, PaymentDriverBase, WebhookResult,
)
from payments.drivers.<psp>.provider import <Psp>Provider

class WorldlineTerminalChannel(PaymentChannelBase):
    code = "terminal"
    capabilities = {"supports_refund": True, "supports_partial_refund": True, "async": True}

class WorldlineTerminalDriver(PaymentDriverBase):
    code = "worldline.terminal"

    @classmethod
    def from_docs(cls, provider_doc, channel_doc, binding_doc):
        return cls(WorldlineProvider(provider_doc), WorldlineTerminalChannel(), settings_doc=binding_doc)

    def create_intent(self, request: IntentRequest) -> DriverResponse: ...
    def confirm_intent(self, provider_intent_id, **kwargs) -> DriverResponse: ...
    def cancel_intent(self, provider_intent_id) -> DriverResponse: ...
    def refund(self, provider_intent_id, amount=None) -> DriverResponse: ...
    def handle_webhook(self, payload: bytes, headers: dict) -> WebhookResult: ...
```

Key conventions:

- **Idempotency keys** scoped to the Frappe `Payment Intent.name` (e.g. `pi_create_<intent_name>`)
- **Map** the PSP's transaction status to the unified FSM bucket (`succeeded` /
  `failed` / `canceled` / `refunded`) via a small dict (see
  `payments/drivers/stripe/terminal_driver.py:_EVENT_TO_STATUS` for an example)
- **Stamp** `metadata.frappe_intent_name` on the PSP intent so the webhook
  handler can find the local record (`obj["metadata"]["frappe_intent_name"]`)
- **Always return a `DriverResponse`** — never raise into the API layer; errors
  are surfaced via `status="failed"`, `error_code`, `error_message`

### 4. Webhook (if the PSP pushes them)

For a PSP with webhooks (Worldline, Saferpay), add:

```python
# payments/api/webhook_<psp>.py
@frappe.whitelist(allow_guest=True, methods=["POST"])
def handle() -> str:
    payload = frappe.request.get_data() or b""
    headers = dict(frappe.request.headers or {})
    driver = _resolve_<psp>_driver()
    result = driver.handle_webhook(payload, headers)
    if not result.signature_valid:
        frappe.local.response["http_status_code"] = 400
        return "invalid signature"
    if frappe.db.exists("Webhook Event Log", result.event_id):
        return "ok"  # dedup
    log = frappe.get_doc({
        "doctype": "Webhook Event Log",
        "event_id": result.event_id,
        "provider": "<psp>",
        "event_type": result.event_type,
        "signature_valid": 1,
        "status": "Queued",
        "raw_payload": payload.decode("utf-8"),
    }).insert(ignore_permissions=True)
    frappe.db.commit()
    frappe.enqueue(
        "payments.api.webhook_<psp>.process_event",
        queue="short",
        job_id=f"<psp>-{result.event_id}",
        deduplicate=True,
        enqueue_after_commit=True,
        log_name=log.name,
    )
    return "ok"
```

For a PSP **without** webhooks (TWINT-style), add a scheduler poll in `hooks.py`:
```python
scheduler_events["cron"]["* * * * *"] = ["payments.api.<psp>.poll_pending_<psp>_transactions"]
```

### 5. Tests

Two layers:

1. **Unit tests** (`test_<channel>_driver.py`) — mock `requests.post` / SDK calls.
   See `payments/drivers/stripe/test_terminal_driver.py` for the canonical pattern.

2. **Smoke test** (`payments/tests/phase<N>_smoke.py`) — bench-executable, idempotent,
   prints a numbered checklist. Skip gracefully if sandbox credentials are not
   present (don't fail by default — emit `skipped: true`).

### 6. Register a Payment Provider + Channel binding

One-time on the target site:

```bash
bench --site <site> execute frappe.client.insert --kwargs '{
  "doc": {
    "doctype": "Payment Provider",
    "provider_name": "worldline",
    "display_label": "Worldline",
    "enabled": 1,
    "mode": "test",
    "driver_class": "payments.drivers.worldline.terminal_driver.WorldlineTerminalDriver",
    "credentials_json": "{\"api_key\":\"sk_test_xxx\"}"
  }
}'

bench --site <site> execute frappe.client.insert --kwargs '{
  "doc": {
    "doctype": "Provider Channel Settings",
    "provider": "worldline",
    "channel": "terminal",
    "enabled": 1
  }
}'
```

### 7. Map a Mode of Payment to this driver in POSNext

```bash
bench --site <site> execute frappe.client.insert --kwargs '{
  "doc": {
    "doctype": "POS Payment Driver Mapping",
    "pos_profile": "Caisse",
    "mode_of_payment": "Worldline Card",
    "provider": "worldline",
    "channel": "terminal",
    "enabled": 1
  }
}'
```

No POSNext UI change needed — `useTerminalDriver` already dispatches based on
`next_action_type` returned by the driver.

### 8. Document the decision

Add an ADR under `docs/adr/ADR-NNN-<psp>-integration.md` and a runbook for any
PSP-specific operational concerns (cert rotation, account setup, etc.).

## Checklist before declaring "done"

- [ ] Provider class + Driver class implemented for every required channel
- [ ] Webhook endpoint OR scheduler poll added in `hooks.py`
- [ ] Unit tests covering create_intent kwargs, error paths, FSM mapping
- [ ] Smoke test runnable via `bench execute payments.tests.phase<N>_smoke.run_all`
- [ ] `Payment Provider` + `Provider Channel Settings` + (if POS) `POS Payment Driver Mapping` records seeded
- [ ] Smoke runs ALL GREEN on Osiris
- [ ] Runbook in `docs/runbooks/` for any rotation / monitoring concern
- [ ] ADR documenting the decision (alternatives considered, consequences)
- [ ] Entry in `wiki/log.md` summarizing the addition

## Reference implementations

- **Stripe Terminal**: `payments/drivers/stripe/terminal_driver.py` (webhook-driven, server-driven mode)
- **TWINT**: `payments/drivers/twint/php_bridge_driver.py` (poll-driven, PHP bridge subprocess)
- **MockDriver**: `payments/drivers/mock_driver.py` (in-memory, useful as a template)
