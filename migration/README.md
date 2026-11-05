# Migrations

## The maintenance rule

**Whoever bumps `SPEC_VERSION` (in `src/tetramark/schema/model.py`) or a
preset's `version:` adds exactly one folder for that step, in the same change.**
No release may ship a bumped counter without its step folder here.

A step folder is **scripted** (it holds a `migrate.py`) or **manual** (it is
README-only). Both kinds have a `README.md`; the folder name and the rule are
the same either way.

A change that affects **only reads** is the one case that bumps no counter, so
it gets no step. The current example is `lineage: true` on a `ref` field
(`tm lineage`): it changes no stored shape, but it *is* part of the schema
fingerprint. Replacing a ledger's preset copy makes `ledger_meta.fingerprint`
stale, and `tm schema apply` refreshes it — that is a meta refresh, not a
migration step. If you cannot describe a change with "no DDL and no stored
values move", it needs a counter and a folder.

## What this directory is

Hand-run upgrade steps for ledgers whose **format** changed. One folder per
counter bump, named after the counter that changed — never after the Tetramark
release.

```
migration/
  README.md
  spec/<N>-to-<N+1>/            README.md, migrate.py (or README only)
  presets/<name>/<N>-to-<N+1>/  README.md, migrate.py (or README only),
                                renames.yaml (optional)
```

Every ledger records the counters a step dispatches on in `ledger_meta`:

- `spec_version` — the core ledger format (`SPEC_VERSION` in
  `src/tetramark/schema/model.py`), bumped only when the stored shape changes
  (core tables, `ledger_meta` keys, generated views/`dolt_tests`);
- `preset_versions` — a JSON object, e.g. `{"base": 1, "molbio": 1}`.

`tetramark_version` and `created_with` are provenance only; they never decide a
step.

## Running a step

Steps are run by hand, one at a time, from the repository root:

```sh
uv run migration/<…>/migrate.py --ledger PATH [--branch B] [--dry-run]
```

`PATH` is the **ledger directory** (the Dolt database, e.g.
`…/Dolt/zsqlab_10_gammaPFD-Fiber_2026.06.25`), not the workspace data directory.
When its parent has a `workspace.yaml`, the step uses the running
`dolt sql-server` if there is one, otherwise the embedded driver; a bare Dolt
directory is always opened embedded. `--branch` selects a session branch
(`db/branch`); the checked-out branch is never moved.

There is deliberately no `tm` command for this. The `tm upgrade` design in
`docs/issues/001-tm-upgrade.md` stays a separate proposal; it can absorb
`migration/` later.

### Manual (README-only) steps

A step that cannot be scripted holds **no `migrate.py`**: its folder has a
`README.md` (and, at most, a frozen preset copy) and no script. The README
states the same things a scripted step's README states — what changes, the
preconditions and the order, the SQL or the file edits, the verification and
the rollback — and the operator does it by hand.

A manual step is the right kind when the change touches the ledger's **files**
(the preset copies under `<ledger>/presets/`, a local `kinds/`) as much as its
rows, or when the rows need a judgement call (a vocabulary remap an operator
has to review). The current example is **spec 5 → 6** (the fixed aliquot
status set): the preset copies must be replaced, `aliquot.status` remapped, the
`migrated:` events written and the `CHECK` added — see
[`spec/5-to-6`](spec/5-to-6/README.md). Its preset steps
([`presets/base/4-to-5`](presets/base/4-to-5/README.md) and the other twelve)
are manual too: they only replace the ledger's stored copy, and the counter is
written by the spec step's `tm schema apply`.

A manual step has no tag and no `migrate(ledger): …` commit of its own: the
operator's own commits carry the work, and the spec step's `tm schema apply`
writes the counters, the fingerprint and the regenerated `dolt_tests` in one
commit.

### A step on a ledger that is already past it

A scripted step refuses a ledger whose counter is not the one it expects —
nothing is written. When the ledger's counter is **newer** than the step's
`from_version`, the refusal names the **next** step's README instead of only
the mismatch, so the operator is pointed at the procedure that continues the
upgrade (for example `migration/spec/4-to-5/migrate.py` on a spec-5 ledger
names `migration/spec/5-to-6/README.md`). The pointer is only added when that
next folder exists and is README-only; a scripted next step keeps the plain
counter message.

