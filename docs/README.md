# Documentation

| Where | What it is |
|---|---|
| [`spec/`](spec/README.md) | **The specification** — how Tetramark works, one file per chapter (§1…§10). Start here. |
| [`guides/`](guides/) | How-to guides: [migrating a legacy ledger](guides/migrating-a-legacy-ledger.md), [the test harness](guides/test-harness.md). |
| [`records/`](records/) | Dated delivery records: [the zsqlab10 migration](records/zsqlab10-migration.md), [wave 10 (spec 4, 0.3.0)](records/wave10-delivery.md). |
| [`issues/`](issues/) | Open design for planned work: [`001-tm-upgrade`](issues/001-tm-upgrade.md), [`002-box-views`](issues/002-box-views.md) (planned spec 5). |

Conventions:

- Delivered work is **not** kept as tickets or design docs: its behaviour lives in `spec/`, its
  migration procedure in `guides/` + `migration/`, and a dated record in `records/` when real data
  was touched. Planned work lives in git-bug issues; while it is being designed, a `docs/issues/`
  doc holds the settled decisions.
- Chapter numbers are stable: "§3" (in a ticket, commit message or code comment) is
  `spec/03-physical-model.md`.
- Things that are executed rather than read — upgrade steps — live with their code under
  [`migration/`](../migration/README.md), one folder per counter bump, each with its own README.
