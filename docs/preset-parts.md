# Preset parts, organism model, status sets and lineage (wave 10)

Status: design settled 2026-10-28 with the user · Issue: git-bug `0a9acec` "BioManager Presets" ·
Tickets: `docs/tickets/25-…` to `29-…` · Builds on wave 9 (tickets 20–24), which must land first.

This document is the source of truth for wave 10. Tickets fold the relevant parts into `docs/spec.md`.

---

## 1. Scope

New presets for three kinds of lab (molecular biology, cell biology, animal genetics). They are adapted
from the data model of [BioManager](https://github.com/gaspolymerase/biomanager) (MIT licence).

- **Borrowed:** *what* BioManager records, i.e. its kinds and fields (organism lines, frozen lots, cell
  lines, antibodies, viruses, oligos, samples).
- **Not borrowed:** *how* it works: JSON custom-field columns, per-module vocabularies, capability
  checklists, schedule rules. Tetramark's presets and the ledger's `kinds/` folder are already the
  configurable layer.
- **Not modelled** (listed in `presets/README.md`): body-weight series, water logs, flip, weaning and
  turnover schedules, due-lists, health checks, genotype-call history with gel images. These are
  time-based activity records, not stored material, and may come back later as **plugins**, not core
  features. Plants and BioManager's "custom" organism are also left out (a ledger's own `kinds/`
  already covers "custom").
- **Prototype:** no real data will enter these presets soon. Every new part and bundle starts at
  `version: 1` and grows through `migration/presets/<name>/N-to-N+1/` steps.
- Every new part's `description:` ends with "Adapted from BioManager (MIT)."

## 2. Parts and bundles

A **part** is an ordinary preset file: flat in `presets/` (so ticket 20's
`<presets_dir>/<name>.yaml` lookup works unchanged), with its own `version:`, `kinds:`, `rules:` and
`status_sets:` (§4). A **bundle** is a preset whose only content is `include:`. Includes are loaded
once each (`schema/loader.py`, `_expand`), so two bundles can share a part, and one ledger can list
`presets: [molbio, cellbio]` without duplicate-kind errors. Kind names stay one global namespace.

| part | kinds | molbio | cellbio | angenet |
|---|---|:-:|:-:|:-:|
| `base` | item, lot; boxes grid9x9, grid96, rack, shelf, freezer, drawer; status sets `tube`, `cryo` | ✓ | ✓ | ✓ |
| `nucleic_acids` | plasmid, plasmid_prep, oligo, oligo_stock | ✓ | ✓ | ✓ |
| `bacteria` | bacterial_strain, bacterial_glycerol, bacterial_pellet | ✓ | | |
| `proteins` | protein_construct, protein_prep | ✓ | | |
| `cell_culture` | cell_line, cell_vial, cell_pellet | ✓ | ✓ | |
| `immuno` | antibody, antibody_lot | | ✓ | |
| `viral_vectors` | virus, virus_prep | | ✓ | |
| `biosamples` | biosample (tissue / blood / DNA / RNA) | | ✓ | ✓ |
| `drosophila`, `c_elegans`, `zebrafish`, `mouse` | §3 | | | ✓ |

Each part's `description:` names the bundles that include it. Each part includes only what it references
(e.g. `bacteria` includes `nucleic_acids`, because `bacterial_glycerol` holds plasmids; `cell_culture`
includes `nucleic_acids` for `cell_pellet → plasmid`). A part never includes a bundle.

`molbio` stays the bundle name existing ledgers already list. Splitting it is the
`migration/presets/molbio/2-to-3` step (ticket 27). It renames no tables, but `preset_versions` gains
the part keys.

### 2.1 New non-organism kinds (fields adapted from BioManager's inventory lists)

- **oligo** (entity, `nucleic_acids`): `named_fields`, `sequence` (sequence), `direction` (enum fwd/rev),
  `target` (string), `modification` (string), `purification` (string), `tm_c` (decimal), `length_nt` /
  `gc_pct` (computed from sequence where expressible). **oligo_stock** (stock): `concentration_um`,
  `solvent`.
- **antibody** (entity, `immuno`): `named_fields`, `host`, `clonality` (enum monoclonal/polyclonal/
  recombinant), `clone`, `isotype`, `reactivity`, `conjugate`, `applications`, `rrid`.
  **antibody_lot** (stock): `supplier`, `catalog_no`, `lot_no`, `concentration_mg_ml`,
  `recommended_dilution`, `received_on`, `expires_on`.
- **virus** (entity, `viral_vectors`): `named_fields`, `virus_type` (enum aav/lentivirus/retrovirus/
  adenovirus/other), `serotype`, `payload` (ref → plasmid, **not** lineage), `promoter`, `biosafety_level`.
  **virus_prep** (stock): `titer`, `titer_unit` (enum vg_ml/tu_ml/pfu_ml), `made_on`, `made_by`.
- **cell_vial** gains BioManager's `selection`, `mycoplasma` (enum not_tested/negative/positive) and
  `mycoplasma_tested_on`. `cell_line.parental` becomes `lineage: true`.
