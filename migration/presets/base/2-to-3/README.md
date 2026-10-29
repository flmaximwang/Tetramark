# base 2 → 3 — the slotless box kind `incubator`

Counter: `ledger_meta.preset_versions.base` 2 → 3. Tag before writing: `pre-migrate-base-2`.
One commit: `migrate(ledger): base 2 -> 3`.

## What changes

Design §3 (ticket 27) gives `base` one more box kind: `incubator`, slotless, because fly, worm and
cell-culture material all sit in one. Base preset version 3 is otherwise version 2 (status sets
`tube`/`cryo`/`animal`, the `item`/`lot` kinds and the existing box geometries).

The step applies the **additive** diff between the ledger's live database and its own (just placed)
v3 schema: the new `box_incubator` table and the generated views in their current (spec 5) shape —
box kinds have no `v_box_<kind>` view any more; each existing box gets a `v_box_<box name>` view. It
refuses a destructive plan (it never drops or rewrites an existing column)
before anything is written. Then it bumps `preset_versions.base` + the fingerprint, and the shared
runner regenerates the derived `dolt_tests` from the same schema in that commit.

Since spec 5 (ticket 30) the repository's `presets/base.yaml` is **version 4** (the `grid96` →
`grid8x12` rename). This step therefore installs the **frozen [`base-3.yaml`](base-3.yaml)** shipped
next to `migrate.py`, not the repository copy: it must place the v3 content whose counter it bumps.
That is the retrofit rule in [`migration/README.md`](../../../README.md) — a later bump freezes the
old target copy into every earlier step that places the whole preset closure.

## Preconditions

- `ledger_meta.spec_version = 5`. Run [`migration/spec/4-to-5`](../../../spec/4-to-5/README.md) first
  (the step refuses a spec-4 ledger). The wave-11 order is `spec/4-to-5`, then the preset steps, then
  [`migration/presets/base/3-to-4`](../3-to-4/README.md) last.
- `ledger_meta.preset_versions.base = 2` (a second run refuses; the shared runner checks it).
- A clean working set.
- Files, not Dolt content: the step places **every preset copy the ledger lists** from this checkout
  before it opens the ledger (the spec-5 loader needs current copies for every stock kind's
  `statuses:`), with **two frozen exceptions**: `base` is held at
  [`base-3.yaml`](base-3.yaml) (the repository copy is v4 since spec 5) and `cell_culture` at
  `cell_culture-1.yaml` (ticket 28 bumped the repository copy to v2). A ledger that lists `molbio`
  therefore also gets molbio v3 and its four parts; its counter is bumped by
  [`migration/presets/molbio/2-to-3`](../../molbio/2-to-3/README.md). The order of the two steps does
  not matter: whichever runs first may already create the other's new tables (`box_incubator`, the
  oligo tables), and the second then finds only its counter to bump.
- `cell_culture` is held at the **frozen `cell_culture-1.yaml` next to `migrate.py`**: ticket 28
  bumps the repository copy to v2, and this step must not install a later preset version behind its
  own counter's back (the ledger would then load v2 content while `preset_versions.cell_culture`
  says 1, and [`migration/presets/cell_culture/1-to-2`](../../cell_culture/1-to-2/README.md) could
  not run afterwards). Run the cell_culture step **after** this one on a ledger that still carries
  cell_culture 1.
- A directory copy `<ledger>/presets/base/` would shadow the flat `base.yaml` this step places;
  convert it to a file first (as for every wave-10 preset step).

## Running it

```sh
uv run migration/presets/base/2-to-3/migrate.py --ledger /path/to/ledger --dry-run
uv run migration/presets/base/2-to-3/migrate.py --ledger /path/to/ledger
uv run migration/presets/base/2-to-3/migrate.py --ledger /path/to/ledger --branch tetramark-upgrade
```

`--dry-run` (and a refusal) restores the preset copies it placed, so the ledger stays
byte-identical; the printed plan is what a real apply would execute.

## Manual steps

None for the new kind. A ledger's own `kinds/` file that redefines an `incubator` box kind must be
removed first (the loader rejects a duplicate kind). Find leftovers — expected output is empty:

```sh
LEDGER=/path/to/ledger
grep -rn "incubator" "$LEDGER/kinds" "$LEDGER/rules"
```

## Verification

```sh
LEDGER=/path/to/ledger
dolt sql -q "SELECT \`value\` FROM \`ledger_meta\` WHERE \`key\` = 'preset_versions'"  # base: 3, ...
dolt log -n 1                                                                        # migrate(ledger): base 2 -> 3
dolt tag | grep pre-migrate-base-2
dolt sql -q "SHOW TABLES LIKE 'box_incubator'"
uv run tm -w /path/to/workspace -l <ledger> check                                    # OK: ... passed
```

Expected: `box_incubator` exists (no `v_box_incubator` since spec 5; every existing box has its own
`v_box_<box name>` view instead), no `FAIL`, and `tm schema plan` reports no pending steps.

## Failure, rollback, caveats

- **A destructive plan is a refusal, not a failure.** Nothing is written: no tag, no commit, and the
  placed preset copies are restored. Fix the ledger's local schema and retry.
- **Dolt DDL is not transactional.** If a statement fails mid-way, the statements already executed
  stay in the working set; the script makes **no** commit and prints the last good commit plus
  `dolt reset --hard <last-good-commit>`. The pre-migration tag stays as the restore point.
- **Rollback after success:** `dolt reset --hard pre-migrate-base-2` and copy the previous
  `base.yaml` (version 2, shipped as
  [`base-2.yaml`](../1-to-2/base-2.yaml) in the base 1 → 2 step) back into `<ledger>/presets/`;
  preset copies are files, not Dolt content. This step's own [`base-3.yaml`](base-3.yaml) is the
  base copy it placed and `cell_culture-1.yaml` the cell_culture copy it kept, if those need
  restoring too.
