# molbio 1 → 2 — kind renames, cell kinds, `glycerol_pct` on the stock

Counter: `ledger_meta.preset_versions.molbio` 1 → 2. Tag before writing:
`pre-migrate-molbio-1`. One commit: `migrate(ledger): molbio 1 -> 2`.

## What changes

Ships with **0.2.0** (ticket 22). The molbio preset moves to one rule — *all
aliquots of one stock are the same* — so tube-specific properties become stock
fields and the kind names say what the thing is:

| object | old kind | new kind |
|---|---|---|
| entity | `construct` | `protein_construct` |
| entity | `strain` | `bacterial_strain` |
| stock | `bacterial` | `bacterial_glycerol` |
| stock | `pellet` | `bacterial_pellet` |

Column and field names inside a kind do not change (`plasmid.construct` keeps
its name and now references `protein_construct`). `renames.yaml` feeds the four
renames to the diff planner, so the kind table, columns, constraints, views and
the core `kind` discriminator values are rewritten by the existing machinery —
not by hand-written renames. No data is dropped.

The bacterial tube field `glycerol_pct` moves to `stock_bacterial_glycerol`
(`DECIMAL(5,2) DEFAULT 15`): each stock's value is copied from its aliquots'
`aliquot_bacterial` rows, then that extension table is dropped. Every aliquot of
one stock must agree on the value; the step **refuses before writing anything**
and lists the offending stock ids if they do not (set one value, or `NULL`
everywhere, in the old table first). A stock whose aliquots recorded no value
keeps the new column's default.

The v2 preset adds kinds (additive: tables, views, FKs and generated tests only):

- `cell_line` (entity): `species`, `cell_type`, `source`, `genotype`, and
  `parental` (a self-reference to the parent line — stable lines point at their
  parent instead of a plasmid);
- `cell_vial` (stock): `passage`, `medium`, `freezing_medium`,
  `cells_per_vial`, `dmso_pct` (default 10);
- `cell_pellet` (stock): `cell_count`, `passage`, `transfection`,
  `harvested_on`; it accepts a `cell_line` or a `plasmid` (transient
  transfection / transduction harvests).

The generated `dolt_tests` are regenerated from the v2 schema **in the same
commit** (MB02 now checks `stock_bacterial_glycerol`, MB04
`entity_protein_construct`; a new MB05 checks `cell_vial.dmso_pct` is 0–100), and
`ledger_meta.fingerprint` is written from the v2 schema, not from the old one.

