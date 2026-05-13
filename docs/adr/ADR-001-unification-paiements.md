# ADR-001 — Unification des intégrations de paiement Neoffice

- **Status** : Accepted
- **Date** : 2026-05-13
- **Decided by** : Jérémy Christillin
- **Participants** : Claude (Opus 4.7)
- **Context plan file** : `~/.claude/plans/alors-est-ce-que-tu-stateless-sun.md`

## Context

L'écosystème de paiement Neoffice est éclaté en quatre apps Frappe distinctes :
- `frappe/payments` (fork v15, CWD courant) — hub web checkout officiel, 7 gateways, mais aucune notion de Terminal physique ni de service externe PHP.
- `twint_integration` — app TWINT désinstallée avec service PHP Slim 4 séparé.
- `neopay_integration` — app Stripe Terminal v2.0 abandonnée avec 8 modules JS POS Awesome-couplés.
- `POSNext` — consomme Wallee (Worldline) via un import **en dur** (`pos_next/api/guest_ordering.py:790`), aucune abstraction de driver.

Le besoin actuel :
1. Nouvelle intégration **Stripe** (web + terminal physique de paiement) en remplacement de Wallee.
2. Nouvelle intégration **TWINT** in-store (cashier encaisse via TWINT).
3. Architecture **extensible** pour ajouter d'autres marques de terminal (Worldline, Saferpay, Adyen, PostFinance Pay) sans réécrire POSNext.

## Decision

**Tout étendre dans la Payments app (`/Users/jeremy/GitHub/payments/`)** — pas de 3e app, pas de fork hardcore de `frappe/payments`. On ajoute des fichiers nouveaux (DocTypes, drivers, API) sans modifier les fichiers existants (sauf ajouts marginaux de champs).

Pose d'une **ontologie Provider × Channel × Driver** :
- `Payment Provider` = un PSP (Stripe, Twint, futurs)
- `Payment Channel` = un canal de consommation (web, terminal, qr_bridge)
- `Payment Driver` = implémentation concrète d'un couple (Provider, Channel)
- `Payment Intent` = table de fait unifiée pour toutes les transactions

POSNext consomme cette architecture via une couche **`useTerminalDriver` agnostique** au PSP. Wallee est retiré (l'UX dialog est conservée mais re-câblée).

## Alternatives considérées

### Alternative 1 — Nouvelle app `neoffice_payments` séparée

- **Pour** : ne dépend pas de l'upstream `frappe/payments` (mort-vivant selon ses propres mainteneurs, issues #105 #108).
- **Contre** : un repo de plus à maintenir, duplication des concepts existants dans `payments/payment_gateways/`.
- **Verdict** : rejeté — Jérémy veut explicitement « tout réunir dans cette application » (`payments/`).

### Alternative 2 — Garder le pattern N Settings DocTypes (un par PSP × Channel)

- **Pour** : simplicité initiale, copie/colle du legacy.
- **Contre** : explosion N×M (4 PSP × 3 channels = 12 DocTypes de config). Duplication des credentials.
- **Verdict** : rejeté — l'utilisateur a confirmé vouloir une architecture « prête pour ajouter Worldline/Saferpay/etc. plus tard ». L'ontologie est obligatoire.

### Alternative 3 — Décentraliser : `payments/` gère le web, POSNext gère le terminal

- **Pour** : respecte les frontières actuelles.
- **Contre** : duplication des drivers Stripe entre les deux repos. POSNext devient lui-même un mini-PSP.
- **Verdict** : rejeté — Jérémy veut un hub unique pour la maintenance.

## Consequences

### Positives

- **Un seul code path** pour Stripe (web + terminal + futur billing) via le même Provider/credentials.
- **Reporting financier unifié** via la table `Payment Intent` (au lieu de N tables `*_transaction`).
- **Ajout d'un PSP en ~1 semaine** : 1 driver + 1 record Provider + 1 mapping POSNext (aucune modif UI Vue).
- **POSNext propre** : `useTerminalDriver` agnostic, change de PSP par config (Mode of Payment mapping).
- **Webhook idempotence** garantie par `Webhook Event Log.event_id` UNIQUE.

### Négatives

- **Migration legacy** : porter `Stripe Settings` legacy + `Twint Settings` legacy vers la nouvelle ontologie demande un script de migration (~3 jours).
- **Rebase upstream** plus délicat : on ajoute des champs à `stripe_settings.json` (minimum, documentés dans `CHANGES_NEOSERVICE.md`).
- **Coût cognitif initial** : 3 concepts (Provider/Channel/Driver) à intégrer pour un nouveau dev.

### À surveiller

- Si `frappe/payments` upstream livre un jour un support Terminal natif (improbable vu le statut maintainership), on évaluera la consolidation.
- Si l'utilisateur ajoute > 5 PSP dans les 12 mois, valider que l'ontologie tient (probable).

## Implementation

Voir le plan complet : `~/.claude/plans/alors-est-ce-que-tu-stateless-sun.md`.

Phases :
0. Préparation + docs Obsidian (J1-J5)
1. Fondations `payments/` — ABCs + DocTypes ontologiques (2 sem)
2. Stripe Terminal server-driven driver (2 sem)
3. POSNext refonte + Wallee out (2 sem)
4. PHP Bridge TWINT sur neoservice (2 sem)
5. UI TWINT QR (1 sem)
6. Webshop + reconciliation (1 sem)
7. Hardware tests + go-live Osiris (1 sem)
8+. Templates pour PSP additionnels (futur)

## References

- Plan détaillé : `~/.claude/plans/alors-est-ce-que-tu-stateless-sun.md`
- Source technique : `~/Downloads/compass_artifact_wf-575de1d2-40cc-4e77-a4dd-7593282d7f88_text_markdown.md`
- ADR-002 (TWINT via PHP Bridge) — choix complémentaire
- ADR-003 (Stripe Terminal server-driven) — mode opératoire
- ADR-004 (Ontologie Provider × Channel × Driver) — détail technique
- Code legacy abandonné : `/Users/jeremy/GitHub/twint_integration/`, `/Users/jeremy/GitHub/neopay_integration/`
