# 11 · Read side: find, box show, ls, history
Depends on: 10  · Spec: §6

- `ops/find.py`: `find(query)` → exact id (any object) → alias → name/text LIKE across entity names and
  string/text fields of kind tables; ranked results with object type, kind, id, summary, location.
  Behaviour reference: `<legacy-zsqlab-checkout>/find_entity.sh` and `test_find_entity.py` (read only).
- `ops/render.py`: `box show` grid via rich (rows × cols, cell = aliquot id or child box name, status colouring,
  `A,B` when capacity>1), list form for named/slotless; `--json` shape `{box, kind, geometry, path, slots:[{label,row,col,occupants:[…]}]}`.
- `ledger history ID`: aliquot_event rows + `dolt_log`/`dolt_diff_<table>` entries touching the id.
- Shared output formatter for table/json/csv used by all ls/show commands (refactor 10's output to use it).

Tests: find precedence and multi-hit ranking; grid render snapshot for grid9x9 with capacity 1 and 2; json shape;
history shows create/move/use in order.
