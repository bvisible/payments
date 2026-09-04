<!-- //// Neoffice — added file (no upstream equivalent). The ontology, field by field. This is the
     //// reference for every DocType JSON listed in `NEOFFICE_FORK_MARKERS.md`, and it
     //// states the problem with upstream's pattern in one line: a single `*_settings`
     //// DocType per PSP fuses credentials, channel config and business logic, so a
     //// second channel means copying the credentials (Stripe web + terminal + billing
     //// = three doctypes, same API keys three times).
     //// Commits: e32ecf5 2026-05-13 "Phase 1". French. -->
# ADR-004 — Ontologie Provider × Channel × Driver (séparation propre)

- **Status** : Accepted
- **Date** : 2026-05-13
- **Decided by** : Jérémy Christillin

## Context

Le pattern actuel de `frappe/payments` (`payments/payment_gateways/doctype/{x}_settings/`) fusionne dans un **seul** DocType de Settings :
- les credentials du PSP (API keys, secrets)
- la config du channel (Web checkout URL, webhook secret)
- la logique métier (`get_payment_url`, `validate`, `finalize_request`)

Cela rend impossible d'ajouter un nouveau **channel** (Terminal physique, Server-side bridge, Billing) sans dupliquer toutes les credentials. Exemple concret : pour Stripe Web + Stripe Terminal + Stripe Billing, le pattern naïf produit 3 DocTypes `*_settings` séparés avec les mêmes API keys recopiées.

## Decision

Pose d'une ontologie à 3 niveaux :

### 1. Payment Provider (1 record par PSP)

Représente un PSP avec ses credentials de base.

| Champ | Type | Notes |
|---|---|---|
| `provider_name` | Data (unique) | `stripe`, `twint`, `wallee`, `worldline`, … |
| `display_label` | Data | "Stripe", "TWINT", … |
| `enabled` | Check | toggle global |
| `mode` | Select | `test` / `live` |
| `credentials_json` | Code (Password) | JSON encrypté contenant les clés sensibles |
| `driver_class` | Data | Référence Python ex: `payments.drivers.stripe.StripeProvider` |
| `health_check_url` | Data (read-only) | overridable |

### 2. Payment Channel (1 record par type de canal)

Représente un canal d'utilisation. Système, peu de records (4-5 valeurs).

| Champ | Type | Notes |
|---|---|---|
| `channel_code` | Data (unique) | `web`, `terminal`, `qr_bridge`, `billing` |
| `display_label` | Data | "Web checkout", "POS Terminal", … |
| `capabilities_json` | Code | JSON : `supports_refund`, `supports_tip`, `supports_partial_capture`, `async`, … |
| `icon` | Data | emoji ou Font Awesome class |

### 3. Provider Channel Settings (table de jonction, N records par Provider × Channel)

Config spécifique à un couple `(Provider, Channel)` — c'est là que vit la spécialisation.

| Champ | Type | Notes |
|---|---|---|
| `provider` | Link Payment Provider | |
| `channel` | Link Payment Channel | |
| `enabled` | Check | activation du couple |
| `driver_class` | Data | overridable (ex: `payments.drivers.stripe.terminal_driver.StripeTerminalDriver`) |
| `config_json` | Code | JSON config spécifique (ex: `terminal_location_id`, `webhook_secret`, …) |
| `webhook_endpoint` | Data (read-only) | URL `/api/method/payments.api.webhook_{provider}.handle` |

Unique sur `(provider, channel)`.

### 4. Payment Device (terminal physique)

Lié à un `Provider Channel Settings` (le couple Stripe×Terminal). Une caisse peut avoir N devices.

| Champ | Type | Notes |
|---|---|---|
| `device_label` | Data | "Caisse 1", "Comptoir mobile" |
| `provider_device_id` | Data (unique) | `tmr_xxx` chez Stripe |
| `device_type` | Select | `bbpos_wisepos_e`, `stripe_s700`, `stripe_s710`, … |
| `serial_number` | Data | |
| `provider_channel_settings` | Link | |
| `location` | Link Stripe Terminal Location (ou équivalent) | |
| `status` | Select | `online` / `offline` |
| `last_seen_at` | Datetime | |
| `device_sw_version` | Data (read-only) | |

### 5. Payment Intent (table de fait unifiée)

Une ligne par tentative de paiement, quel que soit le PSP/channel.

| Champ | Type | Notes |
|---|---|---|
| `name` | Data (unique, auto) | format `PI-2026-00000001` |
| `provider` | Link Payment Provider | |
| `channel` | Link Payment Channel | |
| `provider_channel_settings` | Link | |
| `amount` | Int | en plus petite unité (centimes/rappen) |
| `currency` | Data | ISO 4217 — `CHF`, `EUR`, … |
| `status` | Select | `requires_action` / `processing` / `succeeded` / `failed` / `canceled` / `refunded` |
| `reference_doctype` | Link DocType | "POS Invoice", "Sales Invoice", "Web Form", … |
| `reference_name` | Dynamic Link | |
| `device` | Link Payment Device | nullable |
| `provider_intent_id` | Data (unique idx) | `pi_xxx` chez Stripe, transaction_id TWINT, … |
| `client_secret` | Data | nullable, pour SDK client (Stripe Web) |
| `next_action_type` | Select | `display_card_present_modal`, `display_qr_payload`, `redirect_to_url`, `none` |
| `next_action_payload` | Code | JSON pour le frontend |
| `metadata_json` | Code | métadonnées libres |
| `error_code` | Data | si `failed` |
| `error_message` | Small Text | si `failed` |
| `created_at` | Datetime | |
| `completed_at` | Datetime | quand `succeeded`/`failed`/`canceled` |

