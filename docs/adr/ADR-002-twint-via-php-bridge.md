<!-- //// Neoffice — added file (no upstream equivalent). Why TWINT in-store goes through a central
     //// PHP bridge on neoservice (`twint-ag/sdk`) instead of Stripe's TWINT QR: 1.3%
     //// direct against 1.9% + CHF 0.30, no CHF 5 000 cap, and TWINT is not available on
     //// Stripe's physical readers at all. Explains `payments/drivers/twint/`.
     //// Commits: e32ecf5 2026-05-13 "Phase 1". French. -->
# ADR-002 — TWINT in-store via PHP Bridge centralisé sur neoservice (pas Stripe TWINT QR)

- **Status** : Accepted
- **Date** : 2026-05-13
- **Decided by** : Jérémy Christillin

## Context

Pour permettre aux caisses Neoffice d'encaisser via **TWINT** (paiement mobile suisse), deux chemins techniques sont disponibles :

1. **TWINT via Stripe (PaymentIntent online avec QR display)** — Stripe expose nativement TWINT comme `payment_method_type` depuis mai 2024. Le QR est généré par Stripe, affiché sur l'écran caisse, le client scanne avec son app TWINT. Stripe fait foi.
2. **TWINT via PHP Bridge Twint Merchant centralisé** — déploiement de `twint-ag/sdk` (PHP officiel) sur `neoservice.neoffice.me`, appelé par Frappe via subprocess+HTTP (pattern EBICS proven). Compte TWINT Merchant direct par client ERPNext.

Source technique de l'analyse : `compass_artifact_wf-575de1d2-*.md` §2 confirme que TWINT n'est **PAS** disponible sur les readers physiques Stripe (WisePOS E, S700/S710) — seul le QR online est possible quel que soit le chemin choisi.

## Decision

**TWINT via PHP Bridge Twint Merchant centralisé sur neoservice** (chemin 2).

L'architecture symétrique à EBICS :
- `neoffice-devops/twint-merchant-php/` (Composer, `twint-ag/sdk`)
- `neoffice-devops/twint_bridge.php` (CLI subprocess)
- `neoffice-devops/neoffice_devops/api/twint.py` (proxy Frappe stateless, copie `api/ebics.py`)
- Storage P12 multi-merchant : `/home/frappe/twint-certs/{merchant_uuid}.p12` (0o600)
- Stateless : aucune donnée persistée entre appels

Côté `payments/` :
- `drivers/twint/php_bridge_driver.py` (HTTP client → neoservice)
- `payment_gateways/doctype/twint_settings/` (par client ERPNext : `merchant_uuid`, `p12_password`)

## Alternatives considérées

### Alternative — TWINT via Stripe QR online

- **Pour** :
  - 1 seul PSP, 1 réconciliation, 1 dashboard
  - Pas de PHP à maintenir
  - Webhook Stripe unique pour tous les paiements
  - Onboarding plus rapide (déjà actif sur le compte Stripe)
- **Contre** :
  - **Coût** : 1.9% + CHF 0.30 par transaction vs **1.3% sans frais fixe** en direct Twint Merchant (~0.6 point de marge pour un panier moyen)
  - Plafond CHF 5 000 par transaction (limite Stripe pour TWINT)
  - Dépendance à l'activation Stripe TWINT côté compte (jours d'attente)

### Verdict

Rejetée. Sur un volume POS Neoffice qui sera dominé par TWINT en Suisse romande, **0.6 point de marge est significatif**. Le pattern PHP est déjà opérationnel pour EBICS (proven sur `neoservice.neoffice.me` depuis 2025), reproductible.

L'utilisateur a explicitement choisi cette voie en disant : « TWINT, l'intégration, c'est plus de faire chaque serveur, mais c'est de centraliser le serveur à un endroit, comme ça ce sera beaucoup plus facile à maintenir. »

## Consequences

### Positives

- **Économie ~0.6%** sur les transactions TWINT vs Stripe TWINT QR
- **Pattern réplicable** : la plateforme PHP centrale sur neoservice peut héberger Postfinance B2B, Worldline OFCB, etc. dans le futur (utilise la même stack que `ebics-client-php`)
- **Multi-merchant** : chaque client ERPNext peut avoir son propre compte TWINT Merchant (P12 séparé)
- **Webshop** : peut bénéficier du même bridge (pas seulement POS)

### Négatives

- **2 PSP à réconcilier** côté comptabilité (Stripe pour cartes + TWINT direct)
- **SPOF** : neoservice down = TWINT down pour tous les clients (mitigation : monitoring Prometheus + alerting)
- **Webhooks TWINT** : pas de webhook natif fiable → polling 15-60s depuis le scheduler Frappe pendant les transactions actives (UX dégradée potentielle si > 30s)
- **Library `twint-ag/sdk`** peut être abandonnée (vérifier en Phase 0, fallback `pdoehring/twint`)

### À surveiller

- **Volume TWINT** vs Cartes : si TWINT < 10% du CA POS, l'écart de 0.6% ne justifie peut-être pas la complexité. Revoir à 6 mois post go-live.
- **Maintenance lib PHP** : si `twint-ag/sdk` est abandonné, REST direct ou fallback.

## Implementation

Phase 4 du plan (`~/.claude/plans/alors-est-ce-que-tu-stateless-sun.md`). 2 semaines, parallélisable avec Phase 2-3.

## References

- Pattern EBICS proven : `/Users/jeremy/GitHub/neoffice-devops/neoffice_devops/api/ebics.py` + `/Users/jeremy/GitHub/neoffice-devops/ebics_bridge.php`
- Library candidate : https://github.com/twint-ag/sdk (officielle, dépendances dans `/Users/jeremy/GitHub/twint_integration/php_service/composer.json:43-84`)
- Fallback library : `pdoehring/twint` (fork community)
- Comparaison tarifs : compass_artifact §2 + https://stripe.com/en-ch/pricing/local-payment-methods
