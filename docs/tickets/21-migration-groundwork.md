# 21 · Migration groundwork: `migration/` layout, renames hook, `spec/1-to-2`
Depends on: — · Spec: §5 (schema evolution), §ledger format version · Issue: git-bug `2f6a0d5`

Goal: one simple, durable place for upgrade steps. Each step is keyed by **the version counter that
changed**, not by the Tetramark release. Every ledger already records both counters in `ledger_meta`:
`spec_version` (core format, `SPEC_VERSION` in `schema/model.py`) and `preset_versions`
(e.g. `{"base": 1, "molbio": 1}`).

## Decisions (settled, do not re-open)

Layout:

```
migration/
  README.md
  spec/<N>-to-<N+1>/            README.md, migrate.py
  presets/<name>/<N>-to-<N+1>/  README.md, migrate.py, renames.yaml (optional)
```

- **Maintenance rule** (state it at the top of `migration/README.md`): whoever bumps `SPEC_VERSION` or a
  preset's `version:` adds exactly one folder for that step, in the same change.
- Each step is run by hand:
  `uv run migration/<…>/migrate.py --ledger PATH [--branch B] [--dry-run]`. There is no new `tm`
  command. The `tm upgrade` work in `docs/issues/001-tm-upgrade.md` stays separate. Add a short note to
  that issue saying `migration/` holds the first real steps and can be absorbed later.
- Script contract, implemented once in a shared helper (`src/tetramark/migrate.py`, imported by every
  `migrate.py`):
  1. open the ledger, read the relevant counter, and refuse unless it equals `N`;
  2. `--dry-run` prints every SQL statement and writes nothing;
  3. apply → exactly one Dolt commit, message `migrate(ledger): spec N -> N+1` or
     `migrate(ledger): <preset> N -> N+1`. Bump the counter, `tetramark_version` and the fingerprint
     in the same commit;
  4. create the tag `pre-migrate-<spec|preset>-<N>` before writing;
  5. Dolt DDL is not transactional. On failure, print the last good commit and the
     `dolt reset --hard` command.
- Each step's `README.md`: what changes, preconditions, **manual steps** (e.g. hand-written rule SQL),
  a `grep` command that finds leftovers in a ledger's `kinds/` and `rules/`, and verification
  (`tm check` with no FAIL).
- **Renames hook:** a preset step may ship `renames.yaml`:

  ```yaml
  entity: {construct: protein_construct}
  stock:  {bacterial: bacterial_glycerol}
  fields: {stock.bacterial_glycerol: {old_col: new_col}}   # optional
  ```

  Feed it to the existing diff planner (`schema/diff.py`, `_plan_renames` / `_plan_column_renames`) as
  an explicit rename map, an alternative to `renamed_from`. Presets will **not** carry
  `renamed_from`. Reuse the existing table rename, `aliquot_<kind>` rename and `kind`-value rewrite;
  do not hand-write SQL for these.

First real step, `migration/spec/1-to-2/`: spec 2 (commit `861faae`, release 0.1.2) added `alias_list`
to the generated views. zsqlab10 is still at spec 1. Implement this step: recreate the generated views
(and whatever else 861faae changed in stored shape; check that commit), then bump to 2. Also check
what the current `tm` does when it opens a spec-1 ledger, and record that in the step README.

## Files

- `migration/README.md`, `migration/spec/1-to-2/{README.md,migrate.py}`
- `src/tetramark/migrate.py` (new helper), `src/tetramark/schema/diff.py` (rename-map parameter)
- `docs/issues/001-tm-upgrade.md` (one-paragraph note), `docs/spec.md` (one paragraph pointing to `migration/`)
- tests: `tests/test_migrate.py` (new)

## Constraints

- Independent of ticket 20, so it can run in parallel. Don't depend on where presets live. Take
  the ledger path as input.
- Real `dolt`, no mocks. Do not commit; leave the tree dirty. Do not touch real ledgers under
  `/Users/maxim/Documents/Lab/Dolt`.

## Verify

- Test: init a ledger, force `spec_version=1` and the old views (or build the fixture from the
  `861faae^` DDL), run `spec/1-to-2` → views have `alias_list`, `spec_version=2`, one commit, tag
  present, `tm check` clean. A second run refuses (counter ≠ 1).
- Test: a rename map given to the planner renames a kind table and rewrites core `kind` values, with no
  data loss, matching what `renamed_from` does today.
- `--dry-run` writes nothing (Dolt HEAD unchanged).
