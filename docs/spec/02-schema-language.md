# 2. Schema language (YAML)

A **preset** is a single YAML file (`presets/<name>.yaml`) containing kinds, mixins, and rules; a
local preset may instead be a directory, optionally with `kinds/` and `rules/` files. Presets can
`include` other presets. A ledger's effective schema = union of its presets + local kinds; name
collisions are an error.

```yaml
# presets/molbio.yaml
preset: molbio
version: 2
include: [base]
status_sets:                     # global set names; usually defined in base
  tube:
    pending: {in_stock: false, to: [stored, missing, consumed]}
    stored: {in_stock: true, to: [missing, consumed]}
    missing: {in_stock: false, to: [stored, consumed]}
    consumed: {in_stock: false}
mixins:
  sequence_fields:
    sequence: {type: sequence, unique: true}
    length_bp: {type: computed, expr: "CHAR_LENGTH(sequence)", sql_type: int}
kinds:
  plasmid:
    object: entity
    mixins: [sequence_fields]
    fields:
      name: {type: string, required: true, unique: true}
      backbone: {type: string}
      resistance: {type: enum, values: [amp, kan, cm, spec, tet]}
      construct: {type: ref, to: protein_construct, lineage: true}  # FK → entity_protein_construct; a lineage edge
  bacterial_glycerol:
    object: stock
    entity_kinds: [plasmid, bacterial_strain]
    statuses: tube               # required on every stock kind
    fields:
      strain: {type: string, required: true}
      medium: {type: string}
      glycerol_pct: {type: decimal, precision: 5, scale: 2, default: 15}
  grid81:
    object: box
    geometry: {type: grid, rows: "A-J!I", cols: "1-9"}   # A..J skipping I
rules:
  - id: MB01
    description: every plasmid with a construct has a sequence
    severity: fail
    sql: "SELECT COUNT(*) FROM entity_plasmid WHERE construct IS NOT NULL AND sequence IS NULL"
    expect: {single_value: 0}
```

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

`lineage: true` marks a **`ref`** field as a descent edge for `tm lineage` (§6): `plasmid.construct` and
`cell_line.parental` are the bundled examples. It is valid only on `type: ref`, on entity and stock
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
molbio's `cell_vial` uses `cryo`.
