# Presets: parts and bundles

A **part** is an ordinary preset file in this directory (`presets/<name>.yaml`): it has its own
`version:`, `kinds:`, `mixins:`, `rules:` and `status_sets:`, and it includes only the parts it
references. A **bundle** is a preset whose only content is `include:`; it adds no kinds and no rules.
Includes are loaded **once each** (`schema/loader.py`), so two bundles can share a part and one
ledger can list two bundles (`presets: [molbio, cellbio]`) without duplicate-kind errors. Kind names
stay one global namespace.

`molbio` keeps its name because existing ledgers already list it: it is a bundle now, and the
`migration/presets/molbio/2-to-3/` step moves a v2 ledger onto it without renaming a single table.

## The catalogue

| part | kinds | molbio | cellbio | angenet |
|---|---|:-:|:-:|:-:|
| `base` | item, lot; boxes grid9x9, grid96, rack, shelf, freezer, drawer, incubator; status sets `tube`, `cryo`, `animal` | ✓ | ✓ | ✓ |
| `nucleic_acids` | plasmid, plasmid_prep, oligo, oligo_stock | ✓ | ✓ | ✓ |
| `bacteria` | bacterial_strain, bacterial_glycerol, bacterial_pellet | ✓ | | |
| `proteins` | protein_construct, protein_prep | ✓ | ✓ | ✓ |
| `cell_culture` | cell_line, cell_vial, cell_pellet, cell_biosample | ✓ | ✓ | |
| `immuno` | antibody, antibody_lot | | ✓ | |
| `viral_vectors` | virus, virus_prep | | ✓ | |
| `biosamples` | animal_biosample (the angenet side; cell samples are `cell_biosample` in `cell_culture`) | | | ✓ |
| `drosophila` | fly_line; fly_culture; status set `fly` | | | ✓ |
| `c_elegans` | worm_strain; worm_culture, worm_cryo; status set `worm` | | | ✓ |
| `zebrafish` | fish_line; fish_group, fish_clutch, fish_sperm_cryo | | | ✓ |
| `mouse` | mouse_strain; mouse_cohort, mouse_sperm_cryo, mouse_embryo_cryo; box kind `cage` (slotless, capacity 5) | | | ✓ |

The bundles:

- `molbio` — `include: [base, nucleic_acids, bacteria, proteins, cell_culture]`, version 3.
- `cellbio` — `include: [base, nucleic_acids, cell_culture, immuno, viral_vectors]`, version 1.
  `proteins` arrives through `nucleic_acids` (a plasmid points at its protein design).
- `angenet` — `include: [base, nucleic_acids, biosamples, drosophila, c_elegans, zebrafish,
  mouse]`, version 1. `proteins` arrives through `nucleic_acids` (the constructs a line may carry);
  `biosamples` is listed because it names the organism line kinds, and only this union loads them
  all. The four organism parts are the model of `docs/preset-parts.md` §3.

