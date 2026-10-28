# 23 · Aliquots are generic tubes: remove `aliquot_fields`, show aliquot counts in `v_stock_<kind>` (spec 3)
Depends on: 21, 22 · Spec: §1–§3, §5, views section · Issue: git-bug `2f6a0d5`

Goal: settle git-bug `2f6a0d5` ("Wrong aliquot schema") by making the model explicit. An aliquot is
a **generic tube**: location, quantity, unit, status, label, notes. Everything specific to a type of
material belongs on the stock kind, because tubes that differ in a property are different stocks. The
per-stock view becomes the place to see what each stock still has.

## Decisions (settled, do not re-open)

1. **Remove `aliquot_fields` entirely**: from the schema model, the loader, DDL (`aliquot_<stockkind>`
   tables), the diff planner (the extension rename in `_plan_renames` and the extension scope), object
   ops (`objects.py` coercion and the extension-table insert/read paths), CSV import/export columns,
   the `tm schema`/`tm ledger` JSON payloads, and the spec. A preset or local kind that still declares
   `aliquot_fields` fails to load, with a message pointing to `migration/spec/2-to-3/README.md`.
2. **Keep one shared `aliquot_event`.** Reasons, for the spec: nothing in an event depends on the kind;
   per-kind tables would repeat this issue's maintenance cost on every new or renamed kind; one table
   gives one ordered timeline for `tm history` and one FK for `purge`; filtering by kind is a join.
   Add this rationale to the spec in one short paragraph.
3. **No `v_aliquot_<kind>` views.**
4. **Every `v_stock_<kind>` gains these columns**, inserted before the final `alias_list`:
   - `entity_kind`, `entity_name` (from core `entity.kind` / `entity.name`), so a reader sees which
     design the stock belongs to;
   - `aliquots_stored` (count with status `stored`: what is still on the shelf), `aliquots_total`;
   - `quantity_stored` + `unit`: sum of stored quantities when all stored tubes with a quantity share
     one unit, otherwise both NULL;
   - `status`: the same derived status as `v_stock_status` (active / pending / depleted / missing /
     empty). Share one SQL fragment with `_view_stock_status` instead of duplicating the CASE.
5. `SPEC_VERSION = 3`.

Migration step `migration/spec/2-to-3/` (through ticket 21's helper):

- If any `aliquot_<kind>` extension table still exists **and holds a non-NULL value**, refuse. List the
  tables and columns and tell the user to move those properties to the stock kind first, with a
  preset/local step like molbio 1-to-2's `glycerol_pct` move. Empty or all-NULL extension tables are
  dropped.
- Recreate all generated views, bump `spec_version` to 3.

## Files

- `src/tetramark/schema/{model.py,loader.py,ddl.py,diff.py}`, `src/tetramark/core/objects.py`,
  `src/tetramark/ops/io_csv.py`, `src/tetramark/cli/{schema.py,ledger.py,csv_io.py,aliquot.py,stock.py}` (as needed)
- `migration/spec/2-to-3/{README.md,migrate.py}`
- tests: drop or rewrite the `aliquot_fields` tests, add view-column tests and the 2-to-3 migration test
- `docs/spec.md` (§1 object table, kind-table list, views, the `aliquot_event` rationale, the YAML
  example that shows `aliquot_fields`), `docs/migration.md`, `skills/tetramark/SKILL.md`, `README.md`

## Constraints

- No behaviour change to `aliquot_event`, IDs, `tm aliquot use/move/status/purge`, or `v_aliquot_location`.
- Real `dolt`, no mocks. Do not commit; leave the tree dirty. Do not touch real ledgers.

## Verify

- A molbio ledger with a stock holding 3 aliquots (2 stored with 1 ml and 0.5 ml, 1 consumed) shows
  `aliquots_stored=2, aliquots_total=3, quantity_stored=1.5, unit='ml', status='active'` plus
  `entity_kind`/`entity_name` in `v_stock_<kind>`. With mixed units, `quantity_stored` and `unit` are
  NULL. A stock with no aliquots shows `status='empty'`, counts 0.
- A kind YAML with `aliquot_fields` fails to load with the pointer message.
- `spec/2-to-3`: an all-NULL extension table is dropped and the views gain the columns. A populated one
  makes the step refuse with nothing written.
- `grep -rn aliquot_fields src/ docs/spec.md skills/` returns nothing, except the loader's rejection
  message and the migration README.
- Full suite green.
