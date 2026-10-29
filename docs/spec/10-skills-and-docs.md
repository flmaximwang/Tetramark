# 10. Skills & docs

- `skills/tetramark` — operating manual: concepts, find/query recipes, add/move/use/status flows, check,
  history, what not to do (raw SQL writes, hand-editing dolt_tests). Chinese trigger words in description
  (台账, 样品, 盒, 冻存, 库存, 管).
- `skills/tetramark-schema` — writing presets/kinds/rules, schema plan/apply, destructive changes.
- [Migrating a legacy ledger](../guides/migrating-a-legacy-ledger.md) — rulebook for moving an old zsqlab DB: map old tables → kinds, keep old UIDs as
  explicit IDs (or aliases when they don't fit), `box/row/col` → box name + slot label, `status` 三态 →
  status vocab, pellet aliquots → aliquots with quantity, export old tables to CSV → `tm import`,
  checklist (row counts per kind, `tm check` all PASS, spot-check `box show` vs old grid views).

Documentation entry points: [the spec](../spec/README.md) (this document), [guides](../guides/),
[records](../records/) of deliveries and real migrations, and open [design issues](../issues/) for
planned work.
