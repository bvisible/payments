# Payments — Architecture unifiée (Neoffice)

> **Note** : ce document est la version "source de vérité dans le repo" de l'architecture paiements de Neoffice. Il sera **promu vers Obsidian** (`Neoffice/Paiements/00-README.md`) dès que les permissions Full Disk Access seront accordées au runtime Claude Code (cf. `docs/OBSIDIAN_PROMOTION_TODO.md`).
>
> Date de création : 2026-05-13
> Auteur : Jérémy + Claude (session "unification paiements")
> Plan de référence : `~/.claude/plans/alors-est-ce-que-tu-stateless-sun.md`
> Source technique : `~/Downloads/compass_artifact_wf-575de1d2-40cc-4e77-a4dd-7593282d7f88_text_markdown.md`

---

## 1. Vue d'ensemble

L'app `payments` (fork v15 de `frappe/payments`) est étendue pour devenir le **hub unifié** des intégrations de paiement Neoffice :

- **Stripe Web checkout** (existant, conservé)
- **Stripe Terminal server-driven** (nouveau, Phase 2)
- **TWINT Merchant** via PHP Bridge centralisé sur `neoservice.neoffice.me` (nouveau, Phase 4)
- **Futurs PSP** (Worldline, Saferpay, Adyen, PostFinance Pay) : ajoutables en ~1 semaine via le pattern Driver

Les apps `twint_integration` et `neopay_integration` (désinstallées) sont **abandonnées** — du code récupérable est porté ici selon les nouvelles abstractions.

POSNext (`/Users/jeremy/GitHub/POSNext/`) devient consommateur de `payments` via une couche **`useTerminalDriver`** agnostique au PSP. Wallee est **retiré** (remplacé par les drivers Stripe Terminal / TWINT QR).

---

## 2. Ontologie & abstractions

### Concepts

| Concept | Définition | Exemples |
|---|---|---|
| **Payment Provider** | Un PSP (Payment Service Provider) ou bridge externe | `stripe`, `twint`, `worldline`, `saferpay` |
| **Payment Channel** | Un canal de consommation du PSP | `web`, `terminal`, `qr_bridge`, `billing` (futur) |
| **Payment Device** | Un terminal physique attaché à un channel POS | Stripe WisePOS E, Stripe Reader S700, Worldline T630, … |
| **Payment Driver** | Implémentation concrète d'un couple `(Provider, Channel)` | `StripeTerminalDriver`, `StripeWebDriver`, `TwintPHPBridgeDriver` |
| **Payment Intent** | Table de fait unifiée — une transaction tentée | Insertion à chaque `create_intent`, mise à jour par webhook/poll |
| **Webhook Event Log** | Événements bruts dédupliqués par `event_id` | Idempotence garantie au niveau DB |

### Cardinalités

```
Payment Provider (1) ───< (N) Provider Channel Settings   (config par couple PSP×Channel)
                                       │
                       (1) Payment Channel  ───< (N) Payment Device
                                       │
                            (N) Payment Intent ──> Reference Document  (POS Invoice, Sales Invoice, Web Form)
                                       │
                            (N) Payment Event   (FSM log par Intent)
                                       │
                            (N) Webhook Event Log  (raw events liés)
```

### Pourquoi cette séparation

Le pattern `frappe/payments` officiel fusionne *Provider* et *Channel* dans un seul `<gateway>_settings` DocType (cf. `payment_gateways/doctype/stripe_settings/stripe_settings.py`) — impossible d'ajouter Stripe Terminal sans dupliquer toutes les credentials. Avec l'ontologie ci-dessus :
- Stripe Web + Stripe Terminal + Stripe Billing partagent **les mêmes credentials** (`Payment Provider`)
- Chaque `(Provider, Channel)` a sa config dédiée (`Provider Channel Settings`)
- Ajouter un PSP = 1 record `Payment Provider` + 1 driver enregistré dans le registry (pas un nouveau DocType de config)

### FSM Payment Intent

```
                  ┌────────────────┐
                  │ requires_action│  (created)
                  └────────┬───────┘
                           ▼
                  ┌────────────────┐
              ┌── │   processing   │ ── (webhook in flight)
              │   └────────┬───────┘
              │            ▼
              │   ┌────────────────┐
              │   │   succeeded    │  (terminal state)
              │   └────────────────┘
              │
              │   ┌────────────────┐
              ├── │     failed     │  (terminal state)
              │   └────────────────┘
              │
              │   ┌────────────────┐
              ├── │    canceled    │  (terminal state)
              │   └────────────────┘
              │
              │   ┌────────────────┐
              └── │    refunded    │  (partial or full)
                  └────────────────┘
```

---

## 3. Structure des modules

