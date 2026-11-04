# Presets, annotated

The repository's `presets/` (shipped in the wheel as `tetramark/presets/`) holds **parts** and
**bundles**. A part is one YAML file with `version:`, `kinds:`, `mixins:`, `rules:`; a bundle has
only an `include:` list. A part never includes a bundle; bundles assemble parts. Includes load once
each, so two bundles can share a part in one ledger. `tm init --preset NAME` may be repeated and also
accepts a path to a preset YAML file or directory. A bare name resolves through
`$TETRAMARK_PRESETS_DIR` when set, otherwise the bundled copies; `tm init` copies the chosen preset
and its include closure into `<ledger>/presets/`, which is what the ledger opens from then on.
`presets/README.md` has the authoritative table, the include graph and the rule for adding a part.

## The catalogue

| part | version | kinds |
|---|---|---|
| `base` | 5 | entity `item`; stock `lot`; boxes `grid9x9`, `grid8x12`, `rack`, `shelf`, `freezer`, `drawer`, `incubator` |
| `nucleic_acids` | 3 | entities `plasmid`, `oligo`; stocks `plasmid_prep`, `oligo_stock` |
| `bacteria` | 2 | entity `bacterial_strain`; stocks `bacterial_glycerol`, `bacterial_pellet` |
| `proteins` | 3 | entity `protein_construct`; stock `protein_prep`; mixin `sequence_fields` |
| `cell_culture` | 3 | entity `cell_line`; stocks `cell_vial`, `cell_pellet`, `cell_biosample` |
| `immuno` | 2 | entity `antibody`; stock `antibody_lot` |
| `viral_vectors` | 2 | entity `virus`; stock `virus_prep` |
| `biosamples` | 2 | stock `animal_biosample` (the angenet side) |
| `drosophila` | 2 | entity `fly_line`; stock `fly_culture` |
| `c_elegans` | 2 | entity `worm_strain`; stocks `worm_culture`, `worm_cryo` |
| `zebrafish` | 2 | entity `fish_line`; stocks `fish_group`, `fish_clutch`, `fish_sperm_cryo` |
| `mouse` | 2 | entity `mouse_strain`; stocks `mouse_cohort`, `mouse_sperm_cryo`, `mouse_embryo_cryo`; box `cage` |
| `library` | 3 | entity `book`; stock `copy` |

| bundle | version | includes |
|---|---|---|
| `molbio` | 3 | `base`, `nucleic_acids`, `bacteria`, `proteins`, `cell_culture` |
| `cellbio` | 1 | `base`, `nucleic_acids`, `cell_culture`, `immuno`, `viral_vectors` |
| `angenet` | 1 | `base`, `nucleic_acids`, `biosamples`, `drosophila`, `c_elegans`, `zebrafish`, `mouse` |

`proteins` arrives in `cellbio`/`angenet` through `nucleic_acids`. `biosamples` names the four
organism line kinds in `animal_biosample.entity_kinds`, so it is loaded only with those parts (the
`angenet` union) — never on its own. A bundle's own version counts only its `include:` list.

## `base` — domain-neutral inventory and box geometry

| Kind | Object | Purpose |
|---|---|---|
| `item` | entity | Generic reagent, consumable or part. Mixin `named_fields` (`name` required/unique, `description`); field `category`. |
| `lot` | stock | Purchase/delivery lot of an `item`. `supplier` required; `catalog_no`, `received_on`, `expires_on`. `entity_kinds: [item]`. |
| `grid9x9` | box | Rows `A-J` excluding `I`, columns `1-9` → 81 slots. |
| `grid8x12` | box | Rows `A-H`, columns `1-12` → 96 slots (was `grid96` before base v4; spec 5 renamed it). |
| `rack` | box | Named slots `1` … `10`. |
| `shelf` | box | Slotless, capacity 12. |
| `freezer` | box | Slotless, capacity 6. |
| `drawer` | box | Slotless, capacity 1. |
| `incubator` | box | Slotless, unbounded. |

`named_fields` is the reusable entity identity: `name` (string, required, unique, 255) and
`description` (text); every biology entity reuses it. Rules:

| ID | Severity | Checks |
|---|---|---|
| `BASE01` | fail | `expires_on >= received_on` on `stock_lot` |
| `BASE02` | fail | every lot has a non-empty supplier |
| `BASE03` | info | items without any stock |

**Version 5 is the spec-6 model:** it drops the `status_sets:` block (`tube`, `cryo`, `animal`) and
every stock kind's `statuses:` key. Every aliquot uses the one fixed status set (below). Version 4
renamed `grid96` → `grid8x12`; v3 added `incubator`; the v1→2 step is the old initial schema.

## `molbio` — the zsqlab biology inventory (bundle)

`molbio` 3 is `include: [base, nucleic_acids, bacteria, proteins, cell_culture]`; it has no kinds or
rules of its own. The parts split the old one-file preset without renaming a table.

### Mixin `sequence_fields`

```yaml
sequence: {type: sequence, unique: true}
length_bp: {type: computed, expr: "CHAR_LENGTH(sequence)", sql_type: int}
```

