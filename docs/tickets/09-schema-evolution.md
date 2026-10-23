# 09 · Schema plan/apply
Depends on: 06 (and 08's test installer if merged; otherwise leave a hook)  · Spec: §5

- `schema/diff.py`: introspect live DB (`information_schema.columns/table_constraints/key_column_usage/views`,
  `ledger_meta`) → compare with DDL objects from 05 → ordered plan steps tagged additive|destructive with SQL.
  Handle: new kind/field/enum value/view/aliquot table, relaxing NOT NULL, `renamed_from` renames, drops,
  type changes, tightening; dependent generated columns/FKs dropped and rebuilt around a changed column.
- CLI `ledger schema plan` (human + `--json`), `ledger schema apply [--allow-destructive]` → one commit,
  re-installs generated dolt_tests, updates `ledger_meta` fingerprint. Refuse destructive without flag (exit 1, list steps).

Tests: for each change class, mutate the YAML of an inited ledger with data → plan shows expected tags → apply →
plan is empty → data preserved (renames keep values).
