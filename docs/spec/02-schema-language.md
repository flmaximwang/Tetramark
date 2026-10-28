# 2. Schema language (YAML)

A **preset** is a single YAML file (`presets/<name>.yaml`) containing kinds, mixins, and rules; a
local preset may instead be a directory, optionally with `kinds/` and `rules/` files. Presets can
`include` other presets. A ledger's effective schema = union of its presets + local kinds; name
collisions are an error.

A preset meant to be shared is a **part**: it owns one slice of the model and includes only the
parts it references — never a bundle. A preset whose only content is `include:` is a **bundle**
(`molbio` is one): it adds no kinds, rules or status sets, and includes are loaded once each, so two
bundles can list the same part and one ledger can list `presets: [molbio, cellbio]` without
duplicate-kind errors. `presets/README.md` maps every part to the bundles that include it and
records the include graph.

```yaml
# presets/nucleic_acids.yaml (a part: proteins supplies protein_construct and
# the sequence_fields mixin; base supplies named_fields and the status sets)
preset: nucleic_acids
version: 1
include: [base, proteins]
kinds:
  plasmid:
    object: entity
    mixins: [named_fields, sequence_fields]
    fields:
      backbone: {type: string}
      resistance: {type: enum, values: [amp, kan, cm, spec, tet]}
      construct: {type: ref, to: protein_construct, lineage: true}  # FK → entity_protein_construct; a lineage edge
  plasmid_prep:
    object: stock
    entity_kinds: [plasmid]
    statuses: tube               # required on every stock kind
    fields:
      concentration_ng_ul: {type: decimal, precision: 12, scale: 2}
      prep_method: {type: string}
  grid81:                       # a box kind may live in any part (base has the
    object: box                 # bundled ones); geometry is §2.2
    geometry: {type: grid, rows: "A-J!I", cols: "1-9"}   # A..J skipping I
rules:
  - id: MB01
    description: every plasmid with a construct has a sequence
    severity: fail
    sql: "SELECT COUNT(*) FROM entity_plasmid WHERE construct IS NOT NULL AND sequence IS NULL"
    expect: {single_value: 0}
```

Mixins are global too: a mixin declared in one part can be used by a kind in another part that
includes it — `sequence_fields` lives in `proteins` because that is the deepest part both
`protein_construct` and `plasmid` load.

### 2.1 Field types

| type | SQL | notes |
|---|---|---|
| `string` | `VARCHAR(n)` (default 255, `length:`) | |
| `text` | `TEXT` / `MEDIUMTEXT` | not indexable |
| `int` | `BIGINT` | |
| `decimal` | `DECIMAL(p,s)` (default 18,6) | |
| `bool` | `TINYINT(1)` | |
| `date`, `datetime` | `DATE`, `DATETIME` | |
| `enum` | `VARCHAR` + `CHECK (col IN (...))` | adding a value is additive |
| `sequence` | `MEDIUMTEXT` + generated `<col>_sha256 CHAR(64) STORED` | `unique: true` → UNIQUE on the sha column; values normalised (uppercase, whitespace stripped) by the app |
| `ref` | `VARCHAR(64)` + FK | `to:` a kind name (FK → that kind table) or a core object (`entity`/`stock`/`aliquot`/`box`) |
| `computed` | generated column `STORED` | `expr:` SQL over same-row columns, `sql_type:` required |

Modifiers: `required` (NOT NULL), `unique`, `default`, `pattern` (regex, enforced by app and by a
generated Dolt test — Dolt CHECK has no REGEXP guarantee), `description`, `length`.

`lineage: true` marks a **`ref`** field as a descent edge for `tm lineage` (§6): `plasmid.construct` (in
`nucleic_acids`) and `cell_line.parental` (in `cell_culture`) are the bundled examples. It is valid only on `type: ref`, on entity and stock
kinds, and the target must be an entity kind, a stock kind or the core `entity`/`stock`/`aliquot` — a
ref to a box is a load error. The flag changes no DDL and is read-only, but it **is** part of the schema
fingerprint and appears in `tm schema show`; a preset that adds or removes it, like any other
read-only change, needs no `version:` bump and no migration step (§5).

### 2.2 Box geometry

| type | spec | slots |
|---|---|---|
| `grid` | `rows`, `cols` as ranges (`"A-H"`, `"1-12"`, `"A-J!I"` = exclude I) or explicit lists; `label: "{row}{col}"` default | materialised row×col |
| `named` | `slots: [top, middle, bottom]` | materialised list |
| `slotless` | optional `capacity: N` (max aliquots + child boxes) | none; slot is NULL |

Optional on box kind: `slot_capacity` (default 1).

### 2.3 Rules

Custom rule = `id`, `description`, `severity: fail|info`, `sql` (read-only SELECT), `expect`:
`{single_value: X}` | `{rows: 0}` | `{rows_at_most: N}`. Compiled into `dolt_tests` (§4).

### 2.4 Status sets

Statuses belong to **stock kinds**, not to the ledger (spec 4). A preset or a
ledger `kinds/` file may declare top-level `status_sets`, and every stock kind
names one with `statuses:` (or gives an inline set); entity and box kinds must
not have it. Set names are global, like kind names; a duplicate name is a load
error, and every `to:` target must be a status of the same set.

```yaml
status_sets:
  tube:
    pending:  {in_stock: false, to: [stored, missing, consumed]}
    stored:   {in_stock: true,  to: [missing, consumed]}   # on the shelf
    missing:  {in_stock: false, to: [stored, consumed]}
    consumed: {in_stock: false}                            # no to: = final
```

`in_stock` decides what the stock views count as still in stock. Creating an
aliquot (`tm stock add`, `tm aliquot add`, `tm import`) accepts any status of
the stock kind's set; **changing** one must follow `to:`. `tm aliquot status …
--force --note TEXT` overrides a rule and records `aliquot_event.forced = TRUE`;
an unforced event that breaks a rule fails `tm check` (§4).

The bundled sets live in `base`: `tube` (general tubes: pending/stored/missing
and a final consumed), `cryo` (frozen vials: stored/missing/thawed) and `animal`
(living organisms: planned/alive/…). Every existing stock kind uses `tube`;
`cell_culture`'s `cell_vial` uses `cryo`.
