# Migrations

## The maintenance rule

**Whoever bumps `SPEC_VERSION` (in `src/tetramark/schema/model.py`) or a
preset's `version:` adds exactly one folder for that step, in the same change.**
No release may ship a bumped counter without its step folder here.

## What this directory is

Hand-run upgrade steps for ledgers whose **format** changed. One folder per
counter bump, named after the counter that changed — never after the Tetramark
release.

```
migration/
  README.md
  spec/<N>-to-<N+1>/            README.md, migrate.py
  presets/<name>/<N>-to-<N+1>/  README.md, migrate.py, renames.yaml (optional)
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
   commit, and the commit message is verified afterwards;
6. Dolt DDL is **not transactional**. If a statement fails, the step prints the
   last good commit and the restore command (`dolt reset --hard <hash>`), makes
   **no** commit, and exits 1. The pre-migration tag stays as the restore
   point.

A second run on an already-migrated ledger refuses: the counter is `N+1`, not
`N`.

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

`migrate.py` is short; it imports `MigrationStep`/`main` from
`tetramark.migrate` and builds its SQL from the schema helpers (never from
hard-coded DDL where a generator exists). Use the `Ledger`/`Driver` API
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
names of the loaded schema. `fields` keys are `<object>.<new kind>`; use
`aliquot.<stock kind>` for the `aliquot_<kind>` extension table. Load the file
with `tetramark.migrate.load_renames(path)` and pass it to the planner:

```python
from tetramark.migrate import load_renames
from tetramark.schema.diff import plan

renames = load_renames(Path(__file__).with_name("renames.yaml"))
schema_plan = plan(ledger, renames=renames)   # or build_plan(driver, schema, ...)
```

The planner reuses the `renamed_from` machinery: it renames the kind table, the
`aliquot_<kind>` table and columns, rewrites the core `kind` discriminator
values, and translates constraints/views — no hand-written SQL, no data loss.

## Existing steps

| Step | Counter | What it does |
|---|---|---|
| [`spec/1-to-2`](spec/1-to-2/README.md) | `spec_version` 1 → 2 | Recreates the generated views in the spec-2 shape (`alias_list`/`occupant_alias_list`). |