### 6. Payment Event (FSM log)

Une ligne par transition d'état d'un Payment Intent.

| Champ | Type |
|---|---|
| `intent` | Link Payment Intent |
| `from_status` | Data |
| `to_status` | Data |
| `event_source` | Select : `api`, `webhook`, `poll`, `manual` |
| `webhook_event_log` | Link Webhook Event Log (nullable) |
| `payload_excerpt` | Small Text |
| `created_at` | Datetime |

### 7. Webhook Event Log (raw events dédupliqués)

| Champ | Type | Notes |
|---|---|---|
| `event_id` | Data (UNIQUE INDEX) | dédup garantie au niveau DB |
| `provider` | Link Payment Provider | |
| `event_type` | Data | `payment_intent.succeeded`, etc. |
| `received_at` | Datetime | |
| `processed_at` | Datetime | |
| `signature_valid` | Check | |
| `status` | Select : `Queued`, `Processed`, `Failed`, `Skipped` | |
| `raw_payload` | Code (Long Text) | |
| `error` | Small Text | si Failed |
| `intent` | Link Payment Intent | populé après traitement |

### 8. Customer Payment Link (mapping)

| Champ | Type |
|---|---|
| `customer` | Link Customer (ERPNext) |
| `provider` | Link Payment Provider |
| `provider_customer_id` | Data (idx) — `cus_xxx` chez Stripe |
| `email` | Data |
| `is_default_payment_method` | Check |

## Alternatives considérées

### Alternative — Garder N Settings DocTypes (pattern actuel)

- **Pour** : copie/colle du legacy, simple.
- **Contre** : explosion N×M, duplication credentials, impossible de garder une vue unifiée pour le reporting.
- **Verdict** : rejetée (cf. ADR-001).

## Consequences

### Positives

- **Reporting unifié** : `SELECT FROM 'Payment Intent' WHERE status='succeeded' AND created_at > ...` couvre tous les PSP.
- **Ajout PSP en ~1 sem** : 1 record `Payment Provider` + 1 driver class. Pas de nouveau DocType de config (juste un `Provider Channel Settings`).
- **Tests unitaires possibles** sur la FSM (Intent → Event → status transitions).
- **Webhook idempotent par construction** via `Webhook Event Log.event_id` UNIQUE.

### Négatives

- **Migration legacy** des transactions historiques (`Stripe Transaction`, `Twint Transaction`) vers `Payment Intent` : ~3 jours de scripting + tests, en Phase 6 si besoin (ou laisser coexister 6 mois).
- **Coût cognitif** : 3 concepts (Provider/Channel/Driver) à expliquer aux nouveaux devs. Mitigé par cet ADR + l'`ARCHITECTURE.md`.

### À surveiller

- **Schéma figé après Phase 1** : si on découvre un PSP avec une exigence qui ne rentre pas dans le schéma (ex: streaming events, multi-step capture exotique), il faudra revoir. Risque jugé faible.
- **Performance `Payment Intent`** : un index sur `(provider, status, created_at)` est obligatoire dès le départ pour le reporting.

## Implementation

Phase 1 du plan (`~/.claude/plans/alors-est-ce-que-tu-stateless-sun.md`). Création des 8 DocTypes + ABCs (`drivers/base.py`) + registry + MockDriver pour valider l'abstraction sans driver réel.

## ABCs Python (signatures clés)

```python
class PaymentProviderBase(ABC):
    name: str
    @abstractmethod
    def get_credentials(self) -> dict: ...
    @abstractmethod
    def health_check(self) -> dict: ...

class PaymentChannelBase(ABC):
    capabilities: dict
    @abstractmethod
    def supports_currency(self, ccy: str) -> bool: ...

class PaymentDriverBase(ABC):
    provider: PaymentProviderBase
    channel: PaymentChannelBase
    @abstractmethod
    def create_intent(self, intent_doc) -> dict: ...
    @abstractmethod
    def confirm_intent(self, intent_id, **kw) -> dict: ...
    @abstractmethod
    def cancel_intent(self, intent_id) -> dict: ...
    @abstractmethod
    def refund(self, intent_id, amount=None) -> dict: ...
    @abstractmethod
    def handle_webhook(self, payload: bytes, headers: dict) -> dict: ...
```

## References

- ADR-001 — décision d'étendre `payments/` (justifie le choix d'app)
- compass_artifact §6 — pattern webhook (raw body + signature + dedup + RQ)
- Plan complet : `~/.claude/plans/alors-est-ce-que-tu-stateless-sun.md`
