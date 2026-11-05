# base 5 → 6 — the slotless box kinds stop bounding their own contents

Counter: `ledger_meta.preset_versions.base` 5 → 6.

**This step is manual.** There is no `migrate.py` in this folder: nothing in the
database changes, so there is no row work and no SQL for a runner to do. The
step replaces the ledger's stored copy of `base` with the frozen
[`base-6.yaml`](base-6.yaml) and lets `tm schema apply` write the counter, the
fingerprint and the regenerated `dolt_tests` in one commit.

## What changes

- The `shelf`, `freezer` and `drawer` box kinds no longer declare
  `geometry.capacity` (12 / 6 / 1). They are slotless and unbounded like
  `incubator`: how many racks sit on a shelf, how many shelves a freezer has
  and how many boxes a drawer takes is a property of **that piece of
  furniture**, not of the kind.
- Their descriptions no longer promise a count ("holds up to twelve racks or
  boxes" → "racks or boxes sit on it directly").
- Nothing else changes: the kinds, the fields, the rules, the `grid9x9` /
  `grid8x12` geometries and the ten named `rack` slots are exactly those of
  base 5.

Why this needs a counter: the capacity was **enforced on writes**. A base-5
ledger refuses the (N+1)th tube or child box of a slotless box with
`box 'X' is full (capacity N aliquots + child boxes)` — a 7-shelf freezer, a
13-box shelf or a two-box drawer could not be recorded at all. A read-only
change bumps no counter (`migration/README.md`); a change that relaxes a write
check does, because two ledgers claiming `base: 5` would otherwise accept
different data.

`capacity` is never stored: `tm box show --json` reports `geometry.capacity`
as `null` for every box, and `tm schema plan` treats adding or removing it as a
no-op plus a meta refresh. So this step moves no DDL and no stored values —
only the copy the ledger loads.

## Preconditions

- `ledger_meta.spec_version = 6` and `ledger_meta.preset_versions.base = 5`.
  A base-4 ledger runs [`presets/base/4-to-5`](../4-to-5/README.md) first; a
  spec-5 ledger runs [`migration/spec/5-to-6`](../../../spec/5-to-6/README.md)
  first.
- Every **pending historical** preset step has run: this step assumes the
  closure is already status-free and at spec 6.
- A backup of the ledger directory exists (`tm backup` or a filesystem copy) —
  the preset copy is a file and is not in Dolt history.

## Manual procedure

Run it from the root of the Tetramark checkout that ships `base` version 6.

1. Replace the ledger's stored copy with the frozen v6 file:

   ```sh
   LEDGER=/path/to/ledger
   cp migration/presets/base/5-to-6/base-6.yaml "$LEDGER/presets/base.yaml"
   ```

2. No other copy changes: the closure is the same set of parts and only `base`
   bumps. (The `mouse` preset keeps its `cage` at `capacity: 5` — a cage's
   occupant limit is a rule of the kind, not of one cabinet.)

3. Read the plan, then apply it:

   ```sh
   uv run tm -l "$LEDGER" schema plan    # expected: schema is up to date; ledger_meta is stale
   uv run tm -l "$LEDGER" schema apply   # one commit: "apply(schema): refresh ledger_meta"
   ```

   `schema apply` writes `preset_versions` (`base: 6`), the fingerprint,
   `tetramark_version` and the `ledger.yaml` copy from the files the ledger now
   holds. It has no DDL step to run and touches no row.

Nothing else is needed: no box is created, moved or deleted, and a box that the
old bound refused is simply recordable from now on.

## Verification

```sh
LEDGER=/path/to/ledger
# the bound is gone from the copy the ledger holds
grep -n "capacity:" "$LEDGER/presets/base.yaml"          # expected: empty
# ... and from the effective schema it loads
uv run tm -l "$LEDGER" schema show shelf | grep -n "capacity"   # expected: empty
# the counter
dolt sql -q "SELECT \`value\` FROM \`ledger_meta\` WHERE \`key\` = 'preset_versions'"
# nothing pending, nothing failing
uv run tm -l "$LEDGER" schema plan
uv run tm -l "$LEDGER" check
```

Expected: both greps find nothing (a `mouse` ledger still shows `capacity: 5`
for `cage`), `preset_versions` names `base: 6`, the plan reports no pending
step and `tm check` prints no `FAIL`.

## Rollback

- The copy is a **file**: put the v5 content back — either the backup or
  [`../4-to-5/base-5.yaml`](../4-to-5/base-5.yaml), which is the base 5 the
  previous step installs.
- The counter lives in Dolt: `dolt reset --hard <commit before tm schema
  apply>` restores it.
- Do both. A copy at v6 with a counter at v5 (or the reverse) makes the ledger
  load a preset it does not claim to have, and the old write bound comes back
  without warning if the copy is the only thing restored.

See also [`migration/README.md`](../../README.md) and the base entry in
[`presets/README.md`](../../../presets/README.md).