```
payments/  (CWD courant, fork frappe/payments v15)
├── payments/
│   ├── doctype/
│   │   ├── payment_gateway/                # legacy frappe/payments (conservé)
│   │   ├── payment_provider/               # NEW (Phase 1)
│   │   ├── payment_channel/                # NEW (Phase 1)
│   │   ├── provider_channel_settings/      # NEW (Phase 1)
│   │   ├── payment_device/                 # NEW (Phase 1)
│   │   ├── payment_intent/                 # NEW (Phase 1)
│   │   ├── payment_event/                  # NEW (Phase 1)
│   │   ├── webhook_event_log/              # NEW (Phase 1)
│   │   └── customer_payment_link/          # NEW (Phase 1)
│   └── api/
│       ├── intent.py                       # NEW — create/get/cancel/refund
│       ├── webhook_stripe.py               # NEW — pattern raw body + sig + dedup + RQ
│       └── terminal.py                     # Phase 2 — Stripe Terminal API
├── payment_gateways/                       # legacy (Stripe Web, PayPal, Razorpay, …)
│   ├── doctype/{x}_settings/               # inchangé
│   └── stripe_integration.py
├── drivers/                                # NEW (Phase 1)
│   ├── base.py                              # ABCs : ProviderBase, ChannelBase, DriverBase
│   ├── registry.py                          # DriverRegistry
│   ├── mock_driver.py                       # tests
│   ├── stripe/
│   │   ├── provider.py                      # Phase 2
│   │   ├── terminal_driver.py               # Phase 2
│   │   └── web_driver.py                    # Phase 2 (wrap legacy)
│   └── twint/
│       ├── provider.py                      # Phase 4
│       └── php_bridge_driver.py             # Phase 4
├── docs/                                    # NEW — documentation interne
│   ├── ARCHITECTURE.md                      # ce fichier
│   ├── adr/
│   │   ├── ADR-001-unification-paiements.md
│   │   ├── ADR-002-twint-via-php-bridge.md
│   │   ├── ADR-003-stripe-terminal-server-driven.md
│   │   └── ADR-004-ontologie-provider-channel-driver.md
│   ├── runbooks/
│   │   ├── stripe-reader-enrollment.md
│   │   ├── twint-p12-rotation.md
│   │   └── incident-reader-offline.md
│   └── OBSIDIAN_PROMOTION_TODO.md           # tracking promotion vers vault
└── CHANGES_NEOSERVICE.md                    # NEW — divergence vs upstream frappe/payments
```

---

## 4. Phases d'implémentation (résumé)

Voir `~/.claude/plans/alors-est-ce-que-tu-stateless-sun.md` pour le détail.

| Phase | Durée | Statut |
|---|---|---|
| 0 — Préparation + docs Obsidian | J1-J5 parallèle | **EN COURS** |
| 1 — Fondations `payments/` (DocTypes + ABCs) | 2 sem | **EN COURS** |
| 2 — Stripe Terminal driver | 2 sem | pending |
| 3 — POSNext refonte + Wallee out | 2 sem | pending |
| 4 — PHP Bridge TWINT | 2 sem | pending |
| 5 — UI TWINT QR | 1 sem | pending |
| 6 — Webshop + reconciliation | 1 sem | pending |
| 7 — Hardware + go-live | 1 sem | pending |
| 8+ — PSP additionnels (futur) | ~1 sem/PSP | future |

---

## 5. Liens

- **Plan complet** : `~/.claude/plans/alors-est-ce-que-tu-stateless-sun.md`
- **Source technique** (Stripe Terminal CH + TWINT) : `~/Downloads/compass_artifact_wf-575de1d2-40cc-4e77-a4dd-7593282d7f88_text_markdown.md`
- **ADRs** : `docs/adr/`
- **Repo POSNext** : `/Users/jeremy/GitHub/POSNext/`
- **Repo neoffice-devops** : `/Users/jeremy/GitHub/neoffice-devops/` (pattern EBICS à répliquer)
- **Apps legacy à abandonner** : `/Users/jeremy/GitHub/twint_integration/`, `/Users/jeremy/GitHub/neopay_integration/`

---

## 6. Décisions ratifiées avec Jérémy (2026-05-13)

| # | Décision | Choix |
|---|---|---|
| 1 | App strategy | **Tout étendre dans `payments/`** (pas de 3e app) |
| 2 | Multi-PSP | Stripe en Phase 1, architecture extensible pour Worldline/Saferpay/Adyen ultérieurement |
| 3 | TWINT in-store | **PHP Bridge sur neoservice** (pattern EBICS, 1.3% vs 1.9% Stripe), multi-merchant 1 P12 par client ERPNext |
| 4 | Stripe Terminal | Mode **server-driven** (résout dépendance LAN du JS SDK) |
| 5 | POSNext | Refonte UI Vue avec abstraction Driver, Wallee retiré (UX dialog conservée mais re-câblée) |
| 6 | Tests | Hardware en commande, Phase 1-3 sur simulator Stripe (`registration_code=simulated-wpe`), instance Osiris via TransHub MCP |

---

## 7. Avancée live

> Maintenu à jour à chaque session significative. Format : `YYYY-MM-DD HH:MM` + ce qui a été fait + prochaine étape.

### 2026-05-13 17:48 — Session de planification (Jérémy + Claude)

