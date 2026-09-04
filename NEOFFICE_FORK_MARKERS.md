<!-- //// Neoffice — added file (no upstream equivalent). -->

# Neoffice fork markers

This file completes the `//// Neoffice` markers carried in the source. It lists the
files that **cannot carry a comment** — DocType and Page JSON, PO/POT catalogues,
images, `.template` scaffolds — so that the divergence map stays complete.

The map is read the same way everywhere in the fleet:

```bash
grep -rn "////" .          # every divergence that lives in code
cat NEOFFICE_FORK_MARKERS.md   # everything that could not carry a comment
```

Base of the divergence: `a682448a63d59ecf9288cfafa29cdad215ddf0ff`
(`refactor: inline immediately returned variable (backport #163) (#172)`, 2025-09-15),
the merge-base of `bvisible/payments@version-15` and `frappe/payments@version-15`.

---

## payments

### The one fact that matters at merge time

**No upstream DocType JSON is modified.** Every `.json` that differs from upstream is a
**new file** for a **new DocType** — verified with
`git diff --name-status <base>..HEAD -- '*.json'`, which returns only `A` lines. Upstream's
`stripe_settings.json`, `paypal_settings.json` and friends are untouched, so there is
nothing to reconcile field by field: take upstream's wholesale.

> Note for whoever reads `CHANGES_NEOSERVICE.md`: that file (last updated 2026-05-13) says
> `stripe_settings.json` would gain `terminal_enabled` and `terminal_default_location_id`.
> **That never happened.** Those two settings live in
> `Provider Channel Settings.config_json` instead, which is what ADR-004's junction table
> is for. `CHANGES_NEOSERVICE.md` is stale in several other places too; this file and the
> `////` markers are the current map.

### The one add/add collision

