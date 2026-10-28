# spec 2 → 3 — generic aliquots: no `aliquot_<stockkind>` tables, richer `v_stock_<kind>`

Counter: `ledger_meta.spec_version` 2 → 3. Tag before writing: `pre-migrate-spec-2`.
One commit: `migrate(ledger): spec 2 -> 3`.

## What changes

Released as **0.2.0** together with molbio 2 and the migration groundwork (tickets 20–23). Spec 3
settles git-bug `2f6a0d5` by making the model explicit: **an aliquot is a generic tube** (location,
quantity, unit, status, label, notes) and every property of the material belongs on the **stock kind**
— tubes that differ in a property are different stocks. Two generated-shape changes follow:

1. **The per-stock-kind aliquot extension tables are removed.** Spec 1/2 let a stock kind declare a
   map of tube fields, which generated a sibling table `aliquot_<stockkind>(id PK FK→aliquot,
   <fields…>)`. The schema key no longer loads at all, and no DDL is generated for it, so a preset or
   local kind that still declares it fails to load with a pointer to this README.
2. **Every `v_stock_<kind>` gains columns**, inserted before the final `alias_list`:
   `entity_kind`, `entity_name`, `aliquots_stored`, `aliquots_total`, `quantity_stored`, `unit`,
   `status`. `v_stock_status` keeps its columns, but its derived status now comes from the same SQL
   fragment as the stock views. `aliquot_event` stays one shared table (it never had a kind), and no
   `v_aliquot_<kind>` view exists.

What the step does, in order (one Dolt commit, after the pre-migration tag):

- **refuses** — before writing anything — when an extension table still holds a value: it lists every
  `table.column` with up to four of its non-NULL values and tells you to move those properties to the
  stock kind first (see Manual steps);
- **drops** every remaining extension table (an empty table, or one whose property columns are all
  `NULL`, loses nothing);
- **recreates all generated views** from the ledger's own loadable schema, in the spec-3 shape;
- **regenerates the derived `dolt_tests`** from the same schema (the stored spec-2 tests were generated
  with the old shape and may name `aliquot_<kind>` tables);
- **bumps** `spec_version` to 3, `tetramark_version` and the fingerprint, all in the same commit. The
  step declares its target schema (`schema=`), so the shared runner writes that fingerprint and
  installs the regenerated tests in the commit, and verifies the counter/fingerprint/commit message
  afterwards.

The YAML schema itself does **not** change in this step (no preset version bump): if a ledger's preset
still declares the removed key, upgrade it first (Manual steps).

## A 0.2.0+ caveat: a spec-1/2 ledger with the old key cannot even be opened

The loader refuses a kind that declares the removed per-tube field map (that is the point: an old
preset must not silently keep per-tube fields). Therefore:

- a ledger whose `<ledger>/presets/` or local `kinds/` still declares the key **cannot be opened by
  0.2.0+**, and this step cannot run on it;
- run the spec-1/spec-2 era steps with **the last checkout that accepted it** — the wave-9 tree with
  ticket 22 merged (`3ec57d5`, the last 0.2.0-preview tree) — including
  `migration/presets/molbio/1-to-2/` and any local step that moves the tube fields to the stock kind;
  then switch to the 0.2.0 tree and run this step. Ticket 24 (zsqlab10) runs exactly that way.

## Preconditions

- `ledger_meta.spec_version = 2` (the script refuses any other value, and a second run therefore
  refuses).
- A clean working set (`dolt_status` empty). The script refuses otherwise: a migration commit must
  contain only the migration.
- The ledger is **loadable by 0.2.0+**: no preset/local kind declares the removed per-tube field map.
  After the molbio preset step its copy is v2; a local `kinds/` file may still need the manual edit
  below.
- No rule or hand-written query depends on an `aliquot_<kind>` table or on the old stock-view column
  order. Additive `SELECT … FROM v_…` queries keep working; a rule that names the extension table must
  be edited by hand **before** the step (it would fail at query time, not here).
- A backup/branch rule for real lab ledgers (ticket 24): work on a branch
  (`--branch tetramark-upgrade`), never on `main`, never push.
- The tag `pre-migrate-spec-2` must not exist at a different commit; the script refuses if it points
  anywhere other than HEAD.

## Running it

```sh
# 1. inspect: the tag, the dropped tables, every CREATE/DROP VIEW, the meta bump
uv run migration/spec/2-to-3/migrate.py --ledger /path/to/ledger --dry-run

# 2. apply (one commit, tag first)
uv run migration/spec/2-to-3/migrate.py --ledger /path/to/ledger

# 3. on a branch of a served ledger
uv run migration/spec/2-to-3/migrate.py --ledger /path/to/ledger --branch tetramark-upgrade
```

`--dry-run` writes nothing: HEAD, the tag table, the views and `dolt_tests` stay untouched (the
statements are printed with their terminating `;`). A refusal (populated extension table, wrong
counter, dirty working set) also writes nothing.

