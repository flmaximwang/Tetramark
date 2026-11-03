# 4. Validation

`tm check` runs Dolt's `dolt_tests` (same source as Workbench's Tests page). Exit 0 = all PASS,
1 = any FAIL. `-v` prints offending rows (each generated test has a companion detail query).

Tests are generated into `dolt_tests` on `init` / `adopt` / `schema apply`, grouped:
- `core` — the app-enforced invariants in §3, including `core_aliquot_status_known` (every aliquot's
  status is in its stock kind's set) and `core_status_event_allowed` (every unforced `status` event
  follows its stock kind's `to:` rules; `--force` events are exempt).
- `schema:<kind>` — patterns, required refs, entity_kinds restriction.
- `rules:<preset|local>` — custom rules. `severity: info` tests are reported but never fail the exit code
  (stored with a group suffix `:info`). Preset rules are read from the ledger's own
  `<ledger>/presets/` copy (§8); there is no fallback to a source directory outside the ledger.
- `portability:info` — one informational rule (P13) when a configured remote or backup is an
  absolute `file://` URL that points outside the ledger directory. The message names the offenders;
  the rule never changes the exit code. It is how a copied/moved ledger reports the URLs that still
  point at the old machine.

Generated tests are fully replaced on each apply (they are derived state); never hand-edit them.