- **Plan complet rédigé** dans `~/.claude/plans/alors-est-ce-que-tu-stateless-sun.md`
- **Exploration parallèle** : payments app, twint_integration legacy, neopay_integration legacy, POSNext, neoffice-devops, Obsidian (bloqué)
- **2 designs comparés** (pragmatique vs architectural) → choix hybride : étendre `payments/` + ontologie Provider × Channel × Driver propre
- **9 décisions** ratifiées par l'utilisateur (cf. §6 + AskUserQuestion)
- **9 tasks** créées (Phase 0 → Phase 8)
- **Branche feature** : `feat/unified-payments` créée
- **Docs initiales** : `ARCHITECTURE.md` (ce fichier), structure `docs/` posée
- **Blocage Obsidian** : permissions Full Disk Access non accordées au runtime → docs temporairement dans le repo

### 2026-05-13 18:30 — Phase 0 (docs) + Phase 1 (squelette) implémentées

**Phase 0 — Docs (terminée côté repo, en attente promotion Obsidian)** :
- 4 ADRs créés : `docs/adr/ADR-001..004` (unification, twint-php, stripe-terminal-server-driven, ontologie)
- `CHANGES_NEOSERVICE.md` (tracking divergence upstream)
- `docs/OBSIDIAN_PROMOTION_TODO.md` (procédure de promotion dès permissions OK)

**Actions utilisateur Phase 0 restantes** :
- ⚠️ Accorder **Full Disk Access** à Claude Code (Réglages Système > Confidentialité et sécurité)
- ⚠️ Confirmer activation **Stripe Terminal server-driven** sur compte CH Neoffice (email `stripe-terminal-betas@stripe.com` si nécessaire)
- ⚠️ Demander **P12 sandbox TWINT Merchant** (pour test Osiris)

**Phase 1 — Fondations `payments/` (squelette + ABCs + tests)** :
- 8 DocTypes ontologiques créés dans `payments/payments/doctype/` :
  - `payment_provider` (autoname provider_name, validation + get_credentials())
  - `payment_channel` (system DocType, capabilities JSON)
  - `provider_channel_settings` (jonction Provider×Channel, unique constraint, driver_class override)
  - `payment_device` (terminal physique, lié au binding)
  - `payment_intent` (table de fait, **FSM enforced** via `transition_to()`, autoname `PI-.YYYY.-.########`)
  - `payment_event` (FSM log append-only, autoname hash)
  - `webhook_event_log` (dedup via autoname=event_id, unique DB-level)
  - `customer_payment_link` (mapping ERPNext Customer ↔ provider_customer_id)
- Couche `drivers/` :
  - `base.py` — ABCs `PaymentProviderBase`, `PaymentChannelBase`, `PaymentDriverBase` + dataclasses `IntentRequest`, `DriverResponse`, `WebhookResult`
  - `registry.py` — `resolve_driver(provider, channel)` avec lookup override binding → provider default
  - `mock_driver.py` — `MockProvider`/`MockChannel`/`MockDriver` pour tests sans dépendance externe
  - `stripe/__init__.py`, `twint/__init__.py` — placeholders Phase 2/4
- API `payments/api/` :
  - `intent.py` — `create_intent`, `get_intent_status`, `cancel_intent`, `refund_intent` (whitelisted, normalise metadata, drive FSM via driver response)
  - `webhook_stripe.py` — pattern compass §6 : raw body + signature → dedup `event_id` unique → `frappe.enqueue(job_id=..., deduplicate=True)` → worker `process_event` push SocketIO `payment.intent.<id>.updated`
- Tests (Frappe convention `test_<doctype>.py` + intégration dans `payments/tests/`) :
  - `test_payment_provider.py` (validation + credentials JSON)
  - `test_payment_intent.py` (FSM : valid/invalid transitions, idempotence, validation amount/currency/metadata)
  - `test_webhook_event_log.py` (dedup DB-level via `DuplicateEntryError`)
  - `payments/tests/test_intent_api.py` (E2E API + MockDriver round-trip)

**Validation locale** :
- `ast.parse` : tous les Python parsent OK
- `json.loads` : tous les DocType JSON valides

**Inventaire** (44 fichiers neufs) :
- 7 docs (ARCHITECTURE, OBSIDIAN_PROMOTION_TODO, 4 ADRs, CHANGES_NEOSERVICE)
- 26 fichiers DocTypes (8 × {__init__.py + .json + .py} + 3 fichiers test)
- 6 fichiers drivers (base, registry, mock_driver, __init__ × 3)
- 3 fichiers API (intent, webhook_stripe, __init__)
- 2 fichiers tests intégration (__init__ + test_intent_api)

**Phase 1 restante** :
- ⏳ Déployer la branche `feat/unified-payments` sur Osiris via TransHub MCP
- ⏳ Lancer `bench --site osiris.local migrate` puis `bench run-tests --app payments --module payments.tests.test_intent_api`
- ⏳ Acceptance Osiris : create_intent(provider=mock, channel=terminal) round-trip + webhook replay dedup

**Prochaine étape proposée** : (option A) tester sur Osiris ce qu'on a, ou (option B) attaquer Phase 2 (Stripe Terminal driver) et tester en bloc plus tard.
