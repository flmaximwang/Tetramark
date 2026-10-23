---
name: doltledger-schema
description: Use when writing or changing a DoltLedger schema — authoring presets, mixins, kinds, fields, box geometry or validation rules, adding a local kind, running schema plan/apply, or doing a destructive migration (rename/drop/type change/tightening). Triggers include 建库, 预设, 字段, 盒型, 规则, 改结构, 迁移.
---

# Authoring DoltLedger schemas

The schema is YAML, never SQL. `ledger schema apply` diffs the YAML against the live database and
generates the DDL, views, foreign keys and `dolt_tests`. Never add or edit those objects by hand.

## Where schemas live

- **Presets** shipped with the package: `src/doltledger/presets/<name>/preset.yaml`, plus optional
  `kinds/*.yaml` and `rules/*.yaml` next to it. `preset:` is the name, `version:` an integer,
  `include:` a list of preset names or paths.
- **Ledger-local** additions: `ledger.yaml` points at `kinds_dir:` and `rules_dir:`; each `.yaml` in
  those directories is a document with a `kinds:` or `rules:` mapping. Relative paths resolve against
  the ledger directory.
- **`ledger.yaml`** also holds `presets`, `author`, `ids`, `statuses`. Use `ledger init` to create it;
  edit it afterwards to select presets or local dirs.

Name collisions between presets, includes and local files are errors. Includes are merged
parent-first (included documents first), then local files.

## Skeleton

```yaml
# presets/example/preset.yaml
preset: example
version: 1
include: [base]

mixins:
  named_fields:
    name: {type: string, required: true, unique: true}
    description: {type: text}

kinds:
  molecule:
    object: entity                    # entity | stock | box
    mixins: [named_fields]
    fields:
      sequence: {type: sequence, unique: true}
      length_bp: {type: computed, expr: "CHAR_LENGTH(sequence)", sql_type: int}
      project: {type: enum, values: [alpha, beta]}

  batch:
    object: stock
    entity_kinds: [molecule]          # which entity kinds this stock may point at
    fields:
      made_on: {type: date}
    aliquot_fields:
      concentration: {type: decimal, precision: 12, scale: 2}

  grid9:
    object: box
    geometry: {type: grid, rows: "A-C", cols: "1-3"}
    slot_capacity: 1

rules:
  - id: EX01
    description: every molecule records a project
    severity: fail                    # fail | info
    sql: "SELECT COUNT(*) FROM entity_molecule WHERE project IS NULL"
    expect: {single_value: 0}
```

## Field types

| `type` | SQL | Notes |
|---|---|---|
| `string` | `VARCHAR(length)` | default length 255 |
| `text` | `MEDIUMTEXT` | not indexable |
| `int` | `BIGINT` | |
| `decimal` | `DECIMAL(precision, scale)` | defaults 18,6 |
| `bool` | `TINYINT(1)` | |
| `date`, `datetime` | `DATE`, `DATETIME` | ISO values |
| `enum` | `VARCHAR` + `CHECK (col IN (...))` | `values:` required; adding a value is additive |
| `sequence` | `MEDIUMTEXT` + generated `<col>_sha256` | `unique: true` makes the hash unique; the app uppercases and strips whitespace |
| `ref` | `VARCHAR(64)` + FK | `to:` a kind name or `entity`/`stock`/`aliquot`/`box` |
| `computed` | generated `STORED` column | `expr:` over same-row columns + `sql_type:` |

Modifiers: `required`, `unique`, `default`, `pattern` (regex), `description`, `length`
(strings), `precision`/`scale` (decimals). `computed` does not allow `required`, `unique`,
`default` or `pattern`. `sequence` does not allow `default`. `pattern` is checked by the app and by a
generated test (Dolt CHECK cannot guarantee `REGEXP`).

Kinds cannot define the reserved columns `id` and `kind` (and aliquot fields cannot define `id`).
Core columns such as `name` and `notes` may be declared by a kind when they belong in its extension
table; `named_fields` is the usual way to do that.

## Box geometry

| `type` | Spec | Slots |
|---|---|---|
| `grid` | `rows: "A-H"`, `cols: "1-12"`; ranges, `!` exclusions (`"A-J!I"`), comma lists, or YAML lists; optional `label: "{row}{col}"` | row × col |
| `named` | `slots: [top, middle, bottom]` | the listed labels |
| `slotless` | optional `capacity: N` | none; `slot` is NULL |

Every box kind has `slot_capacity` (default 1): max aliquots per slot. Nesting a box uses one slot;
a slot holds aliquots or one child box, never both. Use `slotless` for freezers/shelves/drawers and
`named` for racks.

## Rules

Each rule is one read-only `SELECT` plus an `expect`:

| Expect | Passes when |
|---|---|
| `{single_value: X}` | the query returns one value equal to X (usually `0`) |
| `{rows: 0}` | the query returns no rows |
| `{rows_at_most: N}` | the query returns at most N rows |

`severity: fail` fails `ledger check`; `severity: info` is reported but never changes the exit code
(stored under `<group>:info`). Rule IDs must be unique across presets and local rules.

## Workflow

1. Edit the preset / local kind / rule / `ledger.yaml`.
2. `ledger schema show` — confirm the effective schema (includes and mixins resolved).
3. `ledger schema plan` — read every step and its `additive`/`destructive` tag.
4. `ledger schema apply` — one transaction, one commit, generated tests replaced.
5. `ledger schema plan` again — must print `schema is up to date`.
6. `ledger check` — all non-info tests PASS.

The live fingerprint in `ledger_meta` warns when YAML and DB diverge; that is a prompt to run plan,
not an error.

## Additive vs destructive

**Additive** (applied without flags): new kind, new nullable/defaulted field, new enum value, new
view/rule/test, new aliquot-fields table, relaxing NOT NULL.

**Destructive** (needs `--allow-destructive`): drop a kind/field, type change, new NOT NULL
without a default, new UNIQUE, removing an enum value. Renames are declared as
`renamed_from: old_name` on the kind or field so the plan uses `ALTER … RENAME` (additive, data
preserved) instead of drop+add. Generated columns and dependent FKs are dropped and rebuilt
automatically.

Safe sequence for tightening an existing table: back up (`ledger backup`), add the field nullable,
backfill through `ledger export`/`import` or the app, then make it `required` and apply with
`--allow-destructive`. Always run `ledger check` after apply.

## Worked examples

- `references/presets.md` — the `library` and `molbio` presets annotated field by field.
- `references/evolution.md` — plan/apply transcripts, rename and destructive checklists.
- Live examples: `ledger presets show molbio`, `ledger schema show plasmid`.

## Guardrails

- Never edit `dolt_tests`, generated views, kind tables or `ledger_meta` directly; they are derived.
- Do not write raw `ALTER TABLE`/`CREATE TABLE`; evolve the YAML and apply.
- Do not change an existing rule's meaning silently: bump the preset `version` and update the
  description.
- Run `ledger schema plan` before every apply; the plan is the review.
