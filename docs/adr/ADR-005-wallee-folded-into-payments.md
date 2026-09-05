<!-- //// Neoffice — added file (no upstream equivalent). The record eight files of this fork
     //// cite and that was never written: the decision behind 99e929c (2026-05-19,
     //// "merge wallee_integration into payments — ADR-005"). Reconstructed on 2026-09-05
     //// from that commit's message, the migration patch and the code it describes
     //// (tracker #220). English, per RULE #00; ADR-001..004 predate that rule. -->
# ADR-005 — Wallee folded into `payments` (retiring `wallee_integration`)

- **Status**: Accepted (shipped by `99e929c`, 2026-05-19; recorded 2026-09-05)
- **Date**: 2026-05-19
- **Decided by**: Jérémy Christillin
- **Builds on**: ADR-001 (one payments app), ADR-004 (Provider × Channel × Driver)

## Context

Wallee lived in its own app, `wallee_integration`, written before ADR-001 and ADR-004:

- one **single** `Wallee Settings` DocType fused the credentials of one Wallee account
  with the terminal configuration and the POS mode of payment — a second account
  (test and live on the same site) was impossible;
- its own state machine — `Wallee Transaction`, `Wallee Transaction Item`,
  `Wallee Webhook Log` — duplicated what `Payment Intent` and the unified
  `Webhook Event Log` already record for every other provider;
- its own terminal wizard (`wallee_terminal_wizard`), separate from the Stripe one;
- three Custom Fields on `POS Profile` and one on `Payment Gateway Account` to wire the
  till, and POSNext (`guest_ordering.py`) importing the app directly.

ADR-001 says the fleet has one payments app; ADR-004 says a PSP is a Provider, a way of
paying is a Channel, and the code that talks to the PSP is a Driver. `wallee_integration`
was the last payment code outside that model.

## Decision

Fold Wallee into `payments` as a Provider × Channel × Driver family and retire the app.

1. **Schema.** `Wallee Settings` becomes a per-provider record (`autoname = field:provider`,
   Link to `Payment Provider`, unique): `wallee_test` and `wallee_live` cohabit.
   `Wallee Location` and `Wallee Terminal Configuration` move under the Payments module.
   `WalleeProvider._settings()` looks the record up by provider instead of
   `frappe.get_single`.
2. **Channels and drivers.** The terminal driver stays; a `WalleeWebDriver` adds the
   `wallee.web` channel — a Wallee transaction with `auto_confirmation_enabled` and a
   redirect to the hosted payment page. Webhook handling and refunds delegate to the
   terminal driver: one Wallee state machine. The success and failure pages
   (`www/wallee/success.html`, `failed.html`) resolve a `Payment Intent`, never a legacy
   transaction, and hand over to `webshop.controllers.payment_handler`.
3. **One wizard.** `payment_terminal_wizard` (four steps: Provider → Location → Pairing →
   POS Profile) replaces the Wallee-only wizard and serves Stripe Terminal through the
   same path; the provider kind is detected from `driver_class`.
4. **Migration, idempotent** (`patches/v15_03/merge_wallee_integration.py`): the single
   becomes a per-provider row (provider `wallee_migrated` when none exists), each
   `Wallee Payment Terminal` becomes a `Payment Device`, the three legacy tables and the
   four legacy Custom Fields are dropped — after which
   `bench uninstall-app wallee_integration` is safe. A site that never had the app is
   skipped.
5. **POSNext** stops importing the app: guest ordering creates a `Payment Intent` on
   `wallee.web` through `payments.api.intent.create_intent`, keeps its response shape,
   and reads the POS mode of payment from the per-provider settings.

## Consequences

- Two Wallee accounts per site, test and live side by side — the reason the
  `<family>_test` / `<family>_live` naming exists (see #219 for the webhook endpoint it
  still trips over).
- Wallee audit lives at api.wallee.com and in the unified `Webhook Event Log`; the
  legacy tables are gone, and `Payment Intent` is the only local state.
- `wallee_integration` is retired: not installed on new sites, uninstalled by the
  migration path on old ones. Its `qr_bridge`-style residue — records the old app
  created and nothing shipped — is the same class of gap #221 closed for TWINT.
- Every file that carries the Wallee code is a `//// Neoffice` addition with no upstream
  equivalent (`NEOFFICE_FORK_MARKERS.md`): at the next upstream merge these files are
  ours to keep, and upstream's `wallee` (if it ever ships one) is a separate decision.
