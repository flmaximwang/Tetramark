# spec 5 → 6 — one fixed aliquot status set

Counter: `ledger_meta.spec_version` 5 → 6.

**This step is manual.** There is no `migrate.py` in this folder: the change
touches the ledger's *files* (the preset copies and the local kinds) as much as
its rows, and the row change is a vocabulary remap that an operator has to
review. The counter itself is written by `tm schema apply` (step 5), which
refreshes `spec_version`, `preset_versions`, the fingerprint and the derived
`dolt_tests` in **one** commit.

## What changes

Spec 6 (ticket `Tetramark-epn`) gives every aliquot **one fixed, hardcoded
status set** — `pending`, `stored`, `missing`, `consumed` — that every stock
kind shares (`docs/spec/01-concepts.md` §1.1). Nothing in the schema declares
statuses any more:

1. **The status vocabulary leaves the schema.** A top-level `status_sets:` key
   or a stock kind's `statuses:` key is a **load error** in spec 6. The
   ledger's own copies under `<ledger>/presets/` (and any local `kinds/` file)
   must be replaced with the status-free copies of this checkout — the whole
   closure at once, because a closure that mixes both models does not load.
2. **`aliquot.status` gets a real DB `CHECK`.** The generated
   `core_aliquot_status_known` test (which read the per-kind sets) is gone; the
   database itself now refuses a status outside the four names.
3. **One transition rule for every kind.** The generated
   `core_status_event_allowed` test is global. It only judges an event whose
   *two* endpoints are fixed names, so the old vocabulary that stays in the
   history is ignored and never fails `tm check`.
4. **`in_stock` is `status = 'stored'`** for every stock kind — the stock views
   no longer consult a schema set.

What does **not** change: the core tables, the generated views, the box
geometry, quantities, units, lineages. Only the status vocabulary and the files
that declared it.

## Preconditions

- `ledger_meta.spec_version = 5`. A ledger that is already at spec 6 is done;
  a ledger below spec 5 must run the earlier steps first
  (`migration/spec/4-to-5/README.md` and the preset steps below it).
- **Every pending historical preset step runs first.** The frozen step scripts
  under `migration/presets/` target **spec 5**: they install the spec-5 preset
  copies and their plans assume the spec-5 shape. Run them *before* this step
  (e.g. a base-3 ledger runs `migration/presets/base/3-to-4/` first), because
  after this step their frozen copies would be a second, mixed closure. The
  order is: **preset steps → this step**.
- A clean working set (`dolt status` empty) and a backup. The restore point is
  the Dolt commit before step 3 (plus the ledger's own files) — see
  *Rollback* below.
- The ledger's own `<ledger>/presets/` copies are the **spec-5** ones (they
  still carry `status_sets:`/`statuses:`). This step replaces them; it does not
  merge them.
- Real lab ledgers: work on a Dolt branch (`--branch tetramark-upgrade`), merge
  only after the operator's OK, and never push to `origin` without that OK.

## The old → new status map

Every status of the old per-kind vocabularies maps to one fixed name. A word
that already is a fixed name keeps it (no row changes, no event).

| old status | new status | old vocabulary |
|---|---|---|
| `planned` | `pending` | `animal` |
| `alive` | `stored` | `animal`, `fly` |
| `crossed` | `stored` | `fly` |
| `growing` | `stored` | `worm` |
| `starved` | `stored` | `worm` |
| `lost` | `missing` | `fly` |
| `thawed` | `consumed` | `cryo` |
| `euthanized` | `consumed` | `animal` |
| `found_dead` | `consumed` | `animal` |
| `transferred` | `consumed` | `animal` |
| `cancelled` | `consumed` | `animal` |
| `discarded` | `consumed` | `fly`, `worm` |
| `contaminated` | `consumed` | `worm` |
| `pending`, `stored`, `missing`, `consumed` | unchanged | `tube` |

`thawed` means the same as `consumed`: a thawed cryo vial is gone. A cryo vial
is `stored` while it is in the freezer and `missing` or `consumed` after it
leaves; there is no separate cryo vocabulary.

The detailed states the old words carried (euthanized, starved, contaminated,
…) are **stock properties or event messages** now. They are **not backfilled**
here: a stock's aliquots may disagree about them, and the operator decides per
stock. The new optional enum properties this checkout's presets add (for
example `fate` on an animal stock, `condition` on a worm culture) stay empty
until the operator fills them in.

## The procedure

Do the steps in this order. Steps 1–2 are files, steps 3–4 are rows, step 5 is
the commit that records the counters and step 6 verifies the result.

The `dolt sql` commands below run in the ledger directory (or against the
`dolt sql-server` that serves it).

### 1. Back up the ledger

```sh
LEDGER=/path/to/ledger
cp -a "$LEDGER" "${LEDGER}.bak-$(date +%Y%m%d)"     # the whole directory
```

The ledger directory is self-contained (Dolt database + `ledger.yaml` +
`presets/` + `kinds/`), so copying the directory is the backup.

### 2. Replace the preset copies (files)

