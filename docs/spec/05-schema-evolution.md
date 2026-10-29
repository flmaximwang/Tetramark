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
`migration/presets/<name>/<N>-to-<N+1>/`). Spec 5 (ticket 30) is the current format: one generated
view per **existing box** replaces the per-kind `v_box_<kind>` views, and an aliquot's stored
position follows its in-stock status (§3); base preset 4 renames the box kind `grid96` to
`grid8x12`. A spec-4 ledger at base 3 therefore runs **two** steps in this order:
`migration/spec/4-to-5/` first (it keeps the ledger's own base-3 copy, so its `grid96` boxes stay
readable, and creates the per-box views), then `migration/presets/base/3-to-4/` (it replaces that
copy and renames the kind). The spec step clears the positions of not-in-stock aliquots
(one `move` event per cleared row keeps the old box/slot in `from_box_id`/`from_slot`), rebuilds
every managed view and the derived `dolt_tests` in its one commit, and **refuses** — listing every
offending row and writing nothing — three shapes: a box name that cannot become a view name
(§3), a grid box that holds a child box, and an in-stock aliquot without a position (its README
lists the cleared rows, the upgrade order and the rollback). A spec-4 ledger still at base 2 — a
molbio-2 ledger, for example — follows the same shape one counter lower: `migration/spec/4-to-5/`
first, then `migration/presets/base/2-to-3/` and `migration/presets/molbio/2-to-3/` (and the
`cell_culture`/`library` steps the ledger still needs), with
`migration/presets/base/3-to-4/` **last**: the base v3 copies those steps install are what it
renames, and it must not run before them. The spec step's README states the preconditions, the
upgrade order, verification and rollback; the preset step's README covers the rename. Run both with
`--dry-run` first; on a real ledger write on a Dolt branch and merge only after review. The rule:
whoever bumps a counter adds exactly one folder in the same change. A step runs as `uv run migration/<…>/migrate.py --ledger PATH [--branch B] [--dry-run]`
and, through `src/tetramark/migrate.py`, refuses unless the ledger's counter equals `N`, creates the tag
`pre-migrate-<spec|preset>-<N>` before writing, prints every statement with `--dry-run`, and applies in
exactly one commit (`migrate(ledger): spec N -> N+1`) that bumps the counter, `tetramark_version` and
the fingerprint together. A step that turns a preset into a **bundle** declares the part counters it
introduces (`preset_versions_extra`), so `preset_versions` records every file the ledger now loads. A step whose target schema differs from the ledger's declares it (`schema=`),
so the commit writes the **new** fingerprint and regenerates the derived `dolt_tests` from that schema in
the same commit; a preset step then replaces `<ledger>/presets/` with the repository copy after the
commit (files are not Dolt content) and the runner re-reads the ledger from that copy and verifies the
fingerprint before reporting success. Dolt DDL is not transactional, so a failed step prints the last
good commit and the `dolt reset --hard` command. Preset steps may express renames in `renames.yaml` and
feed them to the diff planner as an explicit rename map instead of `renamed_from`; see
`migration/README.md`. A step also ships the preset content it installs: `presets/` only holds the current version, so the
version whose counter a step bumps (and any preset it keeps at the ledger's current version) is frozen
next to its `migrate.py` and placed with `vendor_preset_files`. A later bump therefore retrofits a
frozen copy into the earlier steps that place the whole preset closure — ticket 28's `cell_culture` 2
lives in `cell_culture/1-to-2/`, and `base/2-to-3` and `molbio/2-to-3` both ship its v1 copy. There is
no `tm` command for this yet — the older `tm upgrade` proposal (`docs/issues/001-tm-upgrade.md`) may
absorb `migration/` later.

A schema change that affects **reads only** — `lineage: true` on a `ref` field is the current example —
changes no stored shape, so it needs no counter bump and no migration step: replace the ledger's preset
copy as usual, and the now-stale `ledger_meta.fingerprint` is refreshed by `tm schema apply` (which has
no DDL steps to run). `migration/README.md` states the same rule.