## What every step guarantees

`src/tetramark/migrate.py` implements this contract once; a step's `migrate.py`
only declares its counter and its SQL:

1. open the ledger, read the counter, and **refuse** unless it equals `N`;
2. refuse a dirty working set — the migration must be the only thing in its
   commit;
3. create the tag `pre-migrate-<spec|preset>-<N>` **before writing**;
4. `--dry-run` prints every SQL statement (tag, step SQL, `ledger_meta` bump)
   and writes nothing — Dolt HEAD stays where it was;
5. apply → **exactly one** Dolt commit, message
   `migrate(ledger): spec N -> N+1` or `migrate(ledger): <preset> N -> N+1`.
   The counter, `tetramark_version` and the fingerprint are bumped in that same
   commit, and the commit message is verified afterwards. A step that declares
   its **target schema** (`schema=`) records *that* schema's fingerprint and
   regenerates the derived `dolt_tests` from it in the same commit, so no stale
   test names an old table after the step;
6. Dolt DDL is **not transactional**. If a statement fails, the step prints the
   last good commit and the restore command (`dolt reset --hard <hash>`), makes
   **no** commit, and exits 1. The pre-migration tag stays as the restore
   point.

A second run on an already-migrated ledger refuses: the counter is `N+1`, not
`N`.

A preset step also updates files, which are **not** Dolt content: `<ledger>/presets/`
is the ledger's own preset copy (0.2.0) and the target copy must replace it, or
the next `open` loads the old preset while `preset_versions` says `N+1`.
The wave-10 preset steps place the copies **before** the runner opens the ledger
(`vendor_repo_presets`, because the spec-4 loader refuses a stock kind without
`statuses:`), or pass `after_commit=` when the step has to open the old ledger
first (the molbio 1 -> 2 renames); the runner runs an `after_commit` hook **only
on a real apply**, after the commit, describes it in `--dry-run` via
`after_commit_note=`, and then re-reads the ledger from its new copy
(`reopened_schema=`) and verifies the fingerprint before reporting success. If
the file hook fails, the report says `commit <hash> was made but the preset copy
update failed` and exits 1 — the Dolt side is already migrated and only the files
need the manual fix from the step README.

Two rules keep historical steps usable after a later release bumps a preset
again:

- **A step ships the preset content it installs.** `presets/` only holds the
  current version, so a step that upgrades to version `N+1` keeps that version's
  YAML next to its `migrate.py` and places it with `vendor_preset_files` (see
  `migration/presets/base/1-to-2/base-2.yaml`). Otherwise the step would try to
  install the current, newer file and refuse its own counter.
- **A later bump retrofits the older steps.** When a preset gets a new version,
  every earlier step that places the whole current closure keeps the *content it
  knows* by shipping a frozen copy too, or it would install the newer file
  behind that preset's own counter and the newer step could then not run. Ticket
  28's `cell_culture` 2 is the worked example: `base/2-to-3` and `molbio/2-to-3`
  both ship and place `cell_culture-1.yaml`. Ticket 30's **base 4** (`grid96` →
  `grid8x12`, spec 5) added `base-3.yaml` to `base/2-to-3`, `molbio/2-to-3`,
  `cell_culture/1-to-2` and `library/1-to-2`, and `base/3-to-4` renames that
  content. Ticket rq9's **proteins 2 / nucleic_acids 2** (the IUPAC sequence
  patterns) is the current example: the same five wave-10 steps now also ship
  and place `proteins-1.yaml` and `nucleic_acids-1.yaml`, and the two new steps
  ship their own target copies (`proteins-2.yaml`, `nucleic_acids-2.yaml`, plus
  `nucleic_acids-1.yaml` in the proteins step because that part's counter stays
  at 1 there). On a wave-12 upgrade the two pattern steps run **after**
  `base/3-to-4` (they need base 4) and in the order proteins → nucleic_acids.
  Ticket `Tetramark-3ht`'s **proteins 4** (a construct's `sequence` is amino
  acids) is the **manual** example: it retrofits the README-only
  `proteins/2-to-3` step with a frozen `proteins-3.yaml`, because that step
  installs the copy of its checkout and would otherwise place v4 behind its own
  counter.
