# 5. Schema evolution
`tm schema plan` diffs the effective YAML schema against the live DB (introspection via
`information_schema` + `ledger_meta`) and prints an ordered SQL plan, each step tagged
`additive` or `destructive`. `tm schema apply` executes it in one transaction + one Dolt commit.

- Additive (applied freely): new kind, new nullable/defaulted field, new enum value, new view/rule/test,
  new kind table, relaxing NOT NULL.
- Destructive (refused without `--allow-destructive`): drop/rename kind or field, type change, tightening
  (new NOT NULL without default, new UNIQUE, removing enum value). Renames are expressed in YAML as
  `renamed_from: old_name` so they become `ALTER … RENAME` instead of drop+add.
- Generated columns and FKs that depend on a changed column are dropped and rebuilt automatically
  (lesson from zsqlab10's slot-column relaxation).
- After apply, the plan must be empty (idempotency is a test).

`tm schema plan/apply` evolves a ledger within one spec version. When the **format** itself changes —
`SPEC_VERSION` or a preset's `version:` is bumped — the step is a hand-run script under `migration/`,
one folder per counter that changed (`migration/spec/<N>-to-<N+1>/`,
`migration/presets/<name>/<N>-to-<N+1>/`). The rule: whoever bumps a counter adds exactly one folder in
the same change. A step runs as `uv run migration/<…>/migrate.py --ledger PATH [--branch B] [--dry-run]`
and, through `src/tetramark/migrate.py`, refuses unless the ledger's counter equals `N`, creates the tag
`pre-migrate-<spec|preset>-<N>` before writing, prints every statement with `--dry-run`, and applies in
exactly one commit (`migrate(ledger): spec N -> N+1`) that bumps the counter, `tetramark_version` and
the fingerprint together. A step whose target schema differs from the ledger's declares it (`schema=`),
so the commit writes the **new** fingerprint and regenerates the derived `dolt_tests` from that schema in
the same commit; a preset step then replaces `<ledger>/presets/` with the repository copy after the
commit (files are not Dolt content) and the runner re-reads the ledger from that copy and verifies the
fingerprint before reporting success. Dolt DDL is not transactional, so a failed step prints the last
good commit and the `dolt reset --hard` command. Preset steps may express renames in `renames.yaml` and
feed them to the diff planner as an explicit rename map instead of `renamed_from`; see
`migration/README.md`. There is
no `tm` command for this yet — the older `tm upgrade` proposal (`docs/issues/001-tm-upgrade.md`) may
absorb `migration/` later.

A schema change that affects **reads only** — `lineage: true` on a `ref` field is the current example —
changes no stored shape, so it needs no counter bump and no migration step: replace the ledger's preset
copy as usual, and the now-stale `ledger_meta.fingerprint` is refreshed by `tm schema apply` (which has
no DDL steps to run). `migration/README.md` states the same rule.

---
