# ADR-003 — Stripe Terminal en mode server-driven (pas SDK JS client)

- **Status** : Accepted
- **Date** : 2026-05-13
- **Decided by** : Jérémy Christillin

## Context

Le mode actuel d'intégration de terminaux de paiement chez Neoffice (Wallee) souffre de problèmes de **fiabilité réseau** : le navigateur du caissier doit être sur le même LAN que le reader pour la découverte mDNS/DNS local. Sur des réseaux WiFi guest ou avec ségrégation VLAN, ça casse régulièrement.

Stripe Terminal expose deux modes d'intégration officiels :
1. **JS SDK client-side** (`@stripe/terminal-js`) — le navigateur découvre le reader sur le LAN.
2. **Server-driven** — le serveur Frappe appelle directement l'API Stripe, qui pilote le reader via son propre canal cloud persistant.

Source : compass_artifact §1, https://docs.stripe.com/terminal/payments/setup-integration?terminal-sdk-platform=server-driven.

## Decision

**Server-driven** pour les readers supportés (BBPOS WisePOS E, Stripe Reader S700, S710). Le navigateur du cashier ne parle plus jamais au reader directement — tout passe par `api.stripe.com`.

Flux end-to-end :
```
[Cashier browser]  →  [Frappe backend]  →HTTPS→  [api.stripe.com]
                                                         │
                                                         │ canal cloud persistant
                                                         ▼
                                              [WisePOS E / S700] (n'importe où sur Internet)
```

Le reader ouvre uniquement du sortant TCP/443 vers les domaines Stripe (`api.stripe.com`, `armada.stripe.com`, `gator.stripe.com`, `*.terminal-events.stripe.com`, plus `api.emms.bbpos.com` et NTP). Pas d'entrant, pas de mDNS, pas de découverte LAN.

API utilisée côté Python (stripe-python 11.6+) :
- `stripe.terminal.Location.create(...)` — création location physique
- `stripe.terminal.Reader.create(registration_code="...", location="tml_...")` — pairing
- `stripe.PaymentIntent.create(amount, currency="chf", payment_method_types=["card_present"], capture_method="manual", idempotency_key=...)` — création intent
- `stripe.terminal.Reader.process_payment_intent(reader, payment_intent=...)` — push au reader
- `stripe.terminal.Reader.set_reader_display(reader, type="cart", cart={...})` — affichage panier (display-only)

Webhooks consommés :
- `terminal.reader.action_succeeded` → trigger capture
- `terminal.reader.action_failed` → propagation failure_code
- `payment_intent.succeeded` → **source de vérité** (l'argent est encaissé)
- `payment_intent.payment_failed` → annulation

## Alternatives considérées

### Alternative — JS SDK client-side

- **Pour** : familier, beaucoup d'exemples dans la communauté Frappe (cf. `neopay_integration` legacy)
- **Contre** :
  - Casse en environnement WiFi multi-VLAN (notre cas)
  - Pas d'intégration possible si cashier sur 4G et reader sur WiFi
  - Plus de surface front-end à maintenir (JS Stripe Terminal SDK)
- **Verdict** : rejetée. C'est précisément le mode qui pose problème avec Wallee.

## Consequences

### Positives

- **Reader sur n'importe quel réseau** — cashier sur iPad 4G + reader sur WiFi staff = OK
- **Pas de découverte LAN** = plus de problèmes DNS/mDNS
- **Machine à états côté serveur** = idempotence, retries, logs centralisés
- **Frontend ultra-mince** : composant Vue qui affiche "Présenter la carte" et écoute SocketIO, c'est tout
- **Conformité PCI** : la carte ne touche jamais notre infra (le reader parle direct à Stripe)

### Négatives

- ⚠️ **À vérifier en Phase 0** : ancienne doc Stripe indiquait que le server-driven était GA aux US/CA seulement, accès sur demande pour autres pays via `stripe-terminal-betas@stripe.com`. La version actuelle ne mentionne plus la restriction, **mais confirmer avec account manager Stripe que le compte CH Neoffice est bien activé en server-driven avant Phase 2**.
- **Pas de mode offline** (limite officielle server-driven)
- **Pas de Bluetooth WisePad 3/M2** (uniquement WisePOS E + S700/S710)
- **Pas de Tap to Pay iPhone/Android** (uniquement avec SDK natifs iOS/Android)
- Le code d'erreur `terminal_reader_timeout` est **souvent un faux négatif** — ne JAMAIS recréer un PaymentIntent sur timeout, relire `GET /v1/payment_intents/{id}` puis décider.

### À surveiller

- **Compte Stripe CH activé en server-driven** : Phase 0 BLOQUE Phase 2 sinon.
- **Mises à jour firmware reader** : Stripe push automatique, reader reboot tous les soirs à minuit (heure Location). Laisser branchés la nuit.
- **WiFi 6 (802.11ax) non supporté** — rester en WPA2/WPA3 standard.

## Implementation

Phase 2 du plan (`~/.claude/plans/alors-est-ce-que-tu-stateless-sun.md`). 2 semaines, dépend de Phase 1 (fondations).

Tests :
- Phase 1-3 sur **simulator** : `registration_code=simulated-wpe` + `POST /v1/test_helpers/terminal/readers/{id}/present_payment_method`
- Phase 7 sur **hardware réel** (en commande, livraison sous quelques semaines)

## References

- Doc Stripe : https://docs.stripe.com/terminal/payments/setup-integration?terminal-sdk-platform=server-driven
- Changelog 30 juin 2025 (parité deux-étapes) : https://docs.stripe.com/changelog/basil/2025-06-30/terminal-server-support-cards-payments
- compass_artifact §1
- Code admin pairing reader : **`07139`** (pas `0-0-0-0-0-1` comme noté dans une spec antérieure)