- **biosample** (stock, `biosamples`): `entity_kinds` = every entity kind of the bundles that include it.
  Because kind names are global and `entity_kinds` is static, `biosamples` lists the cell and animal entity
  kinds and the loader must accept an `entity_kinds` entry that is not loaded; check whether it does, and
  if not, add `entity_kinds_optional: true` or split into `cell_biosample` / `animal_biosample` (ticket 28
  decides and records the choice). Fields: `sample_type` (enum tissue/blood/dna/rna/serum/other),
  `tissue`, `collected_on`, `source_aliquot` (via core `source_aliquot_id`: the animal or vial it was
  taken from), `storage_temp_c`.

## 3. Organism model (`angenet`)

**Colony = stock, individual = aliquot.** Individual organisms of one stock are treated as **identical**,
which is what makes an experiment repeatable. This is wave 9's principle applied to living material.

- **Stock** = organisms of one line that are the same in every recorded property: line, cohort / cross,
  birth date, sex, genotype. Those are stock fields.
- **Aliquot**, by how the organism is counted (BioManager's identity mode):
  - *individually counted* (mouse, tagged fish): one organism, quantity 1, `label` = ear tag / tag,
    box = its `cage`;
  - *group-counted* (fly vial, worm plate, fish tank): one housing unit's population, with quantity =
    headcount and unit `individuals`. The vial, plate or tank **is** the aliquot, sitting in a `rack`,
    `incubator` or `shelf` box.
- **Crosses**: a cross is recorded as its **progeny stock**, with `dam` and `sire` fields
  (`type: ref`, `to: aliquot`, `lineage: true`). A parent is one animal or one vial of virgins, so both
  counting modes work. Core `parent_stock_id` holds the dam's stock. There is no cross object.
- **Litters**: first ledgered as a stock with `genotype` NULL (untyped) and one group aliquot (quantity =
  pups). At genotyping / weaning, create one stock per genotype × sex with `parent_stock_id` = the litter
  stock, and each animal as an aliquot. The litter aliquot goes to a final status. No new core command.

| part | entity (line fields) | living stock | frozen stock | status sets |
|---|---|---|---|---|
| `drosophila` | `fly_line`: balancers, stock_centre, stock_number, flybase_id | `fly_culture` (vial): temperature_c, generation, food_type, virgins_collected_on, selection_marker, `dam`/`sire` | — | `fly` |
| `c_elegans` | `worm_strain`: allele, lab_prefix, wormbase_id, source | `worm_culture` (plate): temperature_c, plate_type, generation, stage | `worm_cryo`: frozen_on, method, recovery_tested_on, recovery_ok | `worm`, `cryo` |
| `zebrafish` | `fish_line`: zfin_name, allele, background | `fish_group` (tank, or a tagged fish): birth_on, sex, genotype, density_per_l, `dam`/`sire`; `fish_clutch` (embryos): set_up_on, embryo_count, larvae_count, `dam`/`sire` | `fish_sperm_cryo`: frozen_on, method, recovery_tested_on, recovery_ok | `animal`, `cryo` |
| `mouse` | `mouse_strain`: background, supplier, mgi_id | `mouse_cohort` (one mouse per aliquot): birth_on, sex (enum f/m/u), genotype, `dam`/`sire` | `mouse_sperm_cryo`, `mouse_embryo_cryo` (embryo_stage): frozen_on, method, recovery_tested_on, recovery_ok | `animal`, `cryo` |

Every organism entity kind uses `named_fields` and a `parental` ref to its own kind (`lineage: true`),
like `cell_line`. New box kinds in `angenet` parts: `cage` (slotless, capacity 5, in `mouse`) and
`incubator` (slotless, in `base`, because fly, worm and cell culture all use it). Zebrafish embryos are
not routinely cryopreserved, so there's no fish embryo cryo kind.

## 4. Status sets and transitions (core, spec 4)

Statuses stop being per ledger. `ledger.yaml` loses `statuses:`.

```yaml
status_sets:            # any preset or ledger kinds/ file may define sets
  tube:
    pending:  {in_stock: false, to: [stored, missing, consumed]}
    stored:   {in_stock: true,  to: [missing, consumed]}
    missing:  {in_stock: false, to: [stored, consumed]}
    consumed: {in_stock: false}              # no `to:` = final
kinds:
  bacterial_glycerol: {object: stock, statuses: tube, …}
  mouse_cohort:       {object: stock, statuses: animal, …}
```

- Every **stock** kind must set `statuses:` to a set name or an inline set; missing → load error.
  Entity and box kinds have none. An aliquot's allowed statuses are its stock kind's set.
- Set names are global, like kind names, and duplicates are an error. Every `to:` target must exist in
  the same set.
- **Creation** (`tm aliquot add`, `tm stock add` with aliquots, `tm import`) accepts any status in the set.
  Creating an aliquot is not a transition, and migrations must be able to record already-final tubes.
- **Changes** (`tm aliquot status`, any edit that changes status) must follow `to:`. `--force` overrides
  a rule, requires `--note`, and writes `aliquot_event.forced = TRUE` (new column).
- Enforcement: the core write path refuses illegal changes. A generated `tm check` rule flags unforced
  `status` events whose `old_status → new_status` isn't allowed by the stock kind's set (this catches SQL
  hand-written in Workbench).
- The global `aliquot.status` CHECK becomes "status belongs to its stock kind's set", validated in the app
  and by a generated rule (Dolt has no cross-table CHECK).

Bundled sets:

| set | statuses (→ allowed next) | in stock | lives in |
|---|---|---|---|
| `tube` | pending → stored/missing/consumed; stored → missing/consumed; missing → stored/consumed; consumed | stored | `base` |
| `cryo` | stored → thawed/missing; missing → stored/thawed; thawed | stored | `base` |
| `animal` | planned → alive/cancelled; alive → euthanized/found_dead/transferred; cancelled, euthanized, found_dead, transferred | alive | `base` (used by `zebrafish`, `mouse`; see below) |
| `fly` | alive → crossed/discarded/lost; crossed → discarded/lost; discarded, lost | alive, crossed | `drosophila` |
| `worm` | growing ↔ starved; growing/starved → contaminated/discarded; contaminated → discarded; discarded | growing, starved | `c_elegans` |

`animal` is used by two parts and set names are global, so it lives in `base` (as `tube` and `cryo`
do). Existing kinds (molbio, library, base `lot`) get `statuses: tube`; `cell_vial` gets `cryo`.

**The aliquots are a stock's real status.** Views show how many are in stock out of how many in total.
Every `v_stock_<kind>` replaces wave 9 ticket 23's `aliquots_stored`, `quantity_stored` and five-value
`status` with:

- `aliquots_in_stock` (aliquots whose status has `in_stock: true`), `aliquots_total`;
- `quantity_in_stock` + `unit` (NULL when in-stock tubes mix units);
- `status`: `active` (in stock > 0) · `depleted` (total > 0, in stock = 0) · `empty` (total = 0).

`v_stock_status` and `v_box_slots` follow the same rule. Statuses that are in stock are read from the
schema when the views are generated.

Migration `migration/spec/3-to-4/`: add `aliquot_event.forced`, regenerate views and rules, and remove
`statuses:` from `ledger.yaml`/`ledger_meta.ledger_yaml` **only if** it equals the default
`[pending, stored, missing, consumed]`. Otherwise refuse, and the README explains how to turn the list
into a `status_set` in the ledger's `kinds/`. Existing aliquot statuses must all be in their stock kind's
set; the step lists any that aren't and refuses.

## 5. `tm lineage ID`

Shows the descent graph of entities and stocks. It's lineage or pedigree, not phylogeny (which relates
species).