Presets are files, not Dolt content: **`<ledger>/presets/molbio.yaml` (the
ledger's own copy) is replaced with this repository's v2 copy after the commit**
(the runner's `after_commit` hook), then the step reopens the ledger and checks
that its fingerprint equals the migrated one. Without that file swap the next
`tm open` would load molbio v1 while `preset_versions.molbio` says 2.
`base.yaml` is unchanged at version 1 and is only re-copied if the ledger's copy
differs from the repository one.

## Preconditions

- `preset_versions.molbio = 1` (the script refuses any other value, so a second
  run refuses) and a `spec_version` the running tool supports (`2`).
- The ledger still has the v1 kind tables the rename map names
  (`entity_construct`, `entity_strain`, `stock_bacterial`, `stock_pellet`);
  the step refuses and lists the missing ones otherwise (a half-migrated
  ledger, or a typo in `renames.yaml`).
- A clean working set (`dolt_status` empty).  The script refuses otherwise: the
  migration commit must contain only the migration.
- **Back up the ledger directory first** — in particular `<ledger>/presets/`,
  which is not stored in Dolt.  `cp -a <ledger> <ledger>.pre-v2` is enough.
- A backup/branch rule for real lab ledgers (ticket 24): work on a branch
  (`--branch tetramark-upgrade`), never on `main`, never push.
- The tag `pre-migrate-molbio-1` must not exist at a different commit; the
  script refuses if it points anywhere other than HEAD.
- Run it from a Tetramark checkout that contains the v2 `presets/` (the step
  reads `presets/molbio.yaml` and `presets/base.yaml` from the repository), and
  with the matching tool version (`uv run` resolves the checkout's venv).

## Running it

```sh
LEDGER=/path/to/ledger

# 1. backup the directory, presets/ included
cp -a "$LEDGER" "$LEDGER.pre-v2"

# 2. inspect: prints the tag, every statement, the generated-tests line and
#    the after-commit file note; writes nothing
uv run migration/presets/molbio/1-to-2/migrate.py --ledger "$LEDGER" --dry-run

# 3. apply (one commit, tag first, presets vendored after the commit)
uv run migration/presets/molbio/1-to-2/migrate.py --ledger "$LEDGER"

# 4. on a branch of a served ledger
uv run migration/presets/molbio/1-to-2/migrate.py --ledger "$LEDGER" \
  --branch tetramark-upgrade
```

`--dry-run` writes nothing: Dolt HEAD, the tag table, the views/tables and the
preset files stay untouched (the statements are printed with their terminating
`;`).

The printed plan has one deliberately spliced point — the data move sits between
the `ADD COLUMN` and the drop of the source table, so the order is
`... ADD COLUMN stock_bacterial_glycerol.glycerol_pct; UPDATE ...; DROP TABLE
aliquot_bacterial; ...`. The planner cannot see the old extension table after
the stock rename, so the step drops it explicitly after the copy.

## Manual steps

**Hand-written rule SQL and local kinds are not rewritten.**  The four kind
renames change table names and the core `kind` values; a ledger's own
`rules/*.yaml` and `kinds/*.yaml` may still name the old ones.  Rewrite them
before running `tm check`, then run the step:

| old name | new name |
|---|---|
| `construct` / `entity_construct` | `protein_construct` / `entity_protein_construct` |
| `strain` / `entity_strain` | `bacterial_strain` / `entity_bacterial_strain` |
| `bacterial` / `stock_bacterial` | `bacterial_glycerol` / `stock_bacterial_glycerol` |
| `pellet` / `stock_pellet` | `bacterial_pellet` / `stock_bacterial_pellet` |
| `aliquot_bacterial` (extension table) | gone — the field is `stock_bacterial_glycerol.glycerol_pct` |

Grep the ledger's own schema/rules for leftovers (run from the ledger directory;
use the `kinds_dir`/`rules_dir` named in its `ledger.yaml` if they are not the
default `kinds/`/`rules/`; expected output after the edits: nothing):

```sh
LEDGER=/path/to/ledger
grep -nE "\b(construct|strain|bacterial|pellet)\b|entity_construct|entity_strain|stock_bacterial\b|stock_pellet|aliquot_bacterial" "$LEDGER/kinds" "$LEDGER/rules"
```

A local kind that still *defines* an old molbio kind name (for example a local
`pellet` overlay) now collides with the renamed preset kind: rename it in the
same edit or delete it, then `tm schema plan`/`tm schema apply` before running
the step.

**Restoring the preset files.**  The v2 copies come from the repository.  If
the post-commit step failed (the step prints `commit <hash> was made but the
post-commit preset copy update failed: ...`), the database is migrated and only
the files are stale:

```sh
cp <tetramark-checkout>/presets/molbio.yaml "$LEDGER/presets/molbio.yaml"
cp <tetramark-checkout>/presets/base.yaml   "$LEDGER/presets/base.yaml"  # unchanged, only if edited
# then:
uv run tm -w <workspace> -l <ledger> check
```

To return to the pre-migration state, reset Dolt to the tag **and** restore the
preset directory from the backup you made above:

```sh
dolt reset --hard pre-migrate-molbio-1
cp -a "$LEDGER.pre-v2/presets/." "$LEDGER/presets/"
```

(The v1 preset text is also in the 0.2.0 checkout's history:
`git show <0.2.0^>:presets/molbio.yaml`.)

## Verification

```sh
LEDGER=/path/to/ledger
# 1. counter and commit
dolt sql -q "SELECT \`value\` FROM \`ledger_meta\` WHERE \`key\` = 'preset_versions'"  # {"base": 1, "molbio": 2}
dolt log -n 1                                                                         # migrate(ledger): molbio 1 -> 2
# 2. restore point
dolt tag | grep pre-migrate-molbio-1
# 3. the old tables are gone, the new ones exist
dolt sql -q "SHOW TABLES LIKE 'entity_protein_construct'"   # 1 row
dolt sql -q "SHOW TABLES LIKE 'stock_bacterial_glycerol'"   # 1 row
dolt sql -q "SHOW TABLES LIKE 'stock_cell_vial'"            # 1 row
dolt sql -q "SHOW TABLES LIKE 'aliquot_%'"                  # only core aliquot_event
# 4. the moved value: one row per old bacterial stock, same percentage
dolt sql -q "SELECT id, glycerol_pct FROM stock_bacterial_glycerol"
# 5. every old row kept its kind identity
dolt sql -q "SELECT kind, COUNT(*) FROM entity GROUP BY kind"
dolt sql -q "SELECT kind, COUNT(*) FROM stock GROUP BY kind"
# 6. the ledger's own preset copy is v2
grep -E '^version:' "$LEDGER/presets/molbio.yaml"           # version: 2
# 7. validation: no FAIL
uv run tm -w /path/to/workspace -l <ledger> check
```

`tm check` runs the **regenerated** `dolt_tests`, so it is the meaningful
end-to-end check: stale tests would reference the old tables and FAIL here.

## Failure, rollback, caveats

- **Dolt DDL is not transactional.**  If a statement fails, the step makes
  **no** commit, prints the last good commit (`DOLT_HASHOF('HEAD')`, which is the
  tag target) and:

  ```sh
  dolt reset --hard <last-good-commit>
  ```

  The preset files are only written after a successful commit, so a failed step
  leaves them untouched.
- **The post-commit file step can fail after the commit exists.**  The step then
  reports `commit <hash> was made but the post-commit preset copy update
  failed: ...` and exits 1; copy the preset files as shown above, then reopen the ledger
  (`tm schema show` must not warn about a fingerprint mismatch).
- **Server-only caveats.**  When the parent workspace's `dolt sql-server` is
  running (e.g. the lab workspace), the step runs through `ServerDriver`: the
  same SQL and the same one-commit rule, but:
  - the `CALL DOLT_TAG(...)` is executed through the server session and is
    **not** transactional (a tag is a repository ref, not a table row), so it
    survives a failed step — that is intended, it is the restore point;
  - to run `dolt reset --hard` against a served database, either stop the
    server first (`uv run tm server stop`) or reset through the server:
    `dolt sql -q "CALL DOLT_RESET('--hard', '<hash>')"` while the server is up
    (the CLI and the server must not both write the repository);
  - `--branch B` uses a session-scoped `db/B` revision database, so other
    sessions keep seeing the branch they had checked out; the tag is
    repository-wide.
- **Rollback after success:** see *Restoring the preset files* — reset to the
  tag **and** restore `<ledger>/presets/`; a reset alone leaves the v2 files in
  place and the v1 database would warn about a fingerprint mismatch (and
  `tm check` would use the v2 tests).  A reset on a branch the operator does not
  own is destructive to their working set — coordinate before doing it.
- **Preset files are shared by every branch and worktree of the ledger.**
  `--branch` moves only the Dolt commit (and the counter/fingerprint stored in
  `ledger_meta`); `<ledger>/presets/` is a plain directory next to
  `ledger.yaml`.  Migrating a branch to molbio 2 therefore makes the *main*
  checkout load v2 files against a v1 database, which only warns (a
  fingerprint mismatch, not an error) but is best avoided: migrate the branch
  you intend to use, and if you switch back restore the v1 preset files from
  the backup together with the reset to the tag.
- **A second run refuses** (`preset_versions.molbio` is 2, not 1).
