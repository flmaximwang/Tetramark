# Presets, annotated

Three presets live in the repository's `presets/`. `base` is the common floor; `molbio` and
`library` both `include: [base]`. `tm init --preset NAME` may be repeated (e.g. `-p base -p
library`) and also accepts a path to a local preset directory or a preset YAML file. Names resolve
through the workspace's `presets_dir`; `tm init` copies the chosen preset and its include closure
into `<ledger>/presets/`, which is what the ledger opens from then on.

```
presets/
  base.yaml
  molbio.yaml       include: [base]
  library.yaml      include: [base]
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
| `protein_construct` | `mature_sequence` (sequence), `aliases`, `molecular_weight_da`, `pi`, `extinction_coefficient_reduced_m1cm1`, `extinction_coefficient_oxidized_m1cm1`, two computed `*_0p1pct` columns (`CAST(eps AS DECIMAL(20,10)) / molecular_weight_da`), `tag` |
| `plasmid` | `construct` (ref → `protein_construct`), `backbone`, `resistance` (enum `amp, kan, cm, spec, tet`), `origin_of_replication`, `promoter`, `affinity_tags` |
| `bacterial_strain` | `genotype` (text), `species` (default `Escherichia coli`), `source` |
| `cell_line` | `species`, `cell_type`, `source`, `genotype`, `parental` (ref → `cell_line`, the parent of a derived/stable line) |

`protein_construct`, `plasmid` and `cell_line` mix in `named_fields`; the two sequence-carrying ones
also mix in `sequence_fields`. The two computed extinction columns are the zsqlab08 Excel formulas,
now generated columns: they cannot drift and cannot be written.

### Stocks

| Kind | `entity_kinds` | Fields |
|---|---|---|
| `bacterial_glycerol` | `plasmid`, `bacterial_strain` | `strain` (required), `medium`, `batch`, `glycerol_pct` (decimal 5,2, default 15) |
| `plasmid_prep` | `plasmid` | `concentration_ng_ul`, `prep_method`, `elution_buffer` |
| `protein_prep` | `protein_construct` | `concentration_mg_ml`, `buffer`, `purification_id`, `purity_pct` |
| `bacterial_pellet` | `plasmid`, `bacterial_strain` | `culture_volume_ml`, `od600`, `induction` (enum `iptg, auto, lactose, none`, default `none`), `induction_temp_c` |
| `cell_vial` | `cell_line` | `passage`, `medium`, `freezing_medium`, `cells_per_vial`, `dmso_pct` (decimal 5,2, default 10) |
| `cell_pellet` | `cell_line`, `plasmid` | `cell_count`, `passage`, `transfection`, `harvested_on` |

Molbio 2 has **no `aliquot_fields`**: a property of the material lives on the stock kind (the user
rule is "all aliquots of one stock are the same"). `bacterial_pellet` volumes still live in the core
aliquot `quantity`/`unit` (one tube = one aliquot); a plasmid on `cell_pellet` records a transient
transfection / transduction harvest.

### Rules

| ID | Severity | Checks |
|---|---|---|
| `MB01` | fail | a plasmid with a protein construct has a sequence |
| `MB02` | fail | `bacterial_glycerol.glycerol_pct` is between 0 and 50 |
| `MB03` | fail | protein-prep concentration is > 0 when set |
| `MB04` | info | protein constructs with a sequence record `molecular_weight_da` |
| `MB05` | fail | `cell_vial.dmso_pct` is between 0 and 100 |

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

## Extending a preset locally

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
