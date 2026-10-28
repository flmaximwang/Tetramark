# spec 1 → 2 — `alias_list` in the generated views

Counter: `ledger_meta.spec_version` 1 → 2. Tag before writing:
`pre-migrate-spec-1`. One commit: `migrate(ledger): spec 1 -> 2`.

## What changes

Smoke-released as **0.1.2** (commit `861faae`, "feat: aliases in generated views
and tm show (spec 2)").  The `alias` table was write-only for readers: the
generated views did not expose it.  Spec 2 adds, to the **generated views
only**:

| View | New column |
|---|---|
| `v_<object>_<kind>` (every kind) | `alias_list` |
| `v_stock_status` | `alias_list` |
| `v_aliquot_location` | `alias_list` |
| `v_box_slots` | `occupant_alias_list` (the occupant's aliases: aliquot for a tube, box for a nested box, `NULL` for an empty slot) |

Each column is a correlated scalar subquery
`(SELECT GROUP_CONCAT(alias ORDER BY alias SEPARATOR ';') FROM alias WHERE …)`,
so it is sorted, `;`-joined and `NULL` when the object has no aliases (spec §3).
The name is `alias_list`, not `aliases`, because a kind may already define a
domain field named `aliases` (the bundled `construct` kind does).

Checked against the commit (`git show 861faae --stat`): the only stored-shape
change is `src/tetramark/schema/ddl.py` (the view SQL) plus `SPEC_VERSION` 1 → 2.
The generated `dolt_tests` (`src/tetramark/schema/tests_gen.py`) did **not**
change, so a spec-1 ledger's stored tests are already the spec-2 set; no table,
row or `ledger_meta` key other than the counter is affected.  The rest of the
commit is CLI/`show` output and docs, which do not change the stored shape.

This step therefore only replaces every generated view: `DROP VIEW` +
`CREATE VIEW` per view, built from the ledger's own loadable schema
(`view_statements(ledger.schema)`), followed by the `ledger_meta` bump done by
the shared runner (`src/tetramark/migrate.py`): `spec_version` 2,
`tetramark_version` = the running tool, `fingerprint` recomputed from that same
schema — all in one Dolt commit.

## What the current `tm` does when it opens a spec-1 ledger

Verified against this tree (a spec-1 fixture, and the code path in
`Ledger.open`): **there is no `spec_version` guard.**  `Ledger.open`
(`src/tetramark/core/ledger.py`) loads `ledger.yaml` + the effective schema,
reads `ledger_meta` and calls `check_fingerprint()`; nothing compares
`spec_version` with `SPEC_VERSION` (that comparison only exists in the still
open `tm upgrade` proposal, `docs/issues/001-tm-upgrade.md`).

As a consequence:

- **Opening warns, every time.**  `EffectiveSchema.normalised()` includes
  `spec_version`, so the fingerprint a spec-1 tool wrote is guaranteed to
  differ from the one this tool computes.  A spec-1 ledger prints a
  `UserWarning`: *"the live schema fingerprint does not match ledger.yaml
  (live: …, expected: …); run 'tm schema plan' to see the pending changes"*,
  also collected in `ledger.warnings`.
- **Nothing is rewritten, refused or upgraded on open.**  Reads work, and
  writes are *accepted* — a newer `tm` will happily write to the old-shape
  ledger (the warning is not an error).
- **`tm schema plan` shows the fix**: `drop_view` + `create_view` for every
  generated view, all tagged `additive`, 0 destructive, plus
  "ledger_meta is stale; a meta refresh will be applied".  `tm schema apply`
  would perform exactly that and bump `spec_version`, but with an
  `apply(schema): …` commit and no pre-migration tag — which is why this
  auditable, tagged, `migrate(ledger): …` step exists.

## Preconditions

- `ledger_meta.spec_version = 1` (the script refuses any other value, and a
  second run therefore refuses).
- A clean working set (`dolt_status` empty).  The script refuses otherwise: a
  migration commit must contain only the migration.
- The ledger's YAML schema is loadable and describes the **same kinds** the
  views were generated from — normally true, because presets were not renamed
  in 0.1.2.  The step regenerates the views from the ledger's own presets, so
  a kind set that is already spec-2 shaped works unchanged.
- A backup/branch rule for real lab ledgers (ticket 24): work on a branch
  (`--branch tetramark-upgrade`), never on `main`, never push.
- The tag `pre-migrate-spec-1` must not exist at a different commit; the script
  refuses if it points anywhere other than HEAD.

## Running it

```sh
# 1. inspect: prints the tag, every CREATE/DROP VIEW and the ledger_meta bump
uv run migration/spec/1-to-2/migrate.py --ledger /path/to/ledger --dry-run

# 2. apply (one commit, tag first)
uv run migration/spec/1-to-2/migrate.py --ledger /path/to/ledger

# 3. on a branch of a served ledger
uv run migration/spec/1-to-2/migrate.py --ledger /path/to/ledger --branch tetramark-upgrade
```

`--dry-run` writes nothing: HEAD, the tag table and every view stay untouched
(the statements are printed with their terminating `;`).

## Manual steps

- **Kind fields named `alias_list`.**  Spec 2 reserves that name for the
  generated view column, and the DDL generator rejects a kind that defines it.
  If the grep below finds one, rename that kind field first (a `renamed_from`
  schema change via `tm schema plan`/`apply` before running this step);
  otherwise the step fails while creating that view.
- **Hand-written rule SQL in `rules/` is not rewritten by this step** (and does
  not need to be): the change is additive, so `SELECT … FROM v_…` queries keep
  working; only a query that hard-codes the *positional* column list of a view
  would need an edit.  Fix such rules by hand with `tm`'s rule files, *before*
  the step, then run it.

Grep the ledger's own schema/rules for leftovers (run from the ledger
directory; use the `kinds_dir`/`rules_dir` named in `ledger.yaml` if they are
not the default `kinds/`/`rules/`; expected output: nothing):

```sh
LEDGER=/path/to/ledger
grep -rn "alias_list\|occupant_alias_list" "$LEDGER/kinds" "$LEDGER/rules"
```

Also inspect hand-written SQL that names the generated views, in case it pins a
column list:

```sh
grep -rn "v_entity_\|v_stock_\|v_box_\|v_aliquot_location\|v_stock_status" \
  "$LEDGER/kinds" "$LEDGER/rules"
```

## Verification

```sh
LEDGER=/path/to/ledger
# 1. counter
dolt sql -q "SELECT \`value\` FROM \`ledger_meta\` WHERE \`key\` = 'spec_version'"   # 2
# 2. exactly one migration commit and its message
dolt log -n 1                                                                      # migrate(ledger): spec 1 -> 2
# 3. restore point
dolt tag | grep pre-migrate-spec-1
# 4. the new view columns exist and work
dolt sql -q "SHOW COLUMNS FROM \`v_box_slots\` LIKE 'occupant_alias_list'"
dolt sql -q "SHOW COLUMNS FROM \`v_stock_status\` LIKE 'alias_list'"
dolt sql -q "SELECT \`alias_list\` FROM \`v_entity_item\`"   # NULL when no aliases
# 5. validation: no FAIL
uv run tm -w /path/to/workspace -l <ledger> check
```

`tm check` runs the generated `dolt_tests`; it passes both before and after the
step (the tests do not inspect view columns), so the meaningful post-checks are
the counter, the tag, the commit message and the columns above.

## Failure, rollback, caveats

- **Dolt DDL is not transactional.**  If a `CREATE VIEW` fails, the statements
  already executed stay in the working set; the script makes **no** commit and
  prints the last good commit (`DOLT_HASHOF('HEAD')`, which is the tag target)
  plus:

  ```sh
  dolt reset --hard <last-good-commit>
  ```

  A hard reset restores tracked tables (including the `dolt_schemas` rows that
  define the old views), but Dolt leaves *untracked* objects created by the
  failed attempt behind — check `SHOW FULL TABLES` afterwards and drop any
  stray view/table by hand.
- **Server-only caveats.**  When the parent workspace's `dolt sql-server` is
  running (e.g. the lab workspace), the step runs through `ServerDriver`: the
  same SQL and the same one-commit rule, but:
  - the `CALL DOLT_TAG(...)` is executed through the server session and is
    **not** transactional (a tag is a repository ref, not a table row), so it
    survives a failed step — that is intended, it is the restore point;
  - to run `dolt reset --hard` against a served database, either stop the
    server first (`uv run tm server stop`) or reset through the server:
    `dolt sql -q "CALL DOLT_RESET('--hard', '<hash>')"` while the server is
    up (the CLI and the server must not both write the repository);
  - `--branch B` uses a session-scoped `db/B` revision database, so other
    sessions keep seeing the branch they had checked out; the tag is
    repository-wide.
- **Rollback after success:** reset the branch to the tag and re-run nothing
  until the old tool is used:

  ```sh
  dolt reset --hard pre-migrate-spec-1
  ```

  (The tag is not moved afterwards.  A reset on a branch the operator does not
  own is destructive to their working set — coordinate before doing it.)