Each part's `description:` says what it covers and which bundles include it. New parts start at
`version: 1` and grow through `migration/presets/<name>/N-to-N+1/`, never by editing a shipped
version in place. Every part that adds kinds adapted from [BioManager](https://github.com/gaspolymerase/biomanager)
(MIT) ends its description with "Adapted from BioManager (MIT)." and its new kinds do the same.

## The include graph

```
base
├── immuno                 (antibody, antibody_lot)
├── biosamples             (animal_biosample; loaded with the organism parts, not by include)
├── drosophila             (fly_line, fly_culture; status set fly)
├── c_elegans              (worm_strain, worm_culture, worm_cryo; status set worm)
├── zebrafish              (fish_line, fish_group, fish_clutch, fish_sperm_cryo)
├── mouse                  (mouse_strain, mouse_cohort, mouse_sperm_cryo,
│                           mouse_embryo_cryo; box cage)
└── proteins               (sequence_fields mixin; protein_construct, protein_prep)
    └── nucleic_acids      (plasmid, plasmid_prep, oligo, oligo_stock)
        ├── bacteria       (bacterial_strain, bacterial_glycerol, bacterial_pellet)
        ├── cell_culture   (cell_line, cell_vial, cell_pellet, cell_biosample)
        └── viral_vectors  (virus, virus_prep)
```

- **`nucleic_acids` includes `proteins`** because `plasmid.construct` is a `ref` to
  `protein_construct`; **`viral_vectors` includes `nucleic_acids`** because `virus.payload` is a
  `ref` to `plasmid`. A part includes only what its refs/entity_kinds name.
- **`sequence_fields` lives in `proteins`** — the deepest part that both `plasmid` (via
  `nucleic_acids`) and `protein_construct` load. `base` is lower in the graph, but it is the
  domain-neutral inventory part; biology-specific mixins do not belong there.
- **A part never includes a bundle.** Includes point down the graph only, so a bundle is always
  the outermost file a ledger lists.
- **`biosamples` is the one part that is not self-contained.** It names the organism line kinds
  (`fly_line`, `worm_strain`, `fish_line`, `mouse_strain`) in `animal_biosample.entity_kinds`, and a
  stock's `entity_kinds` may only name entity kinds the same ledger loads — the loader raises
  `'fly_line' is not an entity kind` otherwise. Those kinds are owned by the four organism parts, so
  `biosamples` is loaded **with** them: the `angenet` bundle lists `biosamples` and all four
  organism parts, and that union is what makes the file valid. Do not add `biosamples` to
  `cellbio`, `molbio` or any other bundle, and do not `tm presets show biosamples` on its own.
- **A part includes only base when it references nothing else.** The organism parts name the core
  `aliquot` table in their `dam`/`sire` refs (core objects need no include) and use `named_fields`
  and the status sets from `base`; `cage` lives in `mouse`, because no other part uses it. Set names
  are global, so `fly` and `worm` are declared in their own parts while `animal` and `cryo` stay in
  `base` (shared by zebrafish/mouse and by every frozen stock).

## The biosamples split (ticket 28)

`biosample` must accept **cell lines** (cellbio) and **animal lines** (angenet), but `entity_kinds`
is static and kind names are global, while no bundle loads both families together. The loader
accepts only entity kinds defined in the same effective schema; an `entity_kinds` entry that is not
loaded is a load error (`kinds.<kind>.entity_kinds: '<name>' is not an entity kind`), and there is no
`entity_kinds_optional` switch. Ticket 28 therefore chose the option that needs **no core change**
(design §2.1, option (a)) and split the model by the entities it references:

- **`cell_biosample`** lives in `cell_culture` (`entity_kinds: [cell_line]`, `statuses: tube`), so
  `molbio` and `cellbio` get a cell-side biosample through the part that already defines the cell
  kinds. Fields: `sample_type` (tissue/blood/dna/rna/serum/other), `tissue`, `collected_on`,
  `storage_temp_c`.
- **`animal_biosample`** lives in `biosamples` (`entity_kinds` = the four organism line kinds,
  `statuses: tube`) and is used by `angenet` only. Same four fields.

A sample's **origin is the core stock column** `source_aliquot_id`, exposed as
`tm stock add KIND --entity E --source-aliquot ALQ` — not a schema field. `tm lineage` follows it
from the sample to the tube it was taken from and on to that tube's stock/line, so a cell biosample
taken from a cryovial appears under that vial.

## Organisms: line, colony and aliquot (`angenet`)

The model is the one in `docs/preset-parts.md` §3, and it needs no core change:

- a **line is an entity** (`fly_line`, `worm_strain`, `fish_line`, `mouse_strain`); its `parental`
  ref is a lineage edge;
- a **colony is a stock** (`fly_culture`, `worm_culture`, `fish_group`/`fish_clutch`,
  `mouse_cohort`): the organisms of one line that are identical in every recorded property (cohort
  or cross, birth date, sex, genotype);
- an **aliquot is one animal** (mouse, tagged fish: quantity 1, its tag in the core aliquot
  `label`) **or one housing unit's population** (fly vial, worm plate, fish tank: quantity =
  headcount, unit `individuals`). The vial/plate/tank/cage **is** the aliquot and sits in a box
  (`incubator`, `rack`, `shelf`, or the mouse `cage`);
- a **cross is its progeny stock**: `fly_culture`/`fish_group`/`fish_clutch`/`mouse_cohort` carry
  `dam` and `sire` refs to **aliquots** (a mouse or a virgins vial), both `lineage: true`;
- **statuses come from the stock kind**: `fly` (alive/crossed/discarded/lost), `worm`
  (growing/starved/contaminated/discarded), `animal` and `cryo` from `base`.

### Recipe: register a litter, then split it at genotyping

A litter is a `mouse_cohort` with **`genotype` NULL** and **one group aliquot** (quantity = pups).
Genotyping/weaning creates one stock per genotype × sex with `parent_stock_id` = the litter stock,
each animal as its own aliquot, and finishes the litter aliquot. The IDs below are illustrative (a
ledger with `--id-prefix 77`); substitute the ones `tm` prints.

