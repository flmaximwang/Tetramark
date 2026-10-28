# 30 · One view per box, not per box kind (spec 5)
Depends on: 25 (wave 10 lands first) · Issue: git-bug `20b350c` "Box View Specification" · Follow-up: git-bug
"Box schemas (templates)" (out of scope here)

Goal: every box that exists gets its own view, which shows what is physically in it. Per-kind box views
go away; nobody looks things up by box kind. A tube that is no longer in stock is no longer in a box,
so the views never show tubes that are gone.

## Decisions (settled with the user 2026-10-28, do not re-open)

### Which views exist

- One view per **existing box**: `` `v_box_<box name>` `` (e.g. `` `v_box_WFL-10-01` ``; always quoted, box
  names contain `-`).
- The `v_box_<kind>` views are **dropped** (all of them: `grid9x9`, `grid8x12`, `rack`, `shelf`,
  `freezer`, `drawer`, `box_slotless`, local kinds). `tm export` of boxes reads `box` joined with
  `box_<kind>` instead of the kind view (`ops/io_csv.py`).
- Unchanged: `v_box_slots`, `v_aliquot_location`, `v_stock_status`, `v_entity_<kind>`, `v_stock_<kind>`.
- A view lists only the box's **direct** contents, never the contents of boxes inside it.

### What a view looks like (by geometry type)

- **grid** (`grid9x9`, `grid8x12`): a pivot. First column `row`, then one column per column label
  (`1`…`9`, `1`…`12`). One row per row label, in geometry order (A–H, J for grid9x9), **empty rows
  included**. A cell is the aliquot id; several aliquots in one slot (`slot_capacity` > 1) are joined
  with `,`; an empty cell is NULL. No extra columns.

  ```
  row | 1          | 2          | … | 9
  A   | 1026GN02.1 | 1026FP02.1 | … | 1026K608.1
  B   | 1026K609.1 | 1026K610.1 | … | NULL
  ```

- **named** (`rack`): `slot | occupants`, one row per slot in geometry order, empty slots included
  (`occupants` NULL). Occupants are aliquot ids or child box names, joined with `,`. The user accepted
  this shape to try in real use; expect it to change later.
- **slotless** (`freezer`, `shelf`, `drawer`, `box_slotless`): one column `occupant` (aliquot id or child
  box name), one row per direct occupant, sorted. An empty box returns **zero rows**.

### Rule changes

- **Grid boxes hold tubes only.** `tm box add --in` / `tm box move --in` into a grid box is refused, and
  a generated `tm check` rule flags existing rows. (What other kinds may hold is decided with box
  schemas, not here.)
- **Rename `grid96` → `grid8x12`** (rows A–H, cols 1–12) in `presets/base.yaml`, with ticket 22's rename
  mechanism (`renames.yaml` → diff planner rewrites the table, constraints and `kind` values). Bump
  `base`'s `version:` and add `migration/presets/base/N-to-N+1/`.
