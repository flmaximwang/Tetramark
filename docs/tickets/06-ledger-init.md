# 06 · Ledger init & open
Depends on: 02, 04, 05  · Spec: §1.1, §3 (ledger_meta), §6

- `core/ledger.py`: `init_ledger(workspace, name, presets, id_prefix, ...)` → mkdir `<data_dir>/<name>`,
  `dolt init`, write `ledger.yaml`, load schema, run DDL in one transaction, write `ledger_meta`
  (fingerprint, preset versions, ledger.yaml text, spec_version=1), Dolt commit `init(ledger): <name>`.
  `open(workspace=None, ledger=None)` → `Ledger` (driver, config, schema, id strategies, author resolution
  ledger.yaml → workspace.yaml → git config). Warn (not fail) if live fingerprint ≠ YAML fingerprint
  ("run ledger schema plan").
- `Ledger.write(summary)` context manager: one transaction + one Dolt commit (ASCII `<verb>(<object>): <summary>`),
  honouring `dry_run` (collect & return SQL, execute nothing) and `no_commit`.
- CLI: `ledger init NAME --preset … [--id-prefix]`, `ledger presets list|show`, `ledger schema show [KIND]`.
- Test fixture `ledger_factory(presets=[...])` in conftest (workspace in tmp_path, EmbeddedDriver).

Tests: init with the 03 test presets creates DB + commit; reopen; dry-run executes nothing; fingerprint warning.