Of the ~190 files this fork adds, exactly **one** now also exists upstream:
**`payments/tests/__init__.py`**, created upstream by `6b288b9` (2026-03-25, *"resolve
merge conflicts between version-15 and develop"*) as an **empty** file. Git will raise an
add/add conflict there and nowhere else — take ours, upstream contributes nothing.
Checked by intersecting our added paths with `upstream/version-15`.

Where the conflicts will really be is in the files we **edit**: `razorpay_settings.py`
(51 upstream commits since the base), `paypal_settings.py` (17),
`templates/pages/stripe_checkout.py` (13), `stripe_settings.py` (10),
`braintree_settings.py` (9), `paytm_settings.py` (8), `pyproject.toml` (6),
`mpesa_settings.py` (5), then `stripe_checkout.html` (2) and one each for
`stripe_checkout.css`, `hooks.py`, `.gitignore`. Upstream has not touched
`payments/patches.txt` or `templates/includes/stripe_checkout.js`.

### New DocTypes — the Provider × Channel × Driver ontology (ADR-001, ADR-004)

Upstream fuses PSP credentials, channel configuration and business logic into one
`*_settings` DocType per PSP, which cannot grow a second channel without copying the
credentials. These seven DocTypes replace that pattern; `Payment Intent` is the single
fact table for every transaction whatever the PSP.

| Path | DocType | Purpose |
|---|---|---|
| `payments/payments/doctype/payment_provider/payment_provider.json` | Payment Provider | one record per PSP: `provider_name`, `display_label`, `enabled`, `mode` (test/live), `driver_class`, `credentials_json` (Password) |
| `payments/payments/doctype/payment_channel/payment_channel.json` | Payment Channel | one record per channel: `channel_code` (`web`, `terminal`, `qr_bridge`, `tap_to_pay`…), `display_label`, `icon`, `ui_kind`, `capabilities_json` |
| `payments/payments/doctype/provider_channel_settings/provider_channel_settings.json` | Provider Channel Settings | the junction, named `{provider}-{channel}`: `driver_class`, `webhook_endpoint`, `config_json` (this is where `terminal_default_location_id`, `webhook_secret`, `twint_merchant_uuid` live) |
| `payments/payments/doctype/payment_intent/payment_intent.json` | Payment Intent | the fact table, 26 fields: `status` FSM, `amount`/`currency`, `provider`/`channel`/`device`, `provider_intent_id`, `reference_doctype`/`reference_name`, next-action payload, `metadata_json` |
| `payments/payments/doctype/payment_event/payment_event.json` | Payment Event | the FSM audit trail: `intent`, `from_status`, `to_status`, `event_source`, `payload_excerpt` |
| `payments/payments/doctype/payment_device/payment_device.json` | Payment Device | a physical reader: `provider_device_id`, `device_type`, `serial_number`, `status`, `last_seen_at`. Upstream has no notion of hardware at all |
| `payments/payments/doctype/webhook_event_log/webhook_event_log.json` | Webhook Event Log | webhook de-duplication and forensics: `event_id` (the natural key), `signature_valid`, `raw_payload`, `received_at`/`processed_at` |
| `payments/payments/doctype/customer_payment_link/customer_payment_link.json` | Customer Payment Link | ties an ERPNext Customer to its PSP-side customer id, so a returning shopper is recognised |

All eight founded by `e32ecf5` (2026-05-13, *"Phase 1 — unified payment driver layer
(Provider × Channel × Driver)"*).

### New DocTypes — apps we retired and folded into this one

| Path | DocType | Origin |
|---|---|---|
| `payments/payments/doctype/wallee_settings/wallee_settings.json` | Wallee Settings (36 fields) | from the retired `wallee_integration` app — `99e929c` (2026-05-19, ADR-005) |
| `payments/payments/doctype/wallee_location/wallee_location.json` | Wallee Location | idem, `99e929c` |
| `payments/payments/doctype/wallee_terminal_configuration/wallee_terminal_configuration.json` | Wallee Terminal Configuration | idem, `99e929c` |
| `payments/payment_gateways/doctype/twint_bridge_settings/twint_bridge_settings.json` | Twint Bridge Settings | from the retired `twint_integration` app. Renamed from `Twint Settings` by `cc503b1` (2026-05-13); later grew the P12 upload and its expiry fields (`cf61f54`, 2026-06-21) |

Data already on the fleet is migrated by `payments/patches/v15_03/merge_wallee_integration.py`
and `payments/patches/v15_04/merge_twint_integration.py`.

### New DocType — mobile

| Path | DocType | Purpose |
|---|---|---|
| `payments/payments/doctype/mobile_payment_settings/mobile_payment_settings.json` | Mobile Payment Settings (Single) | one place to choose how the phone collects on site: `enable_tap_to_pay` + `tap_to_pay_provider` + `stripe_location`, `enable_twint` + `twint_provider`. `d06eb26` (2026-09-03) |

### New desk Pages

| Path | Page | Origin |
|---|---|---|
| `payments/payments/page/payment_terminal_wizard/payment_terminal_wizard.json` | `payment-terminal-wizard` — *Payment Terminal Wizard*, roles System Manager / Accounts Manager | `99e929c` (2026-05-19), with the Wallee fold-in |
| `payments/payments/page/payrexx_setup_wizard/payrexx_setup_wizard.json` | `payrexx-setup-wizard` — *Payrexx Setup*, same roles | `754ddf4` (2026-09-01) — standing a Payrexx account up by hand means touching six doctypes in the right order |

### Scaffolds — `.template` (data to the checker, Python to a human)

| Path | Origin |
|---|---|
| `payments/drivers/template/provider.py.template` | `7cfe7fa` (2026-05-13, *"Phase 7 runbooks + Phase 8 PSP template"*) — the `PaymentProviderBase` skeleton a new PSP is copied from; see `docs/adding-a-new-psp.md` |
| `payments/drivers/template/channel_driver.py.template` | idem — the `PaymentDriverBase` skeleton, one per (provider, channel) couple |

Deliberately left without a `////` header: they are copied verbatim to seed a new
driver, and the header would be copied with them.

### Empty `__init__.py` — deliberately unmarked

**26** of the added files are empty `__init__.py` package markers (0 bytes), required by
Python and by Frappe's module loader and carrying no content of their own:

`payments/setup/`, `payments/integrations/` (+ `payrexx/`, `twint/`),
`payments/patches/` (+ `v15_03` … `v15_08`),
`payments/payments/doctype/` × 12 (customer_payment_link, mobile_payment_settings,
payment_channel, payment_device, payment_event, payment_intent, payment_provider,
provider_channel_settings, wallee_location, wallee_settings,
wallee_terminal_configuration, webhook_event_log),
`payments/payments/page/` × 2, `payments/payment_gateways/doctype/twint_bridge_settings/`.

They carry no `////` header on purpose: there is nothing to explain, and a header would
be the only content in the file. The package they mark is itself marked. Listed here so
their absence from a `grep -rn "////"` is a recorded decision, not an oversight.

### Images — the TWINT overlay

Ported from the retired `twint_integration` app by `ec69d96` (2026-05-19, *"Phase 11
fusion twint_integration → payments"*). Upstream ships no image of any kind.

Binary (listed here; the nine sibling `.svg` files carry their own `<!-- //// -->` header):

- `payments/public/images/twint/line.png`
- `payments/public/images/twint/twint-app-icon.png`

### Translations

Upstream `frappe/payments` ships no catalogue at all. Ours is **PO only** — never
`translations/*.csv`, which Frappe loads *after* `locale/*.po` so a fix made only in the
CSV is silently ignored.

- `payments/locale/main.pot` — extracted template, `cc503b1` then regenerated
- `payments/locale/fr.po` — French catalogue, 15 commits, the substantive ones being
  `ef68283` (2026-05-28, first pass), `b6dbd32` (2026-06-21, Twint Bridge Settings
  labels), `ffd59ca` (2026-07-06), `8552492` (2026-07-18, `store_uuid` description),
  `f46761f` (2026-08-21, the card receipt), `f213e01` (2026-08-31, the Payrexx return page)

### CI

Not part of the product divergence, listed for completeness — these are ours and have no
upstream counterpart:

- `.github/workflows/tests.yml` — caller for the reusable fleet CI in the public
  `bvisible/neoffice-ci`, `c58f020` (2026-09-03, wave 2)
- `.github/workflows/fork-markers.yml` — the job that writes these very markers on an
  unmarked push, `d9c4ac8` (2026-09-03)