## Manual steps

1. **Move a populated extension table's properties to the stock kind first.** The step refuses and
   lists them; e.g. molbio 1's `aliquot_bacterial.glycerol_pct` becomes
   `stock_bacterial_glycerol.glycerol_pct` through
   [`migration/presets/molbio/1-to-2/`](../../presets/molbio/1-to-2/README.md), which copies one value
   per stock and drops the table. For a local kind, write a preset/local step (a YAML change plus a
   short `migrate.py` that copies the values per stock with SQL) and run it with the last checkout
   that still opens the ledger; then run this step from the 0.2.0 tree.
2. **Remove the key from local YAML.** A `kinds/` file under the ledger's `kinds_dir` that still
   declares the per-tube field map must be edited by hand (the loader refuses it). Run the grep
   below; expected output after the edit: nothing.
3. **Hand-written rule SQL** naming an `aliquot_<kind>` table or the old stock-view column list is
   not rewritten by this step. Fix it with the ledger's `rules/` files before running the step (or
   accept a failing `tm check` afterwards and fix it then).

Grep the ledger's own schema/rules for leftovers (run from the ledger directory; use the
`kinds_dir`/`rules_dir` named in `ledger.yaml` if they are not the default `kinds/`/`rules/`; expected
output: nothing):

```sh
LEDGER=/path/to/ledger
grep -rnE "aliquot_[a-z]|aliquot_fields" "$LEDGER/kinds" "$LEDGER/rules"
```

Also inspect hand-written SQL that names the stock views, in case it pins the old column list or
assumes no `status` column:

```sh
grep -rn "v_stock_" "$LEDGER/rules"
```

## Verification

```sh
LEDGER=/path/to/ledger
# 1. counter
dolt sql -q "SELECT \`value\` FROM \`ledger_meta\` WHERE \`key\` = 'spec_version'"   # 3
# 2. exactly one migration commit and its message
dolt log -n 1                                                                      # migrate(ledger): spec 2 -> 3
# 3. restore point
dolt tag | grep pre-migrate-spec-2
# 4. no extension table is left (only the core aliquot_event)
dolt sql -q "SHOW TABLES LIKE 'aliquot\\_%'"
# 5. the new stock-view columns exist and carry values
dolt sql -q "SHOW COLUMNS FROM \`v_stock_bacterial_glycerol\`"
dolt sql -r csv -q "SELECT \`id\`, \`entity_kind\`, \`aliquots_stored\`, \`quantity_stored\`, \`unit\`, \`status\` FROM \`v_stock_bacterial_glycerol\`"
# 6. validation: no FAIL, and the dolt_tests no longer name a dropped table
uv run tm -w /path/to/workspace -l <ledger> check
dolt sql -q "SELECT COUNT(*) FROM \`dolt_tests\` WHERE \`test_query\` LIKE '%aliquot\\_%' AND \`test_query\` NOT LIKE '%aliquot\\_event%'"  # 0
```

## Failure, rollback, caveats

- **A populated extension table is a refusal, not a failure.** Nothing is written (no tag, no commit,
  no DDL): follow Manual step 1 and retry.
- **Dolt DDL is not transactional.** If a `DROP TABLE`/`CREATE VIEW` fails mid-way, the statements
  already executed stay in the working set; the script makes **no** commit and prints the last good
  commit (`DOLT_HASHOF('HEAD')`, which is the tag target) plus:

  ```sh
  dolt reset --hard <last-good-commit>
  ```

  A hard reset restores tracked tables (including the `dolt_schemas` rows that define the old views),
  but Dolt leaves *untracked* objects created by the failed attempt behind — check
  `SHOW FULL TABLES` afterwards and drop any stray view/table by hand.
- **Server-only caveats.** When the parent workspace's `dolt sql-server` is running, the step runs
  through `ServerDriver`: the same SQL and the same one-commit rule, but the `CALL DOLT_TAG(...)` is
  executed through the server session and is **not** transactional (a tag is a repository ref, not a
  table row), so it survives a failed step — that is intended, it is the restore point. To run
  `dolt reset --hard` against a served database, either stop the server first
  (`uv run tm server stop`) or reset through the server:
  `dolt sql -q "CALL DOLT_RESET('--hard', '<hash>')"` while the server is up (the CLI and the server
  must not both write the repository). `--branch B` uses a session-scoped `db/B` revision database, so
  other sessions keep seeing the branch they had checked out; the tag is repository-wide.
- **Rollback after success:** reset the branch to the tag and use the older tool again:

  ```sh
  dolt reset --hard pre-migrate-spec-2
  ```

  Resetting to a spec-2 shape only makes sense with a tool that still knows the old shape; the old
  preset copies under `<ledger>/presets/` are files and are not restored by the reset.
