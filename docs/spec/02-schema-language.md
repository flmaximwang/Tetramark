# 2. Schema language (YAML)

A **preset** is a single YAML file (`presets/<name>.yaml`) containing kinds, mixins, and rules; a
local preset may instead be a directory, optionally with `kinds/` and `rules/` files. Presets can
`include` other presets. A ledger's effective schema = union of its presets + local kinds; name
collisions are an error.

A preset meant to be shared is a **part**: it owns one slice of the model and includes only the
parts it references — never a bundle. A preset whose only content is `include:` is a **bundle**
(`molbio` is one): it adds no kinds, mixins or rules, and includes are loaded once each, so two
bundles can list the same part and one ledger can list `presets: [molbio, cellbio]` without
duplicate-kind errors. `presets/README.md` maps every part to the bundles that include it and
records the include graph.

```yaml
# presets/nucleic_acids.yaml (a part: proteins supplies protein_construct and
# the sequence_fields mixin; base supplies named_fields)
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
    entity_kinds: [plasmid]      # no statuses: every aliquot uses the fixed set (§1.1)
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
generated Dolt test — Dolt CHECK has no REGEXP guarantee), `description`, `length`, `long`.

`long: true` marks a `string`, `text` or `sequence` field whose value can be large (a sequence, a
free-text note), so interactive forms show it in short form instead of in full. It is valid only on
those three types; it changes no DDL and is read-only form metadata, excluded from the schema
fingerprint like `description`, and it appears in `tm schema show`. Adding or removing it needs no
`version:` bump and no migration step (§5).

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

### 2.4 Aliquot status

Aliquot status is **not schema**: every aliquot uses one fixed, hardcoded set — `pending`,
`stored`, `missing`, `consumed` — with the meaning, `in_stock` flag and transitions in §1.1. There
are no per-kind status sets and no per-stock-kind status vocabulary.

In the new spec version a top-level `status_sets:` key or a stock kind's `statuses:` key is a
**load error**; entity and box kinds never carried one. Older schema versions still load: a ledger
opens its own stored preset copies, so an old ledger's `status_sets:`/`statuses:` are read exactly
as that version defines them. `tm check` does exactly what the ledger's stored presets say — an old
ledger keeps passing, a new ledger enforces the fixed set — with no special-casing.

The stock views do not consult a schema set: `in_stock` is `status = 'stored'`, and `aliquot.status`
carries a real DB `CHECK (status IN ('pending','stored','missing','consumed'))` (§3), replacing the
generated `core_aliquot_status_known` test.
