# Wave 11 delivery record (ticket 30, release 0.4.0)

**Date**: 2026-10-29 · **Scope**: one view per box (spec 5) · **Source issue**: git-bug `20b350c`
("Box View Specification"; box schemas/templates, git-bug `d16a3bd`, stay out of scope) · **Design**:
`docs/issues/002-box-views.md`, retired with this wave · **Plan**:
[`plans/wave11/plan.json`](../../plans/wave11/plan.json) (loomerto, three views next to it).

All work is merged on `main`; `spec_version` is **5**, `base` is **4**, the release is **0.4.0**. The
wave also closed the one open item of wave 10 (the legacy fixture drift guard).

## What shipped

| # | Change | Merge commit |
|---|---|---|
| 1 | Per-box views replace `v_box_<kind>`: one generated view per **existing box** (`v_box_<box name>`) with the grid-pivot, named and slotless shapes; box names must be view-safe; grid boxes hold tubes only; `tm schema plan/apply` reconciles per-box views from the live `box` rows and `tm check` reports drift; `SPEC_VERSION` 5 | `5ab0a69` |
| 2 | Box lifecycle and read paths: `tm box add`/CSV import create the box's view in the same Dolt commit; `tm box ls`, `tm find` and `tm export` read `box JOIN box_<kind>`; position follows in stock (`tm aliquot status/use/move`, `--status` on move, creation included); CSV import enforces the rules | `e643581` |
| 3 | `migration/spec/4-to-5` plus a checked-in spec-4 fixture (`tests/data/ledgers/spec4-boxes`) and its builder: refuses view-unsafe box names, a grid box holding a child box and an in-stock aliquot without a position; clears the positions of not-in-stock aliquots with one `move` event each; rebuilds the views and `dolt_tests` | `4a14ec3` |
| 4 | `base` 4: `grid96` renamed to `grid8x12` (unchanged A–H × 1–12 geometry) via the rename map, `migration/presets/base/3-to-4`; the earlier wave-10 preset steps carry a frozen `base-3` target copy; spec 03/05/06/08, README, skills and the preset catalogue updated | `c3f13f8` |
| 5 | Wave-10 follow-up: the frozen molbio-1 preset inputs are back to their v1 bytes and the builder fails fast on later-spec keys, so `test_molbio_v1_fixture_matches_its_builder` runs and passes instead of skipping | `dc3498f` |

## Decisions worth remembering

- **An aliquot has a position exactly when it is in stock.** That includes creation: creating an
  in-stock tube without a position is refused (pointing at `tm aliquot move`), leaving stock clears
  the position, and `move` restores the kind's single in-stock status (asking for `--status` when the
  set has several). `spec/4-to-5` therefore **refuses** an in-stock aliquot without a position —
  it never guesses a box — and clears the positions of not-in-stock aliquots, keeping the old
  position on the `move` event (`note = 'spec 4-to-5'`).
- **Per-box views are data, not schema.** `ddl.view_statements` only carries the schema-derived
  views (core + `v_entity_<kind>`/`v_stock_<kind>`); `ddl.box_view_statements(schema, boxes)` builds
  one view per live box, and `diff.build_plan` reconciles them from the live `box` rows (create,
  replace, drop orphans). A box kind the loaded schema does not know is a refusal, not a guess; a
  kind known only through a `RenameMap` (the `grid96 → grid8x12` case) is translated first.
- **Dolt lowercases view names.** The view is physically `v_box_wfl-10-01`; queries stay
  case-insensitive, and plan/check compare view names lowercased. Box names keep their case.
- **A box name is a view name**: `v_box_<name>` must not equal a core view, the name is at most 58
  characters (64 minus the prefix), and two box names may not differ only in case. Exact duplicates
  keep the existing "name in use" refusal.
- **Grid boxes hold tubes only** — no child box (the app refuses, `tm check` flags existing rows and
  the migration refuses); a grid box may itself be nested, e.g. in a rack slot. Named and slotless
  views list a child box by name as an occupant; every view lists only direct contents.
- **`base` 4 is a pure rename.** The geometry and every other base kind stay; the step rewrites the
  kind table name, constraints and the core `kind` values through `renames.yaml`. The existing
  per-box views do not mention the kind table, so their shape does not change.
- **Upgrade order.** A spec-4/base-3 ledger (0.3.0) runs `migration/spec/4-to-5` first — it keeps the
  ledger's own base-v3 copy so the `grid96` boxes are visible while the per-box views are created —
  then `migration/presets/base/3-to-4`. Older ledgers put the matching preset steps in between;
  `tests/test_migrate.py::test_spec_3_to_4_then_molbio_2_to_3_is_the_zsqlab10_path` runs the full
  chain (`spec/3-to-4 → spec/4-to-5 → base/2-to-3 → molbio/2-to-3 → cell_culture/1-to-2 →
  base/3-to-4`).
