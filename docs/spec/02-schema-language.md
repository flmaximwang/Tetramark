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
      construct: {type: ref, to: protein_construct}  # FK → entity_protein_construct
  bacterial_glycerol:
    object: stock
    entity_kinds: [plasmid, bacterial_strain]
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

---