The ledger's own copies are files, not Dolt content. Copy the status-free
copies of **this checkout** over them — every preset the ledger holds (that is
the include closure of its `presets:` list), in one go: a closure that mixes
the old and the new model does not load.

Run this from the root of the Tetramark checkout that has the new presets:

```sh
LEDGER=/path/to/ledger
for file in "$LEDGER"/presets/*.yaml; do
  name=$(basename "$file")
  if [ -f "presets/$name" ]; then
    cp "presets/$name" "$file" && echo "replaced $name"
  else
    echo "no repository copy for $name — remove or replace it by hand"
  fi
done
```

The ledger's `ledger.yaml` names the presets it loads (a bundle names its
parts too, through `include:`); every one of them has a copy in
`<ledger>/presets/`.

**A part that bumped again after spec 6.** The loop installs this checkout's
current copy, which is right for every part whose counter the spec step writes.
A part with a *newer* preset step must first land the version that step moves
from — otherwise the step's rename never runs. Today that is `proteins`: v4
(`migration/presets/proteins/3-to-4/README.md`) splits the sequence mixin and
renames `length_bp` to `length_aa`. Place the frozen v3 copy after the loop,
let step 5 write `proteins: 3`, then run the v4 step:

```sh
cp migration/presets/proteins/2-to-3/proteins-3.yaml \
   "$LEDGER/presets/proteins.yaml"
uv run migration/presets/proteins/3-to-4/migrate.py --ledger "$LEDGER"
```

The v4 step runs **after** step 5 (`tm schema apply`): it refuses a ledger
whose spec is still 5. It also needs `base: 6`, so it runs after
`migration/presets/base/5-to-6/` — and therefore after
`migration/spec/6-to-7/`, which that base step requires.

**A part that bumped again after spec 6 — `base`.** The loop installs this
checkout's `presets/base.yaml`, which is **version 6**: the capacity-free copy
that `migration/presets/base/5-to-6/README.md` upgrades to. Leaving it would
skip the 4 → 5 counter (a base-4 ledger would never record `base: 5`) and turn
`base/5-to-6` into a step that can never run (it expects `base: 5`) — the
ordered 4 → 5 → 6 upgrade would not be real. Put the frozen v5 copy back after
the loop, so step 5 writes `base: 5`:

```sh
cp migration/presets/base/4-to-5/base-5.yaml "$LEDGER/presets/base.yaml"
```

After this step's apply and `migration/spec/6-to-7/`, the operator runs
`migration/presets/base/5-to-6/` to record `base: 6`. The frozen
`base/4-to-5/base-5.yaml` is the v5 content a base-4 ledger already installed;
restoring it here keeps every ledger's copy and counter in step, and leaves the
two base steps independent of this one.

Check the result — the expected output is empty:

```sh
grep -rn "status_sets:\|statuses:" "$LEDGER/presets" "$LEDGER/kinds"
```

A ledger with local kinds (`kinds_dir:` in `ledger.yaml`) must get the same
treatment: **delete** the `status_sets:` key and every `statuses:` key from
those files, and — optionally — add the new enum stock properties this
checkout's presets define (for example `fate` on a mouse cohort, a fish group
or a fly culture, `condition` on a worm culture). A local kind that keeps a
`statuses:` key makes the closure mixed and the ledger does not open.

```sh
grep -rn "status_sets:\|statuses:" "$LEDGER/kinds"   # expected: empty
```

### 3. Remap `aliquot.status` and record the migrated events

Two statements, **in this order**, in one session:

1. the **event insert** — it reads the row's *current* status, which is still
   the old word;
2. the **update** — it writes the new word.

First look at what has to change — the rows the remap touches:

```sh
dolt sql -r csv -q "SELECT status, COUNT(*) FROM aliquot GROUP BY status ORDER BY status"
```

Every remapped aliquot gets **one** `status` event: `old_status` is the old
word, `new_status` the mapped one, and the note says where it came from
(`migrated: <old status>`). **Past events are never rewritten** — the history
keeps the vocabulary it was written with.

```sql
-- 1. one migrated event per remapped aliquot
INSERT INTO `aliquot_event`
  (`aliquot_id`, `event`, `old_status`, `new_status`, `actor`, `note`)
SELECT `id`, 'status', `status`,
       CASE `status`
         WHEN 'planned'      THEN 'pending'
         WHEN 'alive'        THEN 'stored'
         WHEN 'crossed'      THEN 'stored'
         WHEN 'growing'      THEN 'stored'
         WHEN 'starved'      THEN 'stored'
         WHEN 'lost'         THEN 'missing'
         WHEN 'thawed'       THEN 'consumed'
         WHEN 'euthanized'   THEN 'consumed'
         WHEN 'found_dead'   THEN 'consumed'
         WHEN 'transferred'  THEN 'consumed'
         WHEN 'cancelled'    THEN 'consumed'
         WHEN 'discarded'    THEN 'consumed'
         WHEN 'contaminated' THEN 'consumed'
       END,
       '<your name>', CONCAT('migrated: ', `status`)
FROM `aliquot`
WHERE `status` IN ('planned', 'alive', 'crossed', 'growing', 'starved', 'lost',
                   'thawed', 'euthanized', 'found_dead', 'transferred',
                   'cancelled', 'discarded', 'contaminated');

-- 2. the remap itself
UPDATE `aliquot` SET `status` = 'pending'  WHERE `status` = 'planned';
UPDATE `aliquot` SET `status` = 'stored'   WHERE `status` IN ('alive', 'crossed', 'growing', 'starved');
UPDATE `aliquot` SET `status` = 'missing'  WHERE `status` = 'lost';
UPDATE `aliquot` SET `status` = 'consumed' WHERE `status` IN ('thawed', 'euthanized', 'found_dead', 'transferred', 'cancelled', 'discarded', 'contaminated');
```