A sequence is stored normalised (uppercase, whitespace stripped) and fingerprinted with a generated
`sequence_sha256`; `unique: true` puts the UNIQUE index on that hash, not on the `MEDIUMTEXT`.
`length_bp` is recomputed, never typed in. Version 2 (ticket rq9) added the IUPAC alphabet patterns
to `sequence`/`mature_sequence`/`oligo.sequence`; they are validation-only and regenerate tests, not
DDL.

### Entities

| Kind | Part | Fields beyond `sequence`/`length_bp` |
|---|---|---|
| `protein_construct` | `proteins` | `mature_sequence` (sequence), `aliases`, `molecular_weight_da`, `pi`, extinction coefficients as computed columns, `tag` |
| `plasmid` | `nucleic_acids` | `construct` (ref → `protein_construct`, `lineage: true`), `backbone`, `resistance` (enum `amp, kan, cm, spec, tet`), `origin_of_replication`, `promoter`, `affinity_tags` |
| `oligo` | `nucleic_acids` | `sequence`, `direction` (`fwd`/`rev`), `target`, `modification`, `purification`, `tm_c`, computed `length_nt`/`gc_pct` |
| `bacterial_strain` | `bacteria` | `genotype` (text), `species` (default `Escherichia coli`), `source` |
| `cell_line` | `cell_culture` | `species`, `cell_type`, `source`, `genotype`, `parental` (ref → `cell_line`) |

`protein_construct`, `plasmid` and `cell_line` mix in `named_fields`.

### Stocks

| Kind | Part | `entity_kinds` | Fields |
|---|---|---|---|
| `bacterial_glycerol` | `bacteria` | `plasmid`, `bacterial_strain` | `strain` (required), `medium`, `batch`, `glycerol_pct` (default 15) |
| `bacterial_pellet` | `bacteria` | `plasmid`, `bacterial_strain` | `culture_volume_ml`, `od600`, `induction` (enum), `induction_temp_c` |
| `plasmid_prep` | `nucleic_acids` | `plasmid` | `concentration_ng_ul`, `prep_method`, `elution_buffer` |
| `oligo_stock` | `nucleic_acids` | `oligo` | `concentration_um`, `solvent` |
| `protein_prep` | `proteins` | `protein_construct` | `concentration_mg_ml`, `buffer`, `purification_id`, `purity_pct` |
| `cell_vial` | `cell_culture` | `cell_line` | `passage`, `medium`, `freezing_medium`, `cells_per_vial`, `dmso_pct`, `selection`, `mycoplasma` + test date |
| `cell_pellet` | `cell_culture` | `cell_line`, `plasmid` | `cell_count`, `passage`, `transfection`, `harvested_on` |
| `cell_biosample` | `cell_culture` | `cell_line` | `sample_type`, `tissue`, `collected_on`, `storage_temp_c` |

A property of the material lives on the stock kind, never on the aliquot; core quantity/unit carries
per-tube volume. A `bacterial_pellet`'s litres are aliquot quantities, and a plasmid on `cell_pellet`
records a transient transfection/transduction harvest.

### Rules

| ID | Severity | Checks |
|---|---|---|
| `MB01` | fail | a plasmid with a protein construct has a sequence |
| `MB02` | fail | `bacterial_glycerol.glycerol_pct` is between 0 and 50 |
| `MB03` | fail | protein-prep concentration is > 0 when set |
| `MB04` | info | protein constructs with a sequence record `molecular_weight_da` |
| `MB05` | fail | `cell_vial.dmso_pct` is between 0 and 100 |

## `cellbio` parts

- `cell_culture` (**3**) — cell lines and the material frozen or harvested from them. **Version 3
  drops `statuses:`**: a cryovial is `stored` in the freezer and `consumed` once thawed, like every
  other aliquot; there is no cryo vocabulary. The v1→2 step added the `cell_vial` extras and
  `cell_biosample`; the v2→3 step only removes the status vocabulary.
- `immuno` (**2**) — entity `antibody` (`named_fields`; host, clonality enum, clone, isotype,
  reactivity, conjugate, applications, rrid) and stock `antibody_lot` (supplier, catalog_no, lot_no,
  concentration, recommended dilution, received/expiry dates). Rule `IMM01`: a lot cannot expire
  before it was received. Version 2 drops `statuses:`.
- `viral_vectors` (**2**) — entity `virus` (`named_fields`; virus_type enum, serotype, `payload` ref →
  plasmid **without** `lineage` — it is a plain field, promoter, biosafety_level) and stock
  `virus_prep` (titer, titer_unit enum, made_on, made_by). Rule `VIR01`: a recorded titer is positive.
  Version 2 drops `statuses:`.

## `biosamples` and the organism parts (`angenet`)

- `biosamples` (**2**) — stock `animal_biosample` with `entity_kinds` = `fly_line`, `worm_strain`,
  `fish_line`, `mouse_strain`, fields `sample_type`, `tissue`, `collected_on`, `storage_temp_c`. It
  is loaded only with the organism parts (the `angenet` bundle). Version 2 drops `statuses:`.
