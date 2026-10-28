# 4. Validation
`tm check` runs Dolt's `dolt_tests` (same source as Workbench's Tests page). Exit 0 = all PASS,
1 = any FAIL. `-v` prints offending rows (each generated test has a companion detail query).

Tests are generated into `dolt_tests` on `init` / `init --adopt` / `schema apply`, grouped:
- `core` — the app-enforced invariants in §3.
- `schema:<kind>` — patterns, required refs, entity_kinds restriction.
- `rules:<preset|local>` — custom rules. `severity: info` tests are reported but never fail the exit code
  (stored with a group suffix `:info`). Preset rules are read from the ledger's own
  `<ledger>/presets/` copy (§8), never from the workspace `presets_dir`.

Generated tests are fully replaced on each apply (they are derived state); never hand-edit them.

---
