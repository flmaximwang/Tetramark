# Tetramark specification

A general-purpose, version-controlled inventory ledger on [Dolt](https://www.dolthub.com/).
Four core objects — **entity**, **stock**, **aliquot**, **box** — are extended per domain by
declarative YAML **schemas** organised as **presets**. Pick presets, run `tm init`, and you have a
validated, versioned ledger. Biology is one preset (`molbio`), not the scope.

Origin: generalises the hand-written zsqlab ledgers in `<legacy-zsqlab-checkout>`
(per-DB generators, incremental SQL scripts, `stock_pellets`, `find_entity.sh`). Their proven patterns
(registry + per-kind tables with discriminator FKs, Dolt tests as the pass/fail authority, one Dolt
commit per change) are kept; their pain points (one generator per DB, bash fragility, trigger-only
logic, schema drift by hand) are removed.

Non-goals for v1: GUI, multi-entity/mixture stocks, kind-on-kind inheritance, automatic conversion of
the old zsqlab databases (migration is manual, guided by [the migration guide](../guides/migrating-a-legacy-ledger.md)).

## Chapters

| §1 | [Concepts](01-concepts.md) |
| §2 | [Schema language (YAML)](02-schema-language.md) |
| §3 | [Physical model (generated DDL)](03-physical-model.md) |
| §4 | [Validation](04-validation.md) |
| §5 | [Schema evolution](05-schema-evolution.md) |
| §6 | [CLI (`tm`)](06-cli.md) |
| §7 | [Driver layer](07-driver-layer.md) |
| §8 | [Presets](08-presets.md) |
| §9 | [Repository layout](09-repository-layout.md) |
| §10 | [Skills & docs](10-skills-and-docs.md) |

References like "§3" or "§6" in tickets, commit messages and code comments map to the
chapter with that number; each chapter file keeps its number in the title.

