# Promotion vers Obsidian — FAITE le 2026-05-13

> ✅ **Statut** : promotion exécutée le 2026-05-13 après accord Full Disk Access.
>
> Les notes Obsidian sont maintenant **la source de vérité narrative** ; les fichiers de ce dossier `docs/` restent la **source de vérité technique** (paths `file:line`, ADRs détaillés). Les deux se référencent mutuellement.

---

## Notes Obsidian créées

| Fichier | Contenu | Lien vault |
|---|---|---|
| `Neoffice/Payments/00-README.md` | Hub MOC (frontmatter `title` + `tags: neoffice/feature/payments/stripe/twint/posnext` + `aliases`) | [[Neoffice/Payments/00-README]] |
| `Neoffice/Payments/01-Architecture.md` | Ontologie Provider × Channel × Driver + DocTypes + FSM + drivers + webhook + bridge PHP | [[Neoffice/Payments/01-Architecture]] |
| `Neoffice/Payments/02-Decisions.md` | 4 ADRs ratifiés (unification / TWINT-PHP / Stripe-server-driven / ontologie) avec format §8 du vault CLAUDE.md (Statut / Contexte / Constats / Décision / Implications / Liens) | [[Neoffice/Payments/02-Decisions]] |
| `Neoffice/Payments/03-Phase-Tracking.md` | Avancée live par phase, statut global, reste à faire | [[Neoffice/Payments/03-Phase-Tracking]] |

## MOC mis à jour

| Fichier | Modification |
|---|---|
| `Neoffice/00-README.md` | Ajout d'une entrée dans la section « Projets / features documentés » (en tête, plus récent en premier) avec lien vers le hub Payments + résumé exhaustif |
| `wiki/log.md` | Prepend de l'entrée du 2026-05-13 (Action / Files / Outcome / Pour la suite) |

## Note importante sur la coexistence repo ↔ vault

Les deux couches sont **complémentaires** :

| Repo `payments/docs/` | Vault `Neoffice/Payments/` |
|---|---|
| Doc technique avec paths `file:line` | Doc narrative avec wikilinks |
| Lecture par devs qui éditent le code | Lecture par Claude au session start (via `wiki/index.md` → hub Neoffice → Payments) |
| Source de vérité « comment c'est codé » | Source de vérité « pourquoi on a décidé ça » |
| Versionné avec le code dans Git | Versionné avec Obsidian Git |

**Règle** : si une décision évolue (nouvelle phase, ADR amendée, décision invalidée), **mettre à jour les deux**. Le repo en premier (plus proche du code), Obsidian en miroir (narratif).