- **Historical preset steps keep working from this checkout** because the four later wave-10 steps
  (`base/2-to-3`, `molbio/2-to-3`, `cell_culture/1-to-2`, `library/1-to-2`) now install a frozen
  `base-3.yaml` over the repository copy, the retrofit rule `migration/README.md` states.
- **Historical step tests and the spec-5 rules.** Old-step tests regenerate `dolt_tests` from the
  current schema, so their fixtures place aliquots (or seed pre-spec-5 tubes as `pending`) instead of
  relaxing the position rule; `test_migrate.py`'s call fixtures state that explicitly.

## Verification

- **Full suite on `main` after the last merge** (`0b1a175`): **891 passed, 0 failed, 0 skipped**
  (real Dolt, `-n 4`, 957 s). Fast lane (`-m fast`): 352 passed.
- **Preset acceptance** (fresh workspace, repository presets): `tm init` + `tm check` +
  `tm schema plan` for `molbio` (30 checks), `cellbio` (31), `angenet` (34) and `[molbio, cellbio]`
  (34); every plan reported *schema is up to date*.
- **Box acceptance** on a scratch base-4 ledger with `G` (grid9x9, tubes in A1/B2), `H` (grid8x12,
  tube in A1), `R` (rack holding `G` in slot 1), `S` (shelf with two tubes) and an empty freezer `F`:
  - the views are exactly `v_box_G`, `v_box_H`, `v_box_R`, `v_box_S`, `v_box_F` plus the core and
    entity/stock kind views — no `v_box_<kind>`;
  - `v_box_G` has columns `row, 1…9` and 9 rows (A–H, J; A1 and B2 filled); `v_box_H` has columns
    `row, 1…12` and 8 rows (A–H); `v_box_R` has 10 rows with `1 | G`; `v_box_S` has 2 rows;
    `v_box_F` has 0 rows;
  - `tm box add grid8x12 slots`, a 59-character name and a case-only duplicate are refused, as is
    `tm box move F --in H` (a grid box);
  - `tm aliquot status X consumed` clears `box_id`/`slot` and the event keeps the old position in
    `from_box_id`/`from_slot`; `tm aliquot status X stored` without a position is refused with the
    `tm aliquot move` hint; that move restores `stored`;
  - dropping `v_box_G` by hand fails `tm check` (`core_box_views`) and `tm schema apply` recreates it;
  - `tm export` / `tm import` round-trips the ledger (15 rows; imported boxes get their views, check
    clean, plan up to date).
- **Migration end to end** on a copy of `tests/data/ledgers/spec4-boxes`: `spec/4-to-5` then
  `base/3-to-4` gives spec 5 / base 4, `box_grid96` renamed to `box_grid8x12`, box kinds
  `grid8x12`/`grid9x9`/`rack`/`shelf`/`freezer`, the per-box views unchanged in shape, `tm check`
  17/17 and `tm schema plan` up to date; each step commits once with its `pre-migrate-*` tag.
- **No real ledger was touched.** zsqlab10 stays at spec 4 / base 3 until the user runs the steps.

## Upgrade order for an existing ledger

For a 0.3.0 ledger (spec 4, base 3 — e.g. zsqlab10):

1. back up the ledger directory;
2. `migration/spec/4-to-5/migrate.py --ledger PATH --dry-run`, review (it lists the positions it
   would clear and refuses unsafe box names, grid boxes holding boxes, and in-stock aliquots without
   a position), then run it without `--dry-run` — on a Dolt branch for a real ledger;
3. `migration/presets/base/3-to-4/migrate.py --ledger PATH` (same dry-run habit): `grid96` becomes
   `grid8x12`;
4. `tm check` and `tm schema plan` (the plan must be empty).

A ledger at spec 3 runs `migration/spec/3-to-4` first; a base-2/molbio-2/cell_culture-1/library-1
ledger runs those preset steps between `spec/4-to-5` and `base/3-to-4` (the ordering test above is
the reference). Pre-0.2.0 ledgers still need their era checkout, as
[`docs/guides/migrating-a-legacy-ledger.md`](../guides/migrating-a-legacy-ledger.md) explains.

## Open items

1. **Real zsqlab10 upgrade to spec 5 / base 4**: not part of this wave. Run it on a branch with
   `--dry-run` first, with the user's OK, and never push `origin` without explicit consent.
2. **Box schemas / templates** (git-bug `d16a3bd`): a separate issue; not started. Grid boxes holding
   tubes only is the current rule.
3. **`tm upgrade`** ([`docs/issues/001-tm-upgrade.md`](../issues/001-tm-upgrade.md)): still a
   proposal; migration steps remain hand-run scripts.