`<your name>` is the `actor` — the ledger author, the same name `tm` records on
its own events (or leave it `NULL`).

The events of the old vocabulary that are already in the table stay as they
are. The generated `core_status_event_allowed` rule only judges an event whose
two endpoints are fixed names, so `alive > crossed` in the history is ignored.

### 4. Add the DB `CHECK`

```sql
ALTER TABLE `aliquot` ADD CONSTRAINT `ck_aliquot_status` CHECK (`status` IN ('pending', 'stored', 'missing', 'consumed'));
```

Run it **after** step 3: the constraint is checked against the rows that exist
when it is added, so a ledger that still holds `alive` would be refused.
(Skip this step and let `tm schema apply` add it — the plan then contains the
same statement as `add_constraint aliquot.ck_aliquot_status`.)

### 5. Plan, then apply (the one commit)

```sh
uv run tm -l "$LEDGER" schema plan      # review: only the CHECK (if any) + meta
uv run tm -l "$LEDGER" schema apply     # one Dolt commit
```

`tm schema apply` writes, in that one commit:

- `ledger_meta.spec_version` → `6`;
- `ledger_meta.preset_versions` → the versions of the copies you installed in
  step 2 — `base: 5` and `proteins: 3` from the two frozen copies the
  exceptions restore (this is how the preset counters are bumped — see the
  preset step READMEs);
- `ledger_meta.fingerprint` and `ledger_meta.tetramark_version`;
- the regenerated `dolt_tests`: the fixed `core_status_event_allowed` rule
  replaces `core_aliquot_status_known`.

After it, `tm schema plan` reports no pending steps.

### 6. Verify

```sh
LEDGER=/path/to/ledger
# 1. only the four statuses are left
dolt sql -r csv -q "SELECT status, COUNT(*) FROM aliquot GROUP BY status ORDER BY status"
# 2. the CHECK is there
dolt sql -q "SELECT constraint_name FROM information_schema.table_constraints
             WHERE table_schema = DATABASE() AND constraint_type = 'CHECK'
               AND table_name = 'aliquot'"      # ck_aliquot_status
# 3. the counters
dolt sql -q "SELECT \`value\` FROM \`ledger_meta\` WHERE \`key\` = 'spec_version'"   # 6
dolt sql -q "SELECT \`value\` FROM \`ledger_meta\` WHERE \`key\` = 'preset_versions'"
# 4. one migrated event per remapped aliquot, old history untouched
dolt sql -r csv -q "SELECT old_status, new_status, note, COUNT(*) FROM aliquot_event
                     WHERE note LIKE 'migrated: %' GROUP BY old_status, new_status, note"
# 5. the plan is empty and no check fails
uv run tm -l "$LEDGER" schema plan     # no pending steps
uv run tm -l "$LEDGER" check           # no FAIL
```

Expected: only `pending`/`stored`/`missing`/`consumed` in query 1, the
constraint in query 2, `6` in query 3, one `migrated: <old>` row per remapped
aliquot in query 4, an empty plan and no `FAIL` in query 5.

## Rollback

- **Before step 5** (nothing is committed yet): the ledger's Dolt content is
  still at the pre-migration commit; undo the row changes with
  `dolt reset --hard <commit before step 3>` and put the old preset copies
  back from the backup of step 1.
- **After step 5**: `dolt reset --hard <commit before step 3>` restores the
  rows (the status remap, the events and the CHECK are all in the commits
  after that point), and copying the backed-up `<ledger>/presets/` back
  restores the files. The preset copies are files, not Dolt content — they
  have to be restored by hand.
- A ledger that was reset to the old commit opens again with the old
  vocabulary (the tool only warns that the ledger is older than the tool); an
  older tool (0.7.x) can open it unchanged.

## Caveats

- **The step is not transactional.** Dolt DDL is not: if the `ALTER TABLE` in
  step 4 fails, the statements before it stay in the working set. Check
  `dolt status` and `dolt reset --hard <last good commit>` before retrying.
- **Do not mix the closures.** A ledger whose `presets/` copies are half old
  and half new does not open ("a ledger uses one model"). Replace every copy
  in one go.
- **No property backfill.** The new enum stock properties (`fate`, `condition`,
  …) stay empty; the operator fills them per stock. The old vocabulary that
  carried those states lives on in the event history.
- **The old words stay in the history.** `tm history` still shows `alive`,
  `crossed`, … — that is the point: the past is not rewritten.
