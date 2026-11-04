# zebrafish 1 → 2 — the status sets leave the zebrafish part

Counter: `ledger_meta.preset_versions.zebrafish` 1 → 2.

**This step is manual.** There is no `migrate.py` in this folder: the counter is
written by `tm schema apply`, which records the version of the preset copy the
ledger holds. The shared procedure — replacing the copies, remapping
`aliquot.status`, adding the `CHECK`, recording the `migrated:` events and
committing — is in [`migration/spec/5-to-6/README.md`](../../spec/5-to-6/README.md).

## What changes

- The `fish_group` and `fish_clutch` (were `animal`) and the
  `fish_sperm_cryo` (was `cryo`) stock kinds no longer name a status
  set — every aliquot uses the fixed set (`pending`, `stored`,
  `missing`, `consumed`).
- The `animal` and `cryo` sets they used are gone from base as well
  (see [`base/4-to-5`](../../base/4-to-5/README.md)).

Nothing else changes: the kinds, the fields and the rules are the same
as in zebrafish 1.

## The new optional stock property

The detailed state of a fish group or a clutch (`euthanized`,
`found_dead`, `transferred`, `cancelled`) is no longer a status. The part
adds one **optional** enum stock property to `fish_group` and
`fish_clutch` (`fate`) that carries it. It is **not backfilled**: a
migrated group keeps `NULL` until the operator fills it in, and the old
state stays in the aliquot history (see the spec step's README).

## Preconditions

- `ledger_meta.spec_version = 5` and `ledger_meta.preset_versions.zebrafish = 1`.
  The spec step does the row work and writes the counters; this README only
  covers the files (see
  [`migration/spec/5-to-6/README.md`](../../spec/5-to-6/README.md)).
- Every **pending historical** preset step runs first: the frozen scripts under
  `migration/presets/` target spec 5 and install the spec-5 copies.
- A backup of the ledger directory exists (the spec step's step 1).

## Manual procedure

1. Replace the ledger's stored copy of this preset with this checkout's copy
   (files, not Dolt content). Run it from the root of the Tetramark checkout
   that has the new presets:

   ```sh
   LEDGER=/path/to/ledger
   cp presets/zebrafish.yaml "$LEDGER/presets/zebrafish.yaml"
   ```

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
grep -n "status_sets:\|statuses:" "$LEDGER/presets/zebrafish.yaml"     # expected: empty
# the counter (after the spec step's tm schema apply)
dolt sql -q "SELECT \`value\` FROM \`ledger_meta\` WHERE \`key\` = 'preset_versions'"
# the plan is up to date and nothing fails
uv run tm -l "$LEDGER" schema plan
uv run tm -l "$LEDGER" check
```

Expected: the grep finds nothing, `preset_versions` names `zebrafish: 2`, the
plan reports no pending steps and `tm check` prints no `FAIL`.

## Rollback

- The preset copy is a **file**: copy the old `<ledger>/presets/zebrafish.yaml`
  back from the backup (the spec step's step 1).
- The counter lives in Dolt: `dolt reset --hard <commit before tm schema
  apply>` restores it.
- Do both. A copy and a counter that disagree make the ledger load a preset it
  does not claim to have.

See also [`migration/README.md`](../../README.md).
