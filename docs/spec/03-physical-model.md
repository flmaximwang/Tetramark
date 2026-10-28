# 3. Physical model (generated DDL)

Core tables (always present, created by `tm init`):

```
ledger_meta(key PK, value)                       -- schema hash, preset versions, ledger.yaml copy, spec version
entity(id PK, kind, name, created_at, notes, UNIQUE(id,kind))
stock(id PK, kind, entity_id FK→entity, parent_stock_id FK→stock, source_aliquot_id FK→aliquot,
      created_at, notes, UNIQUE(id,kind))
box(id PK, name UNIQUE, kind, parent_box_id FK→box, parent_slot, created_at, notes, UNIQUE(id,kind))
slot(box_id FK→box, label, row_label, col_label, ordinal, PK(box_id,label))
aliquot(id PK, stock_id FK→stock, seq, box_id FK→box, slot, status CHECK IN vocab,
        quantity DECIMAL, unit, label, created_at, notes, UNIQUE(stock_id,seq),
        FK(box_id,slot)→slot)
aliquot_event(id AUTO PK, aliquot_id FK, event ENUM(create,use,move,status,edit), amount, unit,
              from_box_id, from_slot, to_box_id, to_slot, old_status, new_status,
              actor, purpose, note, at)
alias(object_type, object_id, alias, PK(object_type, alias))
```

`ledger_meta` also carries the version stamps:

- `spec_version` is the **ledger format version** — the key an upgrade pipeline
  dispatches on. It is bumped only when the generated shape changes: the core
  table set, the `ledger_meta` key set, or generated objects (views,
  `dolt_tests`). A pure bug-fix release does not change it. Version 2 adds the
  `alias_list`/`occupant_alias_list` columns to the generated views. Version 3
  removes the `aliquot_<stockkind>` extension tables (an aliquot is a generic
  tube; the per-stock-kind tube-field key no longer loads) and adds the
  entity/aliquot/quantity/status columns to every `v_stock_<kind>`. A **v2**
  ledger upgrades to v3 with the hand-run `migration/spec/2-to-3/` step; a v1
  ledger runs `migration/spec/1-to-2` first, and a molbio-1 ledger also runs
  `migration/presets/molbio/1-to-2`, which needs the last tool version that
  still reads `aliquot_fields` (a 0.2.0+ tool cannot open such a ledger at
  all). See `migration/README.md`; within one version `tm schema plan` →
  `tm schema apply` stays additive.
- `tetramark_version` records which tool version last wrote the metadata
  (`tm init` and every `tm schema apply` refresh it).
- `created_with` records the tool version that first formatted the ledger
  (`tm init`/`tm init --adopt`); it never changes afterwards.
- `adopted_from_commit` is written only by `tm init --adopt`: the HEAD hash of
  the adopted database before formatting.

`tetramark_version`, `created_with` and `adopted_from_commit` are provenance
records: they are shown by `tm schema show` but drive no migration logic.

### 3.1 Adopting an existing database (`tm init --adopt`)

`tm init NAME --adopt --preset …` formats a Dolt database that already exists in the workspace:
it performs the same steps as `tm init` (write `ledger.yaml`, create the core and kind tables, write
`ledger_meta`, install the generated `dolt_tests`) but skips `mkdir`/`dolt init` and commits
`adopt(ledger): NAME` on top of the existing history instead of starting a new root commit.
`ledger_meta.adopted_from_commit` records the branch HEAD from *before* that commit; the rest of the
history is untouched, so old commits stay reachable (e.g. `AS OF <old tag>`).

Preconditions — any failure refuses the adopt and writes nothing:

- the selected branch's working set is clean (`dolt_status` has no rows);
- that branch has **no user tables** (no `information_schema.tables` BASE TABLE outside `dolt_*`),
  **no views**, and **no `dolt_tests` rows**;
- the ledger directory has no `ledger.yaml`, or `--force-config` was given (it is overwritten, never
  deleted).

Adopt runs on the branch selected by `-b/--branch` (default: the checked-out branch), so a migration
can format a branch while `main` keeps the old schema. A branch that does not exist is an error — it is
never created. Adopt never drops an existing object: dropping the legacy schema is a separate,
auditable commit made before the adopt.

