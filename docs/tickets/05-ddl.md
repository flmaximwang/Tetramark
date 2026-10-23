# 05 · DDL & view generation
Depends on: 03  · Spec: §3, §2.1, §2.2

`schema/ddl.py`: pure functions from `EffectiveSchema` → ordered list of SQL statements (no DB access):
- core tables exactly as §3 (status CHECK from vocabulary), with stable constraint names (`fk_<table>_<col>`, `ck_…`).
- kind tables with constant discriminator column + `FK(id,kind)`; field SQL per §2.1 (sequence → sha256 generated
  column + optional UNIQUE; enum → CHECK; ref → FK to `<object>_<kind>(id)` or core table; computed → STORED generated).
- `aliquot_<stockkind>` extension tables.
- views `v_box_slots`, `v_aliquot_location` (recursive CTE for box path — verify Dolt 2.3.5 supports it; fall back
  to fixed-depth joins (depth 6) if not), `v_stock_status` (derivation rules §3), `v_<object>_<kind>`.
- `slot_rows(box_id, geometry)` → rows to insert into `slot` when a box is created.
- Statement objects tagged with `object` (table/view/fk/…) and `name` so ticket 09 can diff.

Tests: generate for the test presets from 03 and **execute against a real dolt repo** (EmbeddedDriver from 02 is
not yet guaranteed — use plain `dolt sql` subprocess in the test); assert tables/views exist, discriminator FK
rejects an id in two kind tables, enum CHECK rejects bad value, sequence UNIQUE rejects duplicate, slot FK rejects
a slot not in geometry. Golden-file snapshot of the DDL for one preset.
