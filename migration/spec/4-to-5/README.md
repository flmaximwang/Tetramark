# spec 4 → 5 — one view per existing box, positions follow in stock

Counter: `ledger_meta.spec_version` 4 → 5. Tag before writing: `pre-migrate-spec-4`.
One commit: `migrate(ledger): spec 4 -> 5`.

## What changes

Spec 5 (ticket 30, git-bug `20b350c`) replaces the per-box-**kind** views with one view per
**existing box**, and makes an aliquot's position follow its stock status. The step changes two
things in one commit:

1. **Views.** Every generated view is dropped and rebuilt in the spec-5 shape:
   - the `v_box_<kind>` views (`v_box_grid9x9`, `v_box_grid96`, `v_box_rack`, `v_box_shelf`,
     `v_box_freezer`, `v_box_drawer`, `v_box_incubator`, and any local box kind's view) are gone;
   - each live `box` row gets its own view `` `v_box_<box name>` `` (name kept verbatim, always
     quoted), showing only the box's **direct** contents:

     | box kind geometry | view shape |
     | --- | --- |
     | grid (`grid9x9`, `grid96`) | pivot: first column `row`, then one column per column label; one row per row label in geometry order (empty rows included); a cell is the aliquot id(s), comma-joined, NULL when empty |
     | named (`rack`) | `slot \| occupants`: one row per slot in geometry order, empty slots included; occupants are aliquot ids or child box names, comma-joined |
     | slotless (`freezer`, `shelf`, `drawer`, `box_slotless`, local kinds) | one column `occupant`: one row per direct occupant, sorted; an empty box has zero rows |

     `v_box_slots`, `v_aliquot_location`, `v_stock_status` and the `v_entity_<kind>` /
     `v_stock_<kind>` views are recreated as well (the generated view set is derived state, so the
     step rebuilds all of it). Only managed views are touched: a hand-written view is left alone,
     but a hand-written view that depends on a dropped `v_box_<kind>` view must be dealt with by
     the operator.
   - the derived `dolt_tests` are regenerated from the spec-5 schema in the same commit, so the new
     rules are installed with the views they check: position-follows-in-stock (both directions, and
     the slot rule for slotted boxes), grid boxes hold no child boxes, box names can become view
     names, and `tm check` reports missing/orphaned/out-of-date per-box views.
2. **Positions.** Spec 4 kept an aliquot's `box_id`/`slot` independently of its status, so a tube
   set to `consumed` still sat in a box. In spec 5 an aliquot has a position **exactly when it is in
   stock**. The step clears `box_id` and `slot` of every not-in-stock aliquot that still carries a
   position, and records **one `move` event per cleared row** so `tm history` still shows where it
   was: `from_box_id`/`from_slot` keep the old position, `to_box_id`/`to_slot` are NULL,
   `old_status` = `new_status` = the current status, `actor` is the ledger author and `note` is
   `spec 4-to-5`. Rows are processed (and events inserted) in aliquot-id order.

## Refusals (nothing is written)

The step refuses **before writing anything** — no tag, no commit, no DDL — in these cases, listing
every offending row:

1. **A box name cannot become a view name** (spec 5's name rule): `` `v_box_<name>` `` would equal a
   core view (case-insensitively; today `v_box_slots`, and any future core `v_box_*`), the name is
   longer than **58** characters, or it differs only in case from another box name. There is no
   `tm box` rename command yet: rename the row in the ledger (or drop/recreate the box) first.
2. **A grid box holds a child box.** Grid boxes hold tubes only. Move the child out first
   (`tm box move CHILD --in PARENT`).
3. **An in-stock aliquot has no position** (`box_id` NULL, or a slotted box without `slot`). Wave
   11's Lead decision is that in stock means physically placed, so such a row would fail the new
   generated position rule immediately; the step never guesses a location. It lists each row with
   the command that fixes it: `tm aliquot move <id> --box NAME --slot S`.
4. **An aliquot status is not in its stock kind's status set.** A genuine spec-4 ledger cannot have
   one (`migration/spec/3-to-4` refuses them); without the set the step cannot tell in stock from
   not in stock, so it refuses instead of clearing a position it does not understand.
5. **A box or stock kind the loaded schema does not define.** The step reads the **ledger's own**
   schema (its `<ledger>/presets/` copy plus its local `kinds/`), so this usually means the ledger's
   own preset copy was replaced with a newer one (e.g. base 4, which has no `grid96`) or a local kind
   file is missing. Restore the spec-4 copy, then retry.

## Preconditions and upgrade order

- `ledger_meta.spec_version = 4` (the script refuses any other value; a second run refuses).
- A clean working set (`dolt_status` empty) and no tag `pre-migrate-spec-4` at another commit.
- The ledger's own `<ledger>/presets/` copy is the **spec-4/base-3** copy (`grid96` is still a grid
  kind, the stock kinds name the `tube` status set), and its local `kinds/` (if any) still define
  every kind its rows use. The step reads the ledger's own copy, not the repository's, so it is safe
  to run before base 3 -> 4.
- **Upgrade order** for a spec-4/base-3 ledger: **this step first, then
  `migration/presets/base/3-to-4`** (the base step requires spec 5). Do not replace the ledger's
  base copy with the repository's base-4 copy before this step. A ledger with other presets runs
  their own `migration/presets/<name>/N-to-N+1/` steps as usual.
- Real lab ledgers: run `--dry-run` first and let the operator review the plan; apply on a Dolt
  branch (`--branch tetramark-upgrade`), merge only after the operator's OK, and never push to
  `origin` without that OK.
- A backup exists as the `pre-migrate-spec-4` tag; work on a branch as well.

## Running it

```sh
# 1. inspect: the tag, the cleared rows, the DDL, the meta bump; writes nothing
uv run migration/spec/4-to-5/migrate.py --ledger /path/to/ledger --dry-run

# 2. apply (one commit, tag first)
uv run migration/spec/4-to-5/migrate.py --ledger /path/to/ledger

# 3. on a branch of a served ledger
uv run migration/spec/4-to-5/migrate.py --ledger /path/to/ledger --branch tetramark-upgrade
```

The dry-run prints every statement, so the cleared positions and the per-box view definitions can be
reviewed before anything is written.

## The cleared rows

The `--dry-run` output contains, per not-in-stock aliquot that still had a position, the `UPDATE`
and the `move` event that keep its history. To list them from a backup copy of the ledger (before the
step) or from the step's event rows (afterwards):

```sh
LEDGER=/path/to/ledger
# before: every not-in-stock aliquot that still has a position
dolt sql -r csv -q "SELECT a.id, s.kind, a.status, b.name AS box, a.slot
                    FROM aliquot a JOIN stock s ON s.id = a.stock_id
                    LEFT JOIN box b ON b.id = a.box_id
                    WHERE a.box_id IS NOT NULL
                      AND a.status NOT IN ('stored')"   # adapt to the ledger's in-stock statuses
# after: what the step did (one move event per cleared row)
dolt sql -r csv -q "SELECT aliquot_id, from_box_id, from_slot, old_status, note
                    FROM aliquot_event WHERE event = 'move' AND note = 'spec 4-to-5'
                    ORDER BY aliquot_id"
```

Their per-box views show the cells/slots as empty after the step; the aliquot history keeps the old
position.

## Verification

```sh
LEDGER=/path/to/ledger
# 1. counter and commit
dolt sql -q "SELECT \`value\` FROM \`ledger_meta\` WHERE \`key\` = 'spec_version'"   # 5
dolt log -n 1                                                                      # migrate(ledger): spec 4 -> 5
dolt tag | grep pre-migrate-spec-4
# 2. the box kind views are gone; one view per box exists
dolt sql -q "SELECT table_name FROM information_schema.tables
             WHERE table_schema = DATABASE() AND table_type = 'VIEW' ORDER BY table_name"
# 3. the shapes: G is a 9-row A-H,J pivot, R 10 rows, S 2 rows, F 0 rows
dolt sql -q "SELECT * FROM \`v_box_G\`"
dolt sql -q "SELECT * FROM \`v_box_F\`"
# 4. no not-in-stock aliquot is still placed (the command returns 0)
dolt sql -q "SELECT COUNT(*) FROM aliquot WHERE box_id IS NOT NULL
             AND status NOT IN ('stored')"
# 5. validation: no FAIL (this also checks the per-box views against the box table)
uv run tm -w /path/to/workspace -l <ledger> check
# 6. the base rename is now the next step (spec 5 required by it):
uv run migration/presets/base/3-to-4/migrate.py --ledger /path/to/ledger --dry-run
```

## Failure, rollback, caveats

- **A refusal is not a failure.** Nothing is written (no tag, no commit, no DDL): fix the rows named
  in the message and retry.
- **Dolt DDL is not transactional.** If a statement fails mid-way, the statements already executed
  stay in the working set; the script makes **no** commit and prints the last good commit plus
  `dolt reset --hard <last-good-commit>`. Dolt leaves *untracked* objects created by the failed
  attempt behind — check `SHOW FULL TABLES` afterwards.
- **Rollback after success:** reset the branch to the tag (`dolt reset --hard pre-migrate-spec-4`)
  and use the older (0.3.0) tool. The migration only changes Dolt content (views, aliquots, events,
  `dolt_tests`, `ledger_meta`); the preset copies under `<ledger>/presets/` are files and are not
  touched, so an older tool can reopen the ledger after the reset.
- **Per-box views are derived state.** A branch merge or hand-written SQL can leave them missing,
  orphaned (a view for a box that no longer exists) or stale; `tm schema plan` recreates/drops them
  and `tm check` fails until it does. That is also why this step recreates every managed view instead
  of only the box kind ones.
- **A box rename is not implemented yet**, so a refused box name can only be fixed by editing the
  `box` row (or dropping/recreating the box) — then run the step again.