- **Edges** (child → parent): stock → its entity; `stock.parent_stock_id`; `stock.source_aliquot_id`
  (→ that aliquot → its stock); and any `type: ref` field declared with **`lineage: true`**
  (`cell_line.parental`, organism `parental`, `dam`, `sire`, `plasmid.construct`). Other refs
  (`virus.payload`) are plain fields, not edges. `lineage: true` is only valid on `ref` fields.
- **Nodes**: entities and stocks. Each stock shows `aliquots_in_stock/aliquots_total`. An aliquot is a
  node only when it's an edge endpoint (a `dam`/`sire`, a `source_aliquot_id`).
- **Output**: an indented text tree. The graph can have two parents, so a node reached a second time is
  printed once more as `<id> (see above)`. Options: `--up` (ancestors) / `--down` (descendants), both
  by default; `--depth N`; `--json` (`{nodes, edges}` with edge kind); `--format dot` (Graphviz).
- Read-only. Works on any ledger at spec ≥ 4. On ledgers without `lineage: true` fields it still shows
  the core edges.

## 6. Order and dependencies

Wave 9 (20–24) first. Then:

| # | Ticket | Depends on |
|---|---|---|
| 25 | Status sets + transition rules + in-stock views (spec 4, `migration/spec/3-to-4`) | 23 |
| 26 | `lineage: true` + `tm lineage` | 23 (parallel with 25) |
| 27 | Split molbio into parts; `incubator` and status sets in `base`; `migration/presets/molbio/2-to-3` | 22, 25 |
| 28 | `cellbio` bundle; parts `immuno`, `viral_vectors`, `biosamples`; cell_vial extras | 26, 27 |
| 29 | `angenet` bundle; parts `drosophila`, `c_elegans`, `zebrafish`, `mouse` | 25, 26, 27 (and 28 for `biosamples`) |

27 needs 25 because every stock kind it touches must get `statuses:`. zsqlab10 then needs `spec/3-to-4`
and `molbio 2-to-3`. Both are listed in ticket 27's verify step on a scratch copy; running them on the
real ledger is a separate follow-up after review, as ticket 24 was.