- **Position follows in stock.** An aliquot has `box_id` (and `slot` in a slotted box) **exactly when**
  its status has `in_stock: true` in its stock kind's set (ticket 25). So:
  - changing to a status that is not in stock (`tm aliquot status`, `tm aliquot use` reaching 0, import
    updates) sets `box_id`/`slot` to NULL in the same write; the `aliquot_event` row keeps the old
    position in `from_box_id`/`from_slot`, so `tm history` still shows where it was;
  - changing to an in-stock status without a position is refused, with a hint to run
    `tm aliquot move ALQ --box … --slot …`;
  - `tm aliquot move` of an aliquot that is not in stock also sets its status to the set's in-stock
    status (refuse if the set has more than one, e.g. `fly`, and ask for `--status`);
  - a generated `tm check` rule flags rows that break this (replaces "stored in a slotted box ⇒ slot
    NOT NULL").
- **Box names must work as view names.** `tm box add`, CSV import and any rename path refuse a name that
  (a) makes `v_box_<name>` equal a core view (`slots`, `aliquot_location`, …, and any future core
  `v_box_*`), (b) is longer than 58 characters (64 − `v_box_`), or (c) differs only in case from an
  existing box name. A generated `tm check` rule flags existing boxes that break it.

### How views are kept in step

- Every path that creates, renames or deletes a box (`tm box add`, `tm import`, any future rename/delete)
  creates, replaces or drops the box's view **in the same Dolt commit**. Today only `add` and `import`
  exist; there is no box rename or delete command.
- `tm schema plan/apply` reconciles per-box views from the `box` table (create missing, drop orphans,
  replace changed). The planner today derives views from the schema only (`_desired_views`) and
  recognises managed views by `_VIEW_RE` (`^v_(entity|stock|box)_[a-z]…`), which does not match box
  names like `WFL-10-01`: box views need their own desired set built from live `box` rows, and
  `_managed_view` must treat any `v_box_*` that is not a core view as managed.
- `tm check` reports per-box views that are missing, orphaned or out of date (e.g. after hand-written SQL
  in Workbench or a branch merge).
- `tm box show` stays as is.

### Versions and upgrade

- Generated view shape changes → `SPEC_VERSION` 4 → 5 (ticket 25 takes 4). Step
  `migration/spec/4-to-5/`: drop the `v_box_<kind>` views, create one view per existing box, clear
  positions of aliquots that are not in stock (one `move` event per cleared row, old position in
  `from_box_id`/`from_slot`, `to_*` NULL, note `spec 4-to-5`; the README lists them), and refuse (listing rows) if a box name
  breaks the naming rule or a grid box holds a box.
- Real ledgers (zsqlab10): `--dry-run` first and the user reviews it; write on a Dolt branch; merge only
  after the user's OK; never push to `origin` without the user's explicit OK. Not part of this ticket's
  work; the agent does not touch real ledgers.

## Files

- `src/tetramark/schema/{ddl.py,diff.py,model.py,tests_gen.py}` (per-box view generation, managed-view
  detection, check rules)
- `src/tetramark/core/objects.py` (box add, aliquot status/use/move position rules, name rule, grid
  tube-only), `src/tetramark/ops/io_csv.py` (export without kind views; import keeps views and rules)
- `src/tetramark/cli/{aliquot.py,box.py}` (`--status` on move, messages)
- `presets/base.yaml` (`grid8x12`, version bump), `migration/presets/base/N-to-N+1/`, `migration/spec/4-to-5/`
- tests (new: each view shape, empty grid row, zero-row slotless view, add/import keep views, check drift,
  name rule, grid refuses a box, position cleared on missing/consumed/use-to-0, stored without position
  refused, move restores status, grid96 rename, 4-to-5 step)
- `docs/spec.md` (§3 views, §3 enforcement list, box commands, aliquot commands), `skills/tetramark/SKILL.md`,
  `README.md`

## Constraints

- Real `dolt`, no mocks. Do not commit; leave the tree dirty. Do not touch real ledgers.
- Box schemas (templates in `presets/boxes/`, geometry per box, what each kind may hold) are a separate
  issue; don't start them here.

## Verify

- A ledger with boxes `G` (grid9x9, tubes in A1, B2), `R` (rack, `G` in slot 1) and `S` (box_slotless, 2
  tubes) and an empty freezer `F` has exactly the views `v_box_G`, `v_box_R`, `v_box_S`, `v_box_F` plus
  the core and entity/stock kind views; no `v_box_<kind>`.
- `` SELECT * FROM `v_box_G` `` returns 9 rows (A–H, J) with columns `row, 1…9`; A1 and B2 hold the ids.
  `v_box_R` returns 10 rows with `1 | G`. `v_box_S` returns 2 rows. `v_box_F` returns 0 rows.
- `tm box add grid9x9 slots` and a 59-character name are refused; `tm box move G --in G2` (G2 a grid) is
  refused.
- `tm aliquot status X consumed` → `X.box_id` NULL, the event has the old box and slot, `v_box_G` shows
  NULL there. `tm aliquot status X stored` without a position is refused; `tm aliquot move X --box G
  --slot A1` sets `stored`.
- Dropping a per-box view by hand makes `tm check` fail and `tm schema apply` recreate it.
- `tm export` / `tm import` round-trips boxes without kind views.
- `spec/4-to-5` on a spec-4 fixture produces the views above and passes `tm check`.
