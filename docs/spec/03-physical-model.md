# 3. Physical model (generated DDL)

Core tables (always present, created by `tm init`):

```
ledger_meta(key PK, value)                       -- schema hash, preset versions, ledger.yaml copy, spec version
entity(id PK, kind, name, created_at, notes, UNIQUE(id,kind))
stock(id PK, kind, entity_id FK→entity, parent_stock_id FK→stock, source_aliquot_id FK→aliquot,
      created_at, notes, UNIQUE(id,kind))
box(id PK, name UNIQUE, kind, parent_box_id FK→box, parent_slot, created_at, notes, UNIQUE(id,kind))
slot(box_id FK→box, label, row_label, col_label, ordinal, PK(box_id,label))
aliquot(id PK, stock_id FK→stock, seq, box_id FK→box, slot, status VARCHAR NOT NULL,
        quantity DECIMAL, unit, label, created_at, notes, UNIQUE(stock_id,seq),
        FK(box_id,slot)→slot)                    -- status: its stock kind's set (app + tests)
aliquot_event(id AUTO PK, aliquot_id FK, event ENUM(create,use,move,status,edit), amount, unit,
              from_box_id, from_slot, to_box_id, to_slot, old_status, new_status,
              forced BOOLEAN NOT NULL DEFAULT FALSE, actor, purpose, note, at)
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
  entity/aliquot/quantity/status columns to every `v_stock_<kind>`. Version 4
  moved the status vocabulary out of `ledger.yaml` into per-stock-kind status
  sets, dropped the global `aliquot.status` CHECK, added `aliquot_event.forced`
  and replaced the stock views' `aliquots_stored`/`quantity_stored`/five-value
  `status` with in-stock counts, `quantity_in_stock` + `unit` and a three-value
  `status`. Version 5 replaced the per-kind `v_box_<kind>` views with one
  generated view per existing box, `v_box_<box name>` (§3), and made the stored
  position follow the in-stock status (the hand-run
  `migration/spec/4-to-5/` recreates the views and clears the stored positions
  of not-in-stock aliquots; it refuses a view-unsafe box name, a grid box that
  holds a child box, and an in-stock aliquot without a position). A **v2**
  ledger upgrades to v3 with the
  hand-run `migration/spec/2-to-3/` step; a v3 ledger then runs
  `migration/spec/3-to-4/`, and a v4 ledger `migration/spec/4-to-5/`. A v1
  ledger runs `migration/spec/1-to-2` first, and a molbio-1 ledger also runs
  `migration/presets/molbio/1-to-2`, which needs the last tool version that
  still reads `aliquot_fields` (a 0.2.0+ tool cannot open such a ledger at
  all). Presets have their own counters: a molbio-2 ledger moves to 3 with
  `migration/presets/molbio/2-to-3/` (the preset becomes a bundle of parts and
  gains the oligo kinds), a base-2 ledger to 3 with
  `migration/presets/base/2-to-3/` (the slotless `incubator` box kind), and a
  base-3 ledger to 4 with `migration/presets/base/3-to-4/` (the `grid96` →
  `grid8x12` rename), and its proteins/nucleic_acids to 2 with
  `migration/presets/proteins/1-to-2/` then
  `migration/presets/nucleic_acids/1-to-2/` (IUPAC sequence patterns, after
  base 4 and with no DDL); a bundle step also records the counters of the
  parts it introduces. On a spec-4/base-3 ledger, run `migration/spec/4-to-5/` **before**
  `migration/presets/base/3-to-4/`: the spec step keeps the ledger's own base-3
  copy (and so its `grid96` kind) while it creates the per-box views, then the
  preset step replaces that copy and renames the kind. See
  `migration/README.md`; within one version `tm schema plan` → `tm schema apply`
  stays additive.
- `tetramark_version` records which tool version last wrote the metadata
  (`tm init` and every `tm schema apply` refresh it).
- `created_with` records the tool version that first formatted the ledger
  (`tm init`/`tm adopt`); it never changes afterwards.
- `adopted_from_commit` is written only by `tm adopt`: the HEAD hash of
  the adopted database before formatting.

`tetramark_version`, `created_with` and `adopted_from_commit` are provenance
records: they are shown by `tm schema show` but drive no migration logic.

### 3.1 Adopting an existing database (`tm adopt`)

`tm adopt PATH --preset …` formats an existing bare Dolt database **in place** (the 11 legacy
lab databases of the 0.4 era are just such directories): it performs the same steps as `tm init`
(write `ledger.yaml`, create the core and kind tables, write `ledger_meta`, install the generated
`dolt_tests`) but skips `mkdir`/`dolt init` and commits `adopt(ledger): NAME` on top of the existing
history instead of starting a new root commit. `PATH` is the database directory itself; it is
explicit, so a parent `workspace.yaml` is irrelevant (nothing is searched). `ledger.yaml`'s `name:`
defaults to the directory basename and can be set with `--name`.
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

`tm --dry-run adopt PATH --preset …` runs the read-only preconditions (including the legacy-schema
refusal) and prints the branch, the HEAD that would be recorded, and the DDL; nothing is written.

Kind tables (generated from schema):
- `entity_<kind>(id PK, kind CHAR const DEFAULT '<kind>' CHECK(kind='<kind>'), <fields…>,
  FK(id,kind)→entity(id,kind))` — the discriminator FK: an id can belong to only one kind table.
- `stock_<kind>(…)` and `box_<kind>(…)` likewise.

Views (generated):
- `v_box_slots` — one row per slot (empties included): box name, slot, occupant type/id, stock, entity,
  the occupant stock's `aliquots_in_stock`/`aliquots_total`/`quantity_in_stock` + `unit` and derived
  `status` (NULL when the slot has no aliquot occupant), and `occupant_alias_list` (the occupant's
  aliases: the aliquot's aliases when the occupant is an aliquot, the box's aliases when it is a
  nested box, NULL for an empty slot).
- `v_aliquot_location` — aliquot with full box path (`Freezer-1/Shelf-2/WFL-08-01:A3`) and `alias_list`.
  `tm aliquot show` reads this view for the tube's `path` (§6).
- `v_stock_status` — stock + derived status (`active` when at least one aliquot is in stock;
  `depleted` when it has aliquots but none in stock; `empty` when it has none) +
  `aliquots_in_stock`/`aliquots_total` + `quantity_in_stock` + `unit` + `alias_list`. "In stock" means
  the aliquot's status has `in_stock: true` in its stock kind's status set (§2.4).
- `v_<object>_<kind>` — kind table joined with its core row (convenience for humans/Workbench), for
  every **entity** and **stock** kind. Every
  `v_stock_<kind>` additionally shows, in this order: `entity_kind`/`entity_name` (the owning design,
  from core `entity`), `aliquots_in_stock`, `aliquots_total`, `quantity_in_stock` + `unit` (the sum of
  the in-stock quantities; `unit` is that unit when every in-stock tube **with a quantity** shares one,
  otherwise both are NULL), `status` (the same derived status as `v_stock_status`, generated from one
  shared SQL fragment). The final column is always `alias_list`.
- `v_box_<box name>` — one view per **existing box**, named after the box (quoted, because box names
  may contain `-` and upper case). It lists the box's **direct** contents only, never the contents of
  a box inside it, and its shape follows the box kind's geometry:
  - **grid**: a pivot. First column `row`, then one column per column label in geometry order
    (`1`…`9`, `1`…`12`); one row per row label in geometry order, empty rows included. A cell is the
    aliquot id (several aliquots in one slot, `slot_capacity` > 1, are joined with `,`); an empty cell
    is NULL. No extra columns.
  - **named**: `slot | occupants`, one row per slot in geometry order, empty slots included
    (`occupants` NULL). Occupants are aliquot ids or child box names, joined with `,`.
  - **slotless**: one column `occupant` (an aliquot id or a child box name), one row per direct
    occupant, sorted. An empty box returns **zero rows**.

  Every path that creates or renames a box (`tm box add`, CSV import, `tm box rename`; there is no box
  delete command yet) maintains its view in the same Dolt commit, and
  `tm schema plan/apply` reconciles the views from the live `box` rows (`tm check` reports a view
  that is missing, orphaned or out of date). There are **no `v_box_<kind>` views** any more: one
  view per box replaces them (spec 5).

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
  nesting acyclicity, **grid boxes hold tubes only** (a child box in a grid is refused), stock→entity kind
  restriction, `pattern`, **position follows in stock** (an aliquot has `box_id` exactly when its status
  has `in_stock: true`, and `slot` exactly when that box is slotted; a not-in-stock aliquot carries no
  position), statuses belonging to their stock kind's set, transition rules for unforced `status` events,
  slotless box capacity, and **box names are view-safe**: `v_box_<name>` must not equal a core view,
  the name is at most 58 characters (64 − `v_box_`) and it must not differ only in case from another
  box name.

No triggers (Dolt 2.3.x triggers are single-statement only; all logic lives in the app).
