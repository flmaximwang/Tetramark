# spec 7 → 8 — drop the unused `aliquot.notes` column

Counter: `ledger_meta.spec_version` 7 → 8. Tag before writing: `pre-migrate-spec-7`
(you create it by hand, below). One commit: `apply(schema): refresh ledger_meta`.

**This is a manual (README-only) step: there is no `migrate.py`.** `tm schema plan`
does not diff core-table columns — it only creates missing core tables and
reconciles core `CHECK`s — so the column drop is one hand-run `ALTER TABLE`, and
`tm schema apply` only refreshes `ledger_meta` (which also writes the new
counter). The step deliberately ships **no script and no safeguard**: the
upgrade is documented here, and the operator decides whether an aliquot's
`notes` text is worth exporting before it is dropped.

## What changes

Spec 8 (`Tetramark-8km.4`) removes `aliquot.notes`. An aliquot has no note of
its own worth reading: it borrows its stock's note, which `tm aliquot list`
(`*notes`) and `tm aliquot show` (`* notes`, plus the `* inherited from stock
<id>` line) already show since `Tetramark-8km.1` (0.8.50). The column is unused
by the tool after this release:

- **new ledgers never create it** — `schema/ddl.py`'s `_create_aliquot` no
  longer emits the column, so `tests/data/golden/ddl_full.sql` has no
  `notes` line in the `aliquot` table;
- the read/write paths stop naming it: `_ALIQUOT_COLUMNS`, the `tm aliquot
  edit` allowlist (`notes` is now an unknown field), the `tm aliquot list`
  allowlist (`--where notes=…` is an unknown field; `stock_notes` is the note
  filter), `show_aliquot`/`list_aliquots` records, and the aliquot CSV columns
  (import and export).

The schema plan compares the stored counter with the tool's (the counter is
also in the schema fingerprint, so a ledger last written by a spec-7 tool
carries a different fingerprint too), so a ledger at spec 7 reports its
`ledger_meta` as stale under the spec-8 tool even when its Kinds already match.
That is what lets `tm schema apply` record spec 8 after the manual `ALTER`.

## Before upgrading (do this first)

1. **Back up the ledger directory** (a copy of the Dolt database directory; the
   Dolt history alone is not enough because this step drops a column).

2. **Export any non-empty aliquot notes.** The `dolt` dump below works with
   any tool version, at any time **before the `ALTER` in step 3** (it reads the
   column directly, so it is listed first). The `tm` command needs a **spec-7
   tool (0.8.48–0.8.51 at the time of writing)**, because 0.8.52 no longer
   exposes the tube's own note:

   ```sh
   dolt --data-dir LEDGER sql -r csv \
     -q "SELECT id, notes FROM aliquot WHERE notes IS NOT NULL AND TRIM(notes) <> ''" \
     > aliquot-notes.csv

   # with a spec-7 tool only (the whole record, in one file):
   uv run tm -l LEDGER aliquot list --all --json > aliquot-notes.json
   ```

   Check that the CSV (if any rows came back) holds what you expect before you
   continue. A future `tm aliquot show` cannot reconstruct it: an aliquot's own
   `notes` were never displayed by the spec-8 CLI (they were always the tube's
   private field; `tm aliquot list`/`show` show the **stock's** note).

3. **Make a restore point, then drop the column by hand** in the ledger
   directory. Dolt DDL is not transactional, so tag first and run the one
   `ALTER` after the backup:

   ```sh
   dolt --data-dir LEDGER tag pre-migrate-spec-7
   dolt --data-dir LEDGER sql -q "ALTER TABLE `aliquot` DROP COLUMN `notes`"
   ```

4. **Refresh the meta counter** with the spec-8 tool:

   ```sh
   uv run tm -l LEDGER schema plan    # "schema is up to date" + "ledger_meta is stale"
   uv run tm -l LEDGER schema apply   # one commit: apply(schema): refresh ledger_meta
   ```

   No `--allow-destructive` is needed: the plan has no steps, it is a meta
   refresh. The commit writes `spec_version` 8, the fingerprint, the tool
   version and the `ledger.yaml` copy.

5. **Verify**:

   ```sh
   uv run tm -l LEDGER status                      # spec 8
   dolt --data-dir LEDGER sql -q "SHOW COLUMNS FROM \`aliquot\`"   # no notes row
   uv run tm -l LEDGER check                       # no FAIL
   uv run tm -l LEDGER schema plan                 # up to date, not stale
   ```

   `tm check` must be green (its generated tests do not read `aliquot.notes`);
   reopening the ledger must not warn about a stale fingerprint or a spec
   mismatch any more.

## Old CSVs

An aliquot CSV exported **before** the upgrade still carries a `notes` column.
`tm import` now rejects unknown columns, so drop that column before importing an
old file (a text editor, `csvcut -C notes`, or `cut`). A `tm export` run after
the upgrade no longer writes the column.

## If a ledger skips the ALTER

Nothing breaks: the tool never reads or writes `aliquot.notes` any more, so a
spec-7 ledger with the old column keeps working (the column just holds stale
text). The one thing that does not happen is the counter bump: `tm schema
apply` sees the stale meta and writes spec 8, while the unused column stays
until someone drops it with the `ALTER` above. Do not drop it while a spec-7
(spec 6 or older) tool still reads the ledger: those releases expect the column
to exist.

## Rollback

Restore the backup directory, or reset to the restore point made in step 3:

```sh
dolt --data-dir LEDGER reset --hard pre-migrate-spec-7
```

That brings back both the column and the spec-7 counter; the meta-refresh commit
is on top of the tag, so it is discarded too. The spec-8 tool also opens a restored spec-7 ledger without the
`ALTER` (it never names the column), but the ledger will report stale meta
until a `tm schema apply` records spec 8 again. Only the backup makes the
*dropped text* itself recoverable.

## Why there is no script

`tm schema plan` cannot express a core-column drop (it diffs kind tables, views
and core CHECKs, not every core column), and the epic settled on a manual step
for this one. Writing a script that bypasses the planner would add a second
DDL path to maintain, and there is no data transformation to automate: the
advice is "export the text you care about, then drop the column".