A failed adopt rolls back only its own writes: it drops the tables/views it had created (Dolt's
`DOLT_RESET --hard` leaves untracked tables behind, so a plain reset is not enough), resets the branch
to the pre-adopt HEAD, restores or removes `ledger.yaml`, and leaves the working set clean. It must
**never** remove the database directory: unlike a failed `tm init`, which deletes its own half-created
directory, adopt operates on a real database and answering a failure with `rmtree` would destroy it.

`tm --dry-run init NAME --adopt` runs the read-only preconditions (including the legacy-schema refusal) and
prints the branch, the HEAD that would be recorded, and the DDL; nothing is written.

Kind tables (generated from schema):
- `entity_<kind>(id PK, kind CHAR const DEFAULT '<kind>' CHECK(kind='<kind>'), <fields…>,
  FK(id,kind)→entity(id,kind))` — the discriminator FK: an id can belong to only one kind table.
- `stock_<kind>(…)` and `box_<kind>(…)` likewise.

Views (generated):
- `v_box_slots` — one row per slot (empties included): box name, slot, occupant type/id, stock, entity, status,
  and `occupant_alias_list` (the occupant's aliases: the aliquot's aliases when the occupant is an
  aliquot, the box's aliases when it is a nested box, NULL for an empty slot).
- `v_aliquot_location` — aliquot with full box path (`Freezer-1/Shelf-2/WFL-08-01:A3`) and `alias_list`.
- `v_stock_status` — stock + derived status (`active` if any aliquot stored; `pending` if any pending
  and none stored; `depleted` if all consumed; `missing` if all missing/consumed with ≥1 missing; `empty` if 0 aliquots) + aliquot counts + total quantity per unit + `alias_list`.
- `v_<object>_<kind>` — kind table joined with its core row (convenience for humans/Workbench). Every
  `v_stock_<kind>` additionally shows, in this order: `entity_kind`/`entity_name` (the owning design,
  from core `entity`), `aliquots_stored` (status `stored`: what is still on the shelf),
  `aliquots_total`, `quantity_stored` + `unit` (the sum of the stored quantities; `unit` is that unit
  when every stored tube **with a quantity** shares one, otherwise both are NULL), `status` (the same
  derived status as `v_stock_status`, generated from one shared SQL fragment). The final column is
  always `alias_list`.

**Aliquots have no kind.** An aliquot is a generic tube — location, quantity, unit, status, label,
notes — and everything specific to a type of material lives on the stock kind (tubes that differ in a
property are different stocks, spec §1). There is deliberately **one shared `aliquot_event` table**:
nothing in an event depends on the kind, so per-kind tables would repeat this issue's cost on every new
or renamed kind; one table gives one ordered timeline for `tm history` and one FK for `tm aliquot
purge`, and filtering by kind is a join. No `v_aliquot_<kind>` views exist either: `v_aliquot_location`
already covers every tube and the stock views show the kind-derived facts.

Every view that represents an object aggregates the `alias` table into a sorted, `;`-separated
`alias_list` (NULL when the object has no aliases; `v_box_slots` names it `occupant_alias_list`
because the row describes a slot and its occupant, not only the box). The column is deliberately
**not** called `aliases`: a kind may already have a domain field named `aliases` — molbio's
`protein_construct` kind does, holding semicolon-separated alternative construct names — and a view cannot
expose two columns with the same name. After a `tm stock renumber` the old stock and aliquot ids are
aliases (§6), so these columns are how a view reader still sees the pre-renumber handles.

What the DB enforces vs the app:
- DB: PK/UNIQUE/FK/CHECK/NOT NULL, discriminator FKs, slot existence (`FK(box_id,slot)→slot`), enum values.
- App + Dolt tests (cannot be expressed as DB constraints): slot capacity & aliquot-vs-child-box exclusivity,
  nesting acyclicity, stock→entity kind restriction, `pattern`, status/slot consistency
  (`stored` in a slotted box ⇒ slot NOT NULL), slotless box capacity.

No triggers (Dolt 2.3.x triggers are single-statement only; all logic lives in the app).
