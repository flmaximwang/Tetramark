# proteins 2 → 3 — the status sets leave the proteins part

Counter: `ledger_meta.preset_versions.proteins` 2 → 3.

**This step is manual.** There is no `migrate.py` in this folder: the counter is
written by `tm schema apply`, which records the version of the preset copy the
ledger holds. The shared procedure — replacing the copies, remapping
`aliquot.status`, adding the `CHECK`, recording the `migrated:` events and
committing — is in [`migration/spec/5-to-6/README.md`](../../spec/5-to-6/README.md).

## What changes

- The `protein_prep` stock kind no longer names `statuses: tube` — every
  aliquot uses the fixed set (`pending`, `stored`, `missing`,
  `consumed`).
- The `tube` set it used is gone from base as well (see
  [`base/4-to-5`](../../base/4-to-5/README.md)).

Nothing else changes: the kinds, the fields, the rules and the IUPAC
sequence patterns are the same as in proteins 2.

## Preconditions

- `ledger_meta.spec_version = 5` and `ledger_meta.preset_versions.proteins = 2`.
  The spec step does the row work and writes the counters; this README only
  covers the files (see
  [`migration/spec/5-to-6/README.md`](../../spec/5-to-6/README.md)).
- Every **pending historical** preset step runs first: the frozen scripts under
  `migration/presets/` target spec 5 and install the spec-5 copies.
- A backup of the ledger directory exists (the spec step's step 1).

## Manual procedure

1. Replace the ledger's stored copy of this preset with the **frozen v3 copy**
   in this folder (files, not Dolt content). Do **not** use the checkout's
   `presets/proteins.yaml`: proteins is at v4 now, and this manual step must
   keep installing the content whose counter it writes
   (`migration/README.md`'s retrofit rule). Run it from the root of the
   Tetramark checkout:

   ```sh
   LEDGER=/path/to/ledger
   cp migration/presets/proteins/2-to-3/proteins-3.yaml "$LEDGER/presets/proteins.yaml"
   ```

   The v4 step ([`../3-to-4/README.md`](../3-to-4/README.md)) runs **after**
   the spec step's `tm schema apply`, once the counter says 3.

2. Replace **every** other preset copy the ledger holds in the same go — a
   closure that mixes the old and the new model does not load. The spec step's
   README lists the copies and the check that finds a leftover.

3. There is **no Dolt change in this step**: the counter is written by
   `tm schema apply` (step 5 of the spec step), which records the version of
   the copy you just installed. The rows — the status remap, the `migrated:`
   events and the `CHECK` — are the spec step's work.

## Verification

```sh
LEDGER=/path/to/ledger
# the vocabulary is gone from the copy the ledger holds
grep -n "status_sets:\|statuses:" "$LEDGER/presets/proteins.yaml"     # expected: empty
# the counter (after the spec step's tm schema apply)
dolt sql -q "SELECT \`value\` FROM \`ledger_meta\` WHERE \`key\` = 'preset_versions'"
# the plan is up to date and nothing fails
uv run tm -l "$LEDGER" schema plan
uv run tm -l "$LEDGER" check
```

Expected: the grep finds nothing, `preset_versions` names `proteins: 3`, the
plan reports no pending steps and `tm check` prints no `FAIL`.

## Rollback

- The preset copy is a **file**: copy the old `<ledger>/presets/proteins.yaml`
  back from the backup (the spec step's step 1).
- The counter lives in Dolt: `dolt reset --hard <commit before tm schema
  apply>` restores it.
- Do both. A copy and a counter that disagree make the ledger load a preset it
  does not claim to have.

See also [`migration/README.md`](../../README.md).
