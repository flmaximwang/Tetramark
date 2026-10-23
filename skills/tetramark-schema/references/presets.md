# Bundled presets, annotated

Three presets ship with the package. `base` is the common floor; `molbio` and `library` both
`include: [base]`. `tm init --preset NAME` may be repeated (e.g. `-p base -p library`) and also
accepts a path to a local preset directory.

```
src/tetramark/presets/
  base/preset.yaml
  molbio/preset.yaml       include: [base]
  library/preset.yaml      include: [base]
```

## `base` — domain-neutral inventory and box geometry

| Kind | Object | Purpose |
|---|---|---|
| `item` | entity | Generic reagent, consumable or part. Mixin `named_fields` (`name` required/unique, `description`); field `category`. |
| `lot` | stock | Purchase/delivery lot of an `item`. `supplier` required; `catalog_no`, `received_on`, `expires_on`. `entity_kinds: [item]`. |
| `grid9x9` | box | Rows `A-J` excluding `I`, columns `1-9` → 81 slots. The old WFL-08 9×9 tubes. |
| `grid96` | box | Rows `A-H`, columns `1-12` → 96 slots. |
| `rack` | box | Named slots `1` … `10`. |
| `shelf` | box | Slotless, capacity 12. |
| `freezer` | box | Slotless, capacity 6. |
| `drawer` | box | Slotless, capacity 1. |

Mixin `named_fields` is the reusable entity identity: `name` (string, required, unique, 255) and
`description` (text). `molbio` reuses it on every entity kind.

Rules:

| ID | Severity | Checks |
|---|---|---|
| `BASE01` | fail | `expires_on >= received_on` on `stock_lot` |
| `BASE02` | fail | every lot has a non-empty supplier |
| `BASE03` | info | items without any stock |

## `molbio` — the zsqlab08/10 biology inventory

`include: [base]`, so all box kinds and `item`/`lot` are available too.

### Mixin `sequence_fields`

```yaml
sequence: {type: sequence, unique: true}
length_bp: {type: computed, expr: "CHAR_LENGTH(sequence)", sql_type: int}
```

A sequence is stored normalised (uppercase, whitespace stripped) and fingerprinted with a generated
`sequence_sha256`; `unique: true` puts the UNIQUE index on that hash, not on the `MEDIUMTEXT`.
`length_bp` is recomputed, never typed in.

### Entities

| Kind | Fields beyond `sequence`/`length_bp` |
|---|---|
| `construct` | `mature_sequence` (sequence), `aliases`, `molecular_weight_da`, `pi`, `extinction_coefficient_reduced_m1cm1`, `extinction_coefficient_oxidized_m1cm1`, two computed `*_0p1pct` columns (`CAST(eps AS DECIMAL(20,10)) / molecular_weight_da`), `tag` |
| `plasmid` | `construct` (ref → `construct`), `backbone`, `resistance` (enum `amp, kan, cm, spec, tet`), `origin_of_replication`, `promoter`, `affinity_tags` |
| `strain` | `genotype` (text), `species` (default `Escherichia coli`), `source` |

`construct` and `plasmid` mix in `named_fields` + `sequence_fields`. The two computed extinction
columns are the zsqlab08 Excel formulas, now generated columns: they cannot drift and cannot be
written.

### Stocks

| Kind | `entity_kinds` | Fields | Aliquot fields |
|---|---|---|---|
| `bacterial` | `plasmid`, `strain` | `strain` (required), `medium`, `batch` | `glycerol_pct` (decimal, default 15) |
| `plasmid_prep` | `plasmid` | `concentration_ng_ul`, `prep_method`, `elution_buffer` | — |
| `protein_prep` | `construct` | `concentration_mg_ml`, `buffer`, `purification_id`, `purity_pct` | — |
| `pellet` | `plasmid`, `strain` | `culture_volume_ml`, `od600`, `induction` (enum `iptg, auto, lactose, none`, default `none`), `induction_temp_c` | — |

`pellet` volumes live in the core aliquot `quantity`/`unit` (one tube = one aliquot), matching the
zsqlab10 pellet-aliquots model. `bacterial` stores host strain and medium on the stock; the glycerol
percentage is per tube.

### Rules

| ID | Severity | Checks |
|---|---|---|
| `MB01` | fail | a plasmid with `construct` has a sequence |
| `MB02` | fail | bacterial `glycerol_pct` is between 0 and 50 |
| `MB03` | fail | protein-prep concentration is > 0 when set |
| `MB04` | info | constructs with a sequence record `molecular_weight_da` |

## `library` — proof of generality

`include: [base]`.

| Kind | Object | Fields |
|---|---|---|
| `book` | entity | mixin `named_fields`; `title` (required), `authors`, `isbn` (unique, pattern `^[0-9Xx][0-9Xx-]{8,16}$`), `year`, `publisher` |
| `copy` | stock | `entity_kinds: [book]`; `acquired_on`, `condition` (enum `new, good, fair, poor`, default `good`), `source`, `price` |

Rules:

| ID | Severity | Checks |
|---|---|---|
| `LIB01` | fail | publication year is not in the future |
| `LIB02` | fail | a copy is not acquired before its book's publication year |
| `LIB03` | info | books without any copy |
| `LIB04` | info | copies without an acquisition date |

## Choosing a preset

- Lab samples in the zsqlab lineage → `molbio` (which already includes `base`).
- Only boxes and generic purchases → `base`.
- A different domain → write a local preset that `include: [base]` and add your own kinds; keep the
  four core objects and reuse the box geometries. `library` is the smallest complete example.

## Extending a bundled preset locally

Do not edit the installed package. Put the additions in the ledger:

```yaml
# <ledger>/kinds/equipment.yaml
kinds:
  equipment:
    object: entity
    mixins: [named_fields]
    fields:
      serial_no: {type: string, length: 64, unique: true}
      purchased_on: {type: date}
```

```yaml
# <ledger>/ledger.yaml
presets: [molbio]
kinds_dir: kinds/
rules_dir: rules/
```

Then follow `references/evolution.md`. Local kinds may reuse preset mixins (`named_fields`,
`sequence_fields`) because mixins are resolved across the whole effective schema.
