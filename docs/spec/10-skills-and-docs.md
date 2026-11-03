# 10. Skills & docs

- `skills/tetramark` — operating manual: concepts, find/query recipes, add/move/use/status flows, check,
  history, what not to do (raw SQL writes, hand-editing dolt_tests). Chinese trigger words in description
  (台账, 样品, 盒, 冻存, 库存, 管).
- `skills/tetramark-schema` — writing presets/kinds/rules, schema plan/apply, destructive changes.
- [Migrating a legacy ledger](../guides/migrating-a-legacy-ledger.md) — rulebook for moving an old zsqlab DB: map old tables → kinds, keep old UIDs as
  explicit IDs (or aliases when they don't fit), `box/row/col` → box name + slot label, `status` 三态 →
  status vocab, pellet aliquots → aliquots with quantity, export old tables to CSV → `tm import`,
  checklist (row counts per kind, `tm check` all PASS, spot-check `box show` vs old grid views).
- [Upgrading 0.4.2 → 0.5.0](../guides/upgrading-0.4-to-0.5.md) — the workspace removal in practice:
  `cd` into the ledger directory, the P4 stale-`workspace.yaml` pointer, author and backup changes.
- [Upgrading 0.5.0 → 0.5.1](../guides/upgrading-0.5.0-to-0.5.1.md) — the full-word subcommands: `ls` → `list`,
  `rm` → `remove`, `tm backup --ls` → `tm backup --list`. A hard break, CLI-only, no ledger step.
- [Test harness](../guides/test-harness.md) — embedded-only cost model, session ledger templates, the
  fast/full lanes and the wall-time budget enforced by [`scripts/test-lanes.sh`](../../scripts/test-lanes.sh).

Documentation entry points: [the spec](../spec/README.md) (this document), [guides](../guides/),
[records](../records/) of deliveries and real migrations, and open [design issues](../issues/) for
planned work.