- `drosophila` (**2**) — `fly_line` (balancers, stock_centre, stock_number, flybase_id, parental) and
  `fly_culture` (temperature, generation, food_type, virgins_collected_on, selection_marker,
  `dam`/`sire` refs to aliquots, and the optional `fate` enum **discarded/lost**).
- `c_elegans` (**2**) — `worm_strain` (allele, lab_prefix, wormbase_id, source, parental) plus
  `worm_culture` (temperature, plate_type, generation, stage, optional `condition` enum
  **growing/starved/contaminated**) and `worm_cryo`.
- `zebrafish` (**2**) — `fish_line` (zfin_name, allele, background, parental); `fish_group` (a tank
  or one tagged fish: birth_on, sex, genotype, density_per_l, optional `fate`, `dam`/`sire`),
  `fish_clutch` (set_up_on, embryo_count, larvae_count, optional `fate`, `dam`/`sire`) and
  `fish_sperm_cryo`.
- `mouse` (**2**) — `mouse_strain` (background, supplier, mgi_id, parental); `mouse_cohort` (one
  individually tracked mouse per stock: birth_on, sex, genotype, optional `fate`, `dam`/`sire`),
  `mouse_sperm_cryo`, `mouse_embryo_cryo`; box kind `cage` (slotless, capacity 5).

The animal `fate` enum is **euthanized, found_dead, transferred, cancelled**; the fly `fate` is
**discarded, lost**; the worm `condition` is **growing, starved, contaminated**. All are optional
stock properties: a stock that has not recorded the detail leaves it NULL, and a frozen cryo vial has
no life-state property at all.

## `library` — proof of generality

`include: [base]`, version 3 (v3 drops `statuses:`).

| Kind | Object | Fields |
|---|---|---|
| `book` | entity | mixin `named_fields`; `title` (required), `authors`, `isbn` (unique, pattern), `year`, `publisher` |
| `copy` | stock | `entity_kinds: [book]`; `acquired_on`, `condition` (enum `new, good, fair, poor`), `source`, `price` |

Rules `LIB01`–`LIB04` (year not in the future, copy not acquired before publication, info on books
without copies and copies without an acquisition date).

## One fixed status set (spec 6)

Aliquot status is **core**, not schema. Every stock kind shares:

| status | in stock | can change to |
|---|---|---|
| `pending` | no | `stored`, `missing`, `consumed` |
| `stored` | yes | `missing`, `consumed` |
| `missing` | no | `stored`, `consumed` |
| `consumed` | no | none (final) |

Creation accepts any of the four; only `stored` is in stock (the views count it and it is the only
status that needs a position). A preset that declares `status_sets:`/`statuses:` is a load error in
spec 6. `tm schema show KIND` resolves and prints the same fixed block for every stock kind.

## Version bumps and the manual spec 5 → 6 upgrade

- **A preset `version:` bump ships a step folder** `migration/presets/<name>/N-to-N+1/` in the same
  change. The repository's `presets/` only holds the current version, so a new bump retrofits older
  scripted steps with frozen copies where needed.
- **A core format bump ships** `migration/spec/N-to-N+1/`, scripted or README-only.
- **Spec 5 → 6 is manual:** `migration/spec/5-to-6/README.md`. Every pending historical preset step
  runs first (they target spec 5), then the whole `<ledger>/presets/` closure is replaced with the
  status-free copies of this checkout, `aliquot.status` is remapped with one `migrated: <old status>`
  event per remapped aliquot, and `tm schema plan`/`apply` writes `spec_version` 6,
  `preset_versions`, the `aliquot.status` `CHECK` and the regenerated `dolt_tests`. Past events are
  untouched and the new enum properties are not backfilled. A ledger that still declares
  `status_sets:`/`statuses:` opens as spec 5; a closure that mixes both models does not load. `tm`
  warns on an older ledger: `ledger is spec N, tm is spec N+1: follow
  migration/spec/N-to-N+1/README.md to upgrade it manually`.
- The bumped parts are `base` 4→5, `proteins` 2→3, `nucleic_acids` 2→3, `bacteria` 1→2,
  `cell_culture` 2→3, `biosamples` 1→2, `c_elegans` 1→2, `drosophila` 1→2, `immuno` 1→2,
  `library` 2→3, `mouse` 1→2, `viral_vectors` 1→2, `zebrafish` 1→2; the bundles keep their
  versions (`molbio` 3, `cellbio` 1, `angenet` 1). Each has a README-only
  `migration/presets/<name>/N-to-N+1/` that replaces the ledger's stored copy.

## Choosing a preset

- Lab samples in the zsqlab lineage → `molbio` (which already includes `base`).
- Cell biology → `cellbio`; animal genetics → `angenet`; one ledger may list both, or all three.
- Only boxes and generic purchases → `base`; a library → `library`.
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
`sequence_fields`) because mixins are resolved across the whole effective schema. A local stock kind
declares no status set either, and a local kind that keeps a `statuses:` key makes the closure mixed
(the ledger does not open) — delete it with `migration/spec/5-to-6/README.md`.
