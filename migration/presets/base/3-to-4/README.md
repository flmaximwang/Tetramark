# base 3 → 4 — the box kind `grid96` becomes `grid8x12`

Counter: `ledger_meta.preset_versions.base` 3 → 4. Tag before writing: `pre-migrate-base-3`.
One commit: `migrate(ledger): base 3 -> 4`.

## What changes

Spec 5 (ticket 30, `docs/issues/002-box-views.md`) gives every **existing box** its own generated
view `v_box_<box name>` and drops the per-kind `v_box_<kind>` views. The box kind no longer needs a
name that matches a view, so base preset version 4 renames `grid96` to `grid8x12`; the geometry is
unchanged (rows `A-H`, columns `1-12`, 96 slots) and nothing else in the preset changes.

The step applies the diff between the ledger's live database and its own (just placed) v4 schema
using the explicit rename map [`renames.yaml`](renames.yaml) (`box: {grid96: grid8x12}`): the
planner renames the `box_grid96` table to `box_grid8x12`, rewrites the core `kind` values in `box`,
translates the constraints, drops the old `v_box_grid96` view, and recreates the generated views in
the spec-5 shape (one view per live box plus the core/entity/stock views). It refuses a plan with any
**other** destructive step before anything is written. Then it bumps `preset_versions.base` and the
fingerprint, and the shared runner regenerates the derived `dolt_tests` from the same schema in that
commit.

## Preconditions

- `ledger_meta.spec_version = 5`. Run [`migration/spec/4-to-5`](../../../spec/4-to-5/README.md)
  **first** (this step refuses a spec-4 ledger). That step creates the per-box views while the ledger
  still carries its own base-v3 copy, so the `grid96` boxes are visible to it; it also lists any
  aliquot positions it cleared.
- `ledger_meta.preset_versions.base = 3` (a second run refuses; the shared runner checks it).
- **This is the last preset step.** The wave-11 upgrade order is `migration/spec/4-to-5` first, then
  the older preset steps the ledger still needs — `migration/presets/base/2-to-3` and
  `migration/presets/molbio/2-to-3` (and `cell_culture/1-to-2` / `library/1-to-2`) — and
  `migration/presets/base/3-to-4` last. Those steps now install frozen base-v3 copies (the retrofit
  is the maintenance rule in [`migration/README.md`](../../../README.md)); this step renames that
  content. Running it before them would make them place v3 over a v4 ledger and refuse.
- A clean working set.
- Files, not Dolt content: the step places **every preset copy the ledger lists** from this checkout
  before it opens the ledger. Its own `base.yaml` is placed from the frozen [`base-4.yaml`](base-4.yaml)
  next to `migrate.py`, not from `presets/`: the repository only holds the current version, and a
  later bump must not change what this historical step installs. The other presets' counters are
  bumped by their own steps (they are independent and may already have run).
- A directory copy `<ledger>/presets/base/` would shadow the flat `base.yaml` this step places;
  convert it to a file first (as for every wave-10 preset step).
- [`base-3.yaml`](base-3.yaml) next to `migrate.py` is the content a base-3 ledger carries: it is
  the rollback copy and the fixture shape for tests, since `presets/` no longer ships version 3.

## Running it

```sh
uv run migration/presets/base/3-to-4/migrate.py --ledger /path/to/ledger --dry-run
uv run migration/presets/base/3-to-4/migrate.py --ledger /path/to/ledger
uv run migration/presets/base/3-to-4/migrate.py --ledger /path/to/ledger --branch tetramark-upgrade
```

`--dry-run` (and a refusal) restores the preset copies it placed, so the ledger stays byte-identical;
the printed plan is what a real apply would execute.

## Manual steps

None for the rename. A ledger's own `kinds/` file that redefines a `grid96` box kind must be removed
first (after the rename the loader would see both the local old kind and the preset's new one). Find
leftovers — expected output is empty:

```sh
LEDGER=/path/to/ledger
grep -rn "grid96" "$LEDGER/kinds" "$LEDGER/rules"
```

## Verification

```sh
LEDGER=/path/to/ledger
dolt sql -q "SELECT \`value\` FROM \`ledger_meta\` WHERE \`key\` = 'preset_versions'"  # base: 4, ...
dolt log -n 1                                                                        # migrate(ledger): base 3 -> 4
dolt tag | grep pre-migrate-base-3
dolt sql -q "SHOW TABLES LIKE 'box_grid8x12'"
dolt sql -q "SHOW TABLES LIKE 'box_grid96'"                                          # empty
dolt sql -q "SHOW FULL TABLES WHERE Table_type = 'VIEW'"
uv run tm -w /path/to/workspace -l <ledger> check                                    # OK: ... passed
uv run tm -w /path/to/workspace -l <ledger> schema plan                              # no pending steps
```

Expected: `box_grid8x12` exists and `box_grid96` is gone, `preset_versions.base` is 4, every box has
its own `v_box_<box name>` view with no `v_box_grid96`, `tm check` prints no `FAIL`, and `tm schema
plan` reports no pending steps.

## Failure, rollback, caveats

- **A destructive plan is a refusal, not a failure.** Nothing is written: no tag, no commit, and the
  placed preset copies are restored. Fix the ledger's local schema and retry.
- **Dolt DDL is not transactional.** If a statement fails mid-way, the statements already executed
  stay in the working set; the script makes **no** commit and prints the last good commit plus
  `dolt reset --hard <last-good-commit>`. The pre-migration tag stays as the restore point.
- **Rollback after success:** `dolt reset --hard pre-migrate-base-3` and copy `base-3.yaml` back over
  `<ledger>/presets/base.yaml`; preset copies are files, not Dolt content.
- **Commit made but the copy update failed** (only when a step's post-commit file hook is used;
  this step places the copies before opening): the report names the commit — put `base-4.yaml` at
  `<ledger>/presets/base.yaml` by hand and reopen the ledger.