- **A bundle step records its part counters.** When a preset becomes a bundle
  (`include:` only), its step passes `preset_versions_extra={part: 1, ...}` so
  the one commit also records the version of every file the ledger now loads.
- **A manual preset step installs the copy of its checkout.** Its README names
  the version it upgrades to (`version: 5`), so a later bump of that preset
  must update the older manual step as well — the same retrofit the scripted
  steps need (the frozen copy next to the step).

## Writing a step

`README.md` in the step folder must state:

- **what changes** and why (which release/commit);
- **preconditions** (counter value, tool version, backup/branch rules);
- **manual steps** — anything the script cannot do (e.g. hand-written rule SQL
  in a ledger's `rules/` is not rewritten);
- a **`grep` command** that finds leftovers in a ledger's `kinds/` and
  `rules/` (or the `kinds_dir`/`rules_dir` named in its `ledger.yaml`), with the
  expected (usually empty) output;
- **verification**: `tm check` with no `FAIL`, plus the counters/tags/commit to
  look at.

A **README-only** step states the same list and adds the exact procedure: the
files to replace, the SQL to run (in the order it must run), how the counter
is written, and how to roll back. It has no `migrate.py`, no tag and no commit
of its own.

`migrate.py` is short; it imports `MigrationStep`/`main` from
`tetramark.migrate` and builds its SQL from the schema helpers (never from
hard-coded DDL where a generator exists). A step whose new schema differs from
the ledger's current one loads the repository preset itself and passes it as
both `schema=` (fingerprint + regenerated `dolt_tests`) and, through a small
`reopened_schema=` wrapper around `tetramark.schema.loader.load_ledger_schema`,
for the post-commit verification; the step's `plan` calls the same loader to
build the diff. Use the `Ledger`/`Driver` API
(`driver.execute`/`query`/`commit`, `Ledger.write`) — **no direct `dolt`
shelling**: the step must also run against a ledger served by
`dolt sql-server`.

### Renames: `renames.yaml`

A preset step that renames kinds or columns may ship `renames.yaml` instead of
editing the preset with `renamed_from` (presets do not carry `renamed_from`):

```yaml
entity: {construct: protein_construct}
stock:  {bacterial: bacterial_glycerol}
fields: {stock.bacterial_glycerol: {old_col: new_col}}   # optional
```

Keys of `entity`/`stock`/`box` are **old** kind names, values the **new** kind
names of the loaded schema. `fields` keys are `<object>.<new kind>` (e.g.
`stock.bacterial_glycerol`). Load the file with
`tetramark.migrate.load_renames(path)` and pass it to the planner:

```python
from tetramark.migrate import load_renames
from tetramark.schema.diff import plan

renames = load_renames(Path(__file__).with_name("renames.yaml"))
schema_plan = plan(ledger, renames=renames)   # or build_plan(driver, schema, ...)
```

The planner reuses the `renamed_from` machinery: it renames the kind table and
columns, rewrites the core `kind` discriminator values, and translates
constraints/views — no hand-written SQL, no data loss.

## Existing steps

| Step | Counter | What it does |
|---|---|---|
| [`spec/1-to-2`](spec/1-to-2/README.md) | `spec_version` 1 → 2 | Recreates the generated views in the spec-2 shape (`alias_list`/`occupant_alias_list`). |
| [`spec/2-to-3`](spec/2-to-3/README.md) | `spec_version` 2 → 3 | Drops the empty `aliquot_<stockkind>` extension tables (refusing if one still holds values), recreates the views with the stock entity/aliquot/quantity/status columns, and regenerates `dolt_tests`. |
| [`spec/3-to-4`](spec/3-to-4/README.md) | `spec_version` 3 → 4 | Drops the global `aliquot.status` CHECK, adds `aliquot_event.forced`, recreates the in-stock views, removes the default `statuses:` list from `ledger.yaml`/`ledger_meta` (refusing a custom list), and regenerates `dolt_tests` including the two status rules. |
| [`spec/4-to-5`](spec/4-to-5/README.md) | `spec_version` 4 → 5 | Drops the per-box-kind `v_box_<kind>` views and creates one view per existing box, clears the positions of not-in-stock aliquots (one `move` event each), rebuilds the derived views/`dolt_tests`; refuses a view-unsafe box name, a grid box holding a child box, and an in-stock aliquot without a position. Run before `presets/base/3-to-4`. |
| [`presets/base/1-to-2`](presets/base/1-to-2/README.md) | `preset_versions.base` 1 → 2 | Places the frozen v2 base copy (status sets `tube`/`cryo`/`animal`, `lot` uses `tube`), recreates the generated views; no table changes. |
| [`presets/base/2-to-3`](presets/base/2-to-3/README.md) | `preset_versions.base` 2 → 3 | Places the frozen base v3 copy and applies its additive diff: the slotless `incubator` box kind (`box_incubator` table); refuses a destructive plan. |
| [`presets/base/3-to-4`](presets/base/3-to-4/README.md) | `preset_versions.base` 3 → 4 | Places the frozen v4 base copy and applies the explicit rename map (`renames.yaml`): box kind `grid96` → `grid8x12` (table, constraints, core `kind` values); the per-box views spec 4-to-5 built stay as they are. Refuses any other destructive step. Run after `spec/4-to-5` and after the wave-10 preset steps; the ticket-rq9 `proteins`/`nucleic_acids` steps run after it. |
| [`presets/library/1-to-2`](presets/library/1-to-2/README.md) | `preset_versions.library` 1 → 2 | Places the v2 library copy (`copy` uses the base `tube` set), recreates the generated views; no table changes. |
| [`presets/molbio/1-to-2`](presets/molbio/1-to-2/README.md) | `preset_versions.molbio` 1 → 2 | Renames four molbio kinds, adds `cell_line`/`cell_vial`/`cell_pellet`, moves `glycerol_pct` from the aliquot extension to the stock, vendors the frozen v2 preset copy. |
| [`presets/molbio/2-to-3`](presets/molbio/2-to-3/README.md) | `preset_versions.molbio` 2 → 3 | Turns molbio into a bundle of parts, adds `entity_oligo`/`stock_oligo_stock` (and base v3's `box_incubator`), places every part copy (cell_culture frozen at v1 by ticket 28) and records a `preset_versions` key per part at 1. |
| [`presets/cell_culture/1-to-2`](presets/cell_culture/1-to-2/README.md) | `preset_versions.cell_culture` 1 → 2 | Places the frozen v2 cell_culture copy and applies its additive diff: three `stock_cell_vial` columns (`selection`, `mycoplasma`, `mycoplasma_tested_on`) and the `cell_biosample` kind/table; one commit, additive only. |
| [`presets/proteins/1-to-2`](presets/proteins/1-to-2/README.md) | `preset_versions.proteins` 1 → 2 | Places the frozen proteins v2 copy (IUPAC patterns on the shared `sequence_fields` sequence and `mature_sequence`) and the frozen nucleic_acids v1 copy, recreates the generated views; no table changes. `tm check` reports rows outside the alphabet. Runs after `base/3-to-4`, before `nucleic_acids/1-to-2`. |
| [`presets/nucleic_acids/1-to-2`](presets/nucleic_acids/1-to-2/README.md) | `preset_versions.nucleic_acids` 1 → 2 | Places the frozen nucleic_acids v2 copy (IUPAC pattern on `oligo.sequence`), recreates the generated views; no table changes. Requires proteins 2 and runs after `proteins/1-to-2`. |
| [`spec/5-to-6`](spec/5-to-6/README.md) | `spec_version` 5 → 6 | **Manual** (README only, no `migrate.py`): the fixed aliquot status set. Replaces the ledger's preset copies with the status-free ones, remaps `aliquot.status` (the old → new table), records one `migrated: <old status>` event per remapped aliquot, adds the `ck_aliquot_status` `CHECK` and lets `tm schema apply` write `spec_version`, `preset_versions`, the fingerprint and the regenerated `dolt_tests` in one commit. Past events are never rewritten; no stock property is backfilled. Run the pending historical preset steps first (they target spec 5). |
| [`spec/6-to-7`](spec/6-to-7/README.md) | `spec_version` 6 → 7 | Renames `aliquot_event.note` to `message` (`RENAME COLUMN`), folds each non-empty `purpose` into the message and drops `purpose`, then regenerates `dolt_tests`; refuses an ambiguous/damaged column set without writing. **Dry run by default, `--apply` to write** — the only step with that flag. |
| [`presets/base/4-to-5`](presets/base/4-to-5/README.md) | `preset_versions.base` 4 → 5 | **Manual**: the `tube`/`cryo`/`animal` status sets leave the base preset; the ledger's stored copy is replaced with this checkout's status-free one. The counter is written by the spec step's `tm schema apply`. |
| [`presets/bacteria/1-to-2`](presets/bacteria/1-to-2/README.md) | `preset_versions.bacteria` 1 → 2 | **Manual**: the `tube` status set leaves the bacteria part. |
| [`presets/biosamples/1-to-2`](presets/biosamples/1-to-2/README.md) | `preset_versions.biosamples` 1 → 2 | **Manual**: the `tube` status set leaves the biosamples part. |
| [`presets/c_elegans/1-to-2`](presets/c_elegans/1-to-2/README.md) | `preset_versions.c_elegans` 1 → 2 | **Manual**: the `worm` status set becomes the optional `condition` stock property of a worm culture. |
| [`presets/cell_culture/2-to-3`](presets/cell_culture/2-to-3/README.md) | `preset_versions.cell_culture` 2 → 3 | **Manual**: the `cryo`/`tube` status sets leave the cell culture part. |
| [`presets/drosophila/1-to-2`](presets/drosophila/1-to-2/README.md) | `preset_versions.drosophila` 1 → 2 | **Manual**: the `fly` status set becomes the optional `fate` stock property of a fly culture. |
| [`presets/immuno/1-to-2`](presets/immuno/1-to-2/README.md) | `preset_versions.immuno` 1 → 2 | **Manual**: the `tube` status set leaves the immuno part. |
| [`presets/library/2-to-3`](presets/library/2-to-3/README.md) | `preset_versions.library` 2 → 3 | **Manual**: the `tube` status set leaves the library part. |
| [`presets/mouse/1-to-2`](presets/mouse/1-to-2/README.md) | `preset_versions.mouse` 1 → 2 | **Manual**: the `animal`/`cryo` status sets leave the mouse part; a cohort's detailed state becomes the optional `fate` stock property. |
| [`presets/nucleic_acids/2-to-3`](presets/nucleic_acids/2-to-3/README.md) | `preset_versions.nucleic_acids` 2 → 3 | **Manual**: the `tube` status set leaves the nucleic acids part. |
| [`presets/proteins/2-to-3`](presets/proteins/2-to-3/README.md) | `preset_versions.proteins` 2 → 3 | **Manual**: the `tube` status set leaves the proteins part. The frozen `proteins-3.yaml` keeps this step installing v3 after [proteins 3 → 4](presets/proteins/3-to-4/README.md) bumps the part again. |
| [`presets/proteins/3-to-4`](presets/proteins/3-to-4/README.md) | `preset_versions.proteins` 3 → 4 | Splits the shared `sequence_fields` mixin: `protein_construct` gets its own `sequence` with the amino-acid pattern and `length_aa`, while `plasmid` keeps the nucleotide `sequence`/`length_bp`. Applies the `length_bp` → `length_aa` rename from `renames.yaml`, places the frozen v4 copy, recreates the construct view; requires spec 6 and runs after `spec/5-to-6`. |
| [`presets/viral_vectors/1-to-2`](presets/viral_vectors/1-to-2/README.md) | `preset_versions.viral_vectors` 1 → 2 | **Manual**: the `tube` status set leaves the viral vectors part. |
| [`presets/zebrafish/1-to-2`](presets/zebrafish/1-to-2/README.md) | `preset_versions.zebrafish` 1 → 2 | **Manual**: the `animal`/`cryo` status sets leave the zebrafish part; a group's detailed state becomes the optional `fate` stock property. |
