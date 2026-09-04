<!-- //// Neoffice — added file (no upstream equivalent). The fork's own divergence log, written
     //// at the start of the work (e32ecf5, 2026-05-13).
     //// 
     //// STALE — it says "Last update : 2026-05-13" and it means it. It predates the
     //// Wallee fold-in, the TWINT fold-in, Payrexx, Tap to Pay and the mobile surface.
     //// The one claim that was plainly false — two new fields on
     //// `stripe_settings.json` — was corrected on 2026-09-04: that file has never
     //// diverged from upstream, the settings live in
     //// `Provider Channel Settings.config_json`.
     //// The current map of the divergence is the `//// Neoffice` markers in the source
     //// plus `NEOFFICE_FORK_MARKERS.md` at the root. Read those, not this. -->
# Divergences vs upstream `frappe/payments`

> Ce fichier liste toutes les modifications de notre fork `payments/` par rapport à `frappe/payments` upstream (branche `version-15`). Objectif : garder le rebase upstream possible et documenter le pourquoi de chaque divergence.

Last update : 2026-05-13

---

## 1. Ajouts (fichiers nouveaux, zéro conflit upstream)

Tous nos ajouts sont dans des **fichiers/dossiers nouveaux** pour minimiser les conflits au rebase :

### DocTypes nouveaux
- `payments/payments/doctype/payment_provider/` (Phase 1)
- `payments/payments/doctype/payment_channel/` (Phase 1)
- `payments/payments/doctype/provider_channel_settings/` (Phase 1)
- `payments/payments/doctype/payment_device/` (Phase 1)
- `payments/payments/doctype/payment_intent/` (Phase 1)
- `payments/payments/doctype/payment_event/` (Phase 1)
- `payments/payments/doctype/webhook_event_log/` (Phase 1)
- `payments/payments/doctype/customer_payment_link/` (Phase 1)

### Modules nouveaux
- `payments/drivers/` (Phase 1) : `base.py`, `registry.py`, `mock_driver.py`, `stripe/`, `twint/`
- `payments/api/intent.py` (Phase 1)
- `payments/api/webhook_stripe.py` (Phase 1)
- `payments/api/terminal.py` (Phase 2)
- `payments/api/twint.py` (Phase 4)
- `payments/api/reconciliation.py` (Phase 6)

### Documentation nouvelle
- `payments/docs/` (entier — ARCHITECTURE, ADRs, runbooks)
- `payments/CHANGES_NEOSERVICE.md` (ce fichier)

---

## 2. Modifications de fichiers existants (à minimiser)

<!-- //// Neoffice — corrigé le 2026-09-04 : la section annonçait deux champs qui
     //// n'ont jamais été ajoutés. Conservée comme abandon, pas supprimée. -->
### `payments/payment_gateways/doctype/stripe_settings/stripe_settings.json` (Phase 2) — ABANDONNÉ
- **Prévu** : champs `terminal_enabled` (Check) et `terminal_default_location_id` (Data), pour activer le driver Terminal sur les credentials Stripe Web existantes.
- **Ce qui s'est passé** : jamais fait. `git diff upstream -- stripe_settings.json` est vide. La configuration par canal vit dans `Provider Channel Settings.config_json` (`terminal_default_location_id`, `webhook_secret_override`, …) — c'est précisément ce que l'ontologie ADR-004 existe pour éviter de dupliquer dans un `<psp>_settings` par PSP.
- **Risque rebase** : nul, le fichier ne diverge pas.

### `payments/hooks.py` (Phases 1, 4)
- **Ajout** : `website_route_rules` pour `/api/method/payments.api.webhook_stripe.handle`
- **Ajout** : `scheduler_events` pour `payments.api.twint.poll_pending_twint_transactions` (every_minute) et `payments.api.terminal.sync_stripe_readers_status` (every_5_minutes)
- **Risque rebase** : faible (additions dans des listes/dicts existants)

### `payments/payment_gateways/doctype/stripe_settings/stripe_settings.py` (Phase 2 si besoin)
- **Possiblement** : refactor mineur pour exposer `get_stripe_client()` réutilisable par les drivers Stripe (web + terminal). Si oui, documenter ici.
- **Risque rebase** : moyen — à surveiller.

### `payments/templates/pages/stripe_checkout.py` (Phase 6)
- **Ajout** : insertion d'un `Payment Intent` (provider=stripe, channel=web) au démarrage du flow web, pour unifier le reporting avec Terminal/TWINT
- **Risque rebase** : moyen — fichier touché upstream

### `payments/pyproject.toml` (Phase 2 si besoin)
- **Possiblement** : upgrade `stripe~=11.6.0` → `stripe>=15.1.0` si la parité Terminal deux-étapes n'est pas dispo
- **Risque rebase** : faible (changement de version)

---

## 3. Pas de suppression

Aucun fichier existant n'est supprimé. Tous les checkout pages legacy (Stripe, PayPal, Razorpay, Braintree, GoCardless, PayTM, M-Pesa) restent fonctionnels pour les clients qui les utilisent.

---

## 4. Procédure de rebase upstream

Quand on rebase depuis `frappe/payments` upstream :

```bash
git fetch upstream
git rebase upstream/version-15
```

<!-- //// Neoffice — liste remise à jour le 2026-09-04 : `stripe_settings.json` ne
     //// diverge pas, les pages stripe_checkout et razorpay_settings si. -->
Conflits attendus uniquement sur :
- `hooks.py` (ajouts simples)
- `stripe_checkout.py` / `stripe_checkout.html` / `includes/stripe_checkout.js` (page refaite)
- `razorpay_settings.py` (nos `log_error`)
- `pyproject.toml` (potentiellement)

Pour chacun : garder nos ajouts + accepter les changements upstream.

---

## 5. Liens

- Plan complet : `~/.claude/plans/alors-est-ce-que-tu-stateless-sun.md`
- ADRs : `docs/adr/`
- Architecture : `docs/ARCHITECTURE.md`
