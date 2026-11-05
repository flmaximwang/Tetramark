# base 4 → 5 — the status sets leave the base preset

Counter: `ledger_meta.preset_versions.base` 4 → 5.

**This step is manual.** There is no `migrate.py` in this folder: the counter is
written by `tm schema apply`, which records the version of the preset copy the
ledger holds. The shared procedure — replacing the copies, remapping
`aliquot.status`, adding the `CHECK`, recording the `migrated:` events and
committing — is in [`migration/spec/5-to-6/README.md`](../../spec/5-to-6/README.md).

## What changes

- The top-level `status_sets:` block is gone: the `tube`, `cryo` and
  `animal` sets are no longer declared by any preset.
- The `lot` stock kind no longer names a `statuses:` set — every
  aliquot uses the fixed set (`pending`, `stored`, `missing`,
  `consumed`).
- The description no longer promises "the status sets every stock
  kind names".

Nothing else changes: the kinds, the fields, the rules and the box
geometries are the same as in base 4.

## Preconditions

- `ledger_meta.spec_version = 5` and `ledger_meta.preset_versions.base = 4`.
  The spec step does the row work and writes the counters; this README only
  covers the files (see
  [`migration/spec/5-to-6/README.md`](../../spec/5-to-6/README.md)).
- Every **pending historical** preset step runs first: the frozen scripts under
  `migration/presets/` target spec 5 and install the spec-5 copies.
- A backup of the ledger directory exists (the spec step's step 1).

## Manual procedure

1. Replace the ledger's stored copy of this preset with the frozen v5 file
   (files, not Dolt content). Run it from the root of the Tetramark checkout:

   ```sh
   LEDGER=/path/to/ledger
   cp migration/presets/base/4-to-5/base-5.yaml "$LEDGER/presets/base.yaml"
   ```

   The copy is frozen next to this README on purpose: this step installs
   **base 5**, and the shipped `presets/base.yaml` moves on — it is at version 6
   now ([`5-to-6`](../5-to-6/README.md) drops the slotless box capacities).
   Copying the checkout's file here would install v6 behind a v5 counter.

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
grep -n "status_sets:\|statuses:" "$LEDGER/presets/base.yaml"     # expected: empty
# the counter (after the spec step's tm schema apply)
dolt sql -q "SELECT \`value\` FROM \`ledger_meta\` WHERE \`key\` = 'preset_versions'"
# the plan is up to date and nothing fails
uv run tm -l "$LEDGER" schema plan
uv run tm -l "$LEDGER" check
```

Expected: the grep finds nothing, `preset_versions` names `base: 5` (the
[`5-to-6`](../5-to-6/README.md) step moves it to 6 afterwards), the plan
reports no pending steps and `tm check` prints no `FAIL`.

## Rollback

- The preset copy is a **file**: copy the old `<ledger>/presets/base.yaml`
  back from the backup (the spec step's step 1).
- The counter lives in Dolt: `dolt reset --hard <commit before tm schema
  apply>` restores it.
- Do both. A copy and a counter that disagree make the ledger load a preset it
  does not claim to have.

See also [`migration/README.md`](../../README.md).
