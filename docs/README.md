# Documentation

| Where | What it is |
|---|---|
| [`spec/`](spec/README.md) | **The specification** — how Tetramark works, one file per chapter (§1…§10). Start here. |
| [`guides/`](guides/) | How-to guides: [migrating a legacy ledger](guides/migrating-a-legacy-ledger.md), [the test harness](guides/test-harness.md). |
| [`records/`](records/) | Dated records of things done to real data: [the zsqlab10 migration](records/zsqlab10-migration.md). |
| [`issues/`](issues/) | Open design issues that are not yet tickets (e.g. `001-tm-upgrade`). |
| [`tickets/`](tickets/README.md) | **Open work only** — the current wave's tickets and its handover. |
| `preset-parts.md` | Design of the in-flight preset-parts wave (§ source of truth for tickets 25–29); moves under `spec/` when that wave lands. |

Conventions:

- Delivered work is **not** kept as tickets. Its behaviour lives in `spec/`, its migration procedure
  in `guides/` + `migration/`, and a dated record in `records/` when real data was touched.
- Chapter numbers are stable: "§3" (in a ticket, commit message or code comment) is
  `spec/03-physical-model.md`.
- Things that are executed rather than read — upgrade steps — live with their code under
  [`migration/`](../migration/README.md), one folder per counter bump, each with its own README.