```sh
# One strain, one cage, the two breeders.  Sex is a stock field (individuals of
# one stock are identical), so the female and the male are two cohorts, each
# with one mouse: aliquot 7726KU01.1 and 7726KU02.1 in cage MC-01.
tm entity add mouse_strain --name B6J-Apobec --set background=C57BL/6J --alias b6j
tm box add cage MC-01
tm stock add mouse_cohort --entity b6j --set sex=f --set birth_on=2026-05-01 \
  --quantity 1 --unit mouse --box MC-01 --status alive
tm stock add mouse_cohort --entity b6j --set sex=m --set birth_on=2026-05-01 \
  --quantity 1 --unit mouse --box MC-01 --status alive

# The litter: one stock with genotype NULL (omit --set genotype) and one group
# aliquot of 7 pups, whose parents are the two breeder aliquots.
tm box add cage MC-02
tm stock add mouse_cohort --entity b6j --box MC-02 --quantity 7 --unit individuals \
  --set birth_on=2026-07-01 --set dam=7726KU01.1 --set sire=7726KU02.1

# Genotyping / weaning: one stock per genotype x sex, parent_stock_id = litter,
# each mouse its own aliquot (a cage holds at most 5).
tm stock add mouse_cohort --entity b6j --parent 7726KU03 --box MC-03 --aliquots 3 \
  --quantity 1 --unit mouse --set genotype=Apobec+/- --set sex=f \
  --set birth_on=2026-07-01
tm stock add mouse_cohort --entity b6j --parent 7726KU03 --box MC-04 --aliquots 4 \
  --quantity 1 --unit mouse --set genotype=Apobec-/- --set sex=m \
  --set birth_on=2026-07-01

# The litter aliquot is done, and the pedigree is one command away.
tm aliquot status 7726KU03.1 euthanized
tm lineage 7726KU04 --up        # pup stock -> litter -> dam + sire aliquots -> breeders
tm lineage <strain_id> --down   # strain -> breeders -> litter -> the split stocks
```

There is no cross object and no `tm cross` command: a cross **is** the progeny stock, `parent_stock_id`
carries the litter link, and the `dam`/`sire` refs are lineage edges. `tm lineage` shows both parents
even though a stock has one `parent_stock_id` column.

## Naming rule: entity = what it is, stock = what you can hold

- An **entity kind** is the design or the organism: `plasmid` (the construct map), `protein_construct`
  (the amino-acid design), `oligo` (the sequence you ordered), `cell_line` (the line), `bacterial_strain`
  (the genotype), `antibody` (the reagent's design), `virus` (the vector design). An entity has no
  location, quantity or status.
- A **stock kind** is one physical form of it: `plasmid_prep` (DNA in a tube), `oligo_stock` (a
  resuspended primer), `bacterial_glycerol` (frozen cells), `cell_vial` (frozen cells of a line),
  `cell_pellet` (harvested cells), `antibody_lot` (a vial of one lot), `virus_prep` (one production
  batch), `cell_biosample`/`animal_biosample` (material taken from a line). Every stock kind sets
  `statuses:` (spec §2.4) and names the `entity_kinds:` it can hold; the aliquots are the tubes.

The sugars in the names are deliberate: `bacterial_glycerol`, not `bacterial`; `plasmid_prep`, not
`plasmid`; `antibody_lot`, not `antibody`. A short kind name must never be mistaken for the old
zsqlab table or the entity.

## Adding a part

1. Create `presets/<name>.yaml` with `preset: <name>`, `version: 1`, a `description:` naming the
   bundles that will include it, and the `include:` list of parts it references (never a bundle).
2. Put each kind in exactly one part. If two parts need it, it belongs in their lowest common
   include, not in both. A shared `mixins:` entry follows the same rule (see `sequence_fields`).
   If two bundles need the same *model* over entity kinds that are not loaded together, split the
   kind by the part that owns its entity kind — that is why `cell_biosample` and `animal_biosample`
   are two kinds (see above).
3. Give every kind and field a `description:`; end new BioManager-adapted kinds with
   "Adapted from BioManager (MIT).".
4. Give every **stock** kind `statuses:` (a name from `base`'s sets or an inline set) and
   `entity_kinds:`; entity and box kinds get neither.
5. Move the rules that query the part's tables into the same part, keeping their ids
   (`MB01`, `IMM01`, `VIR01`, …).
6. Add the part to every bundle that should load it (`include:`), and add a line to the table above.
7. **Bumping a shipped version.** Add `migration/presets/<name>/N-to-N+1/` in the same change. The
   repository's `presets/` only ever holds the **current** version, so the step ships two frozen
   copies next to its `migrate.py`: the old version `N` (the fixture and the rollback copy) and the
   version `N+1` it installs (placed with `vendor_preset_files`, so the step keeps working after a
   later bump). `migration/presets/cell_culture/1-to-2/` is the worked example. Any **other** step
   that records this part's counter must freeze the same old copy — `migration/presets/molbio/2-to-3/`
   ships `cell_culture-1.yaml` for exactly that reason.
8. Record the decision in the part's `description:`/comments if the include direction or the home
   of a shared mixin was arguable.

Then run the tests: `uv run pytest -q -n 2 tests/test_presets.py tests/test_cellbio.py`.

## What is not modelled

Adapted from BioManager's inventory lists only; these are **time-based activity records**, not stored
material, and are deliberately left out. They may come back later as **plugins**, not core features:

- body-weight series, water logs;
- cage flip, weaning and turnover schedules, due-lists;
- health checks;
- genotype-call history with gel images;
- plants and BioManager's "custom" organism (a ledger's own `kinds/` already covers "custom").
