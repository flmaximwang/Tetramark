# 12 · CSV import/export
Depends on: 10  · Spec: §6, §10 (migration path)

- `ops/io_csv.py`: `ledger import KIND FILE.csv` — header = core columns (`id` optional explicit, `name`, `entity`,
  `parent_stock`, `notes`, aliases via `alias` column `;`-separated) + kind fields; for stock imports optional
  `box`,`slot`,`quantity`,`unit`,`status`,`aliquots`; for aliquot imports `stock`,`seq`, placement, status, quantity.
  Validate ALL rows first through the same code path as 10 (collect every error with row number), then write in one
  transaction + one commit, or nothing. `--dry-run` reports.
- `ledger export KIND|all [--out DIR]` — CSVs round-trippable through import (+ `boxes.csv`, `aliquots.csv`, `aliases.csv`).

Tests: round trip export → fresh ledger import → identical data; one bad row aborts all with row-numbered error;
explicit legacy ids (e.g. `0826FP02`) accepted.
