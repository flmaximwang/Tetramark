# 8. Presets

Presets are YAML schema documents, not package data. They live in the repository's `presets/`
directory; the wheel ships no `*.yaml` files. A **preset** is a single YAML file containing kinds,
mixins and rules; a preset given by path may also be a directory containing `preset.yaml` plus
optional `kinds/*.yaml` and `rules/*.yaml`. `preset:` is the name, `version:` an integer, `include:`
a list of bare preset names (path includes cannot be copied into a self-contained ledger, see below).

A **part** is a preset meant to be included: it owns a coherent slice of the model (`base`,
`nucleic_acids`, `bacteria`, `proteins`, `cell_culture`, …) and includes only the parts it
references. A **bundle** is a preset whose only content is `include:` — no kinds, mixins, rules or
status sets — so one bundle can assemble several parts and two bundles can share them. Includes are
loaded once each, so listing `molbio` and `cellbio` in one ledger loads the shared parts once; kind
names remain one global namespace. A part never includes a bundle. `presets/README.md` has the
part/bundle table, the include graph and the rule for adding a part.

- **Selecting.** `tm init NAME --preset ...` is repeatable. A value containing `/` (or `\`) or
  ending in `.yaml`/`.yml` is a **path**, used as-is from the caller's cwd. Anything else is a bare
  **name**, resolved as `<presets_dir>/<name>.yaml`; `presets_dir` is a `workspace.yaml` key,
  resolved relative to that file. Without a configured `presets_dir`, a bare name is a usage error
  that names the key. A path works with no `presets_dir`. A preset's own name (its `preset:` field,
  else its file stem/directory name) must match `[A-Za-z][A-Za-z0-9._-]*`: it is the filename of
  the ledger's copy, so a path-like name is rejected.
- **Copying.** `tm init` copies the selected preset **and its `include:` closure** into the ledger:
  `<ledger>/presets/<name>.yaml` for a YAML preset, `<ledger>/presets/<name>/` (with `preset.yaml`,
  `kinds/` and `rules/`) for a directory preset. The copies are byte-identical to the sources.
  Every `include:` must be a bare preset name that matches the included preset's own `preset:`
  field; a path include or a mismatching name is rejected before anything is written, because the
  flattened copy could not resolve it. `include:` entries inside a copy then resolve against the
  copy directory, so the closure is copied too and the ledger is self-contained. After writing the
  copy, `tm init` reloads the ledger from its own `presets/` and fails (rolling the ledger back)
  unless that copy reproduces exactly the planned schema.
- **Resolution.** A ledger opens presets **only** from `<ledger>/presets/`. `ledger.yaml` lists bare
  names (`presets: [molbio]`) and `ledger_meta.preset_versions` is written exactly as before. If a
  copy is missing — including an `include:` target such as `base` — opening fails with the
  expected file path and says that `migration/` or a manual copy fixes it; there is
  no fallback to `presets_dir`, because a silent fallback would let the
  ledger drift from its recorded `preset_versions`. Changing a source preset after init therefore
  does **not** change an existing ledger — replacing its `<ledger>/presets/` files is an explicit
  schema change. The copies live on disk next to `ledger.yaml` and are **not** stored in Dolt, so
  cloning a ledger elsewhere (or restoring a database without its directory) must copy
  `<ledger>/presets/` along with `ledger.yaml`.
- **Inspecting.** `tm presets list` lists the workspace's `presets_dir`; `tm presets show NAME|PATH`
  prints one include-resolved preset; `tm config get|set presets_dir` reads or writes the workspace
  setting.

Preset catalogue:

- `base` (**version 3**) — box kinds `grid9x9` (A–J!I × 1–9), `grid96` (A–H × 1–12), `rack` (named),
  `shelf`/`freezer`/`drawer` (slotless, with a capacity) and `incubator` (slotless, unbounded);
  entity kind `item` (name, category, description); stock kind `lot` (supplier, catalog_no,
  received_on, expires_on, `statuses: tube`); the status sets `tube`, `cryo` and `animal` (§2.4).
- `proteins` (**version 1**, includes `base`) — entity `protein_construct` (sequence, mature_sequence,
  molecular_weight_da, pI, extinction coefficients as computed, tag) and stock `protein_prep`
  (concentration_mg_ml, buffer, purification_id, purity_pct, `statuses: tube`). It also owns the
  `sequence_fields` mixin (sequence + `length_bp`), because it is the deepest part both
  `protein_construct` and `plasmid` load. Included by the `molbio` bundle; `nucleic_acids` includes
  it because a plasmid points at the protein design it expresses.
- `nucleic_acids` (**version 1**, includes `base`, `proteins`) — entities `plasmid` (sequence,
  backbone, resistance, ref `construct` → protein_construct with `lineage: true`) and `oligo`
  (sequence, direction fwd/rev, target, modification, purification, tm_c, `length_nt`/`gc_pct`
  computed from the sequence); stocks `plasmid_prep` (concentration_ng_ul, prep_method,
  elution_buffer) and `oligo_stock` (concentration_um, solvent), both `statuses: tube`. Included by
  `molbio`, `cellbio` and `angenet`.
- `bacteria` (**version 1**, includes `base`, `nucleic_acids`) — entity `bacterial_strain` (genotype,
  species, source); stocks `bacterial_glycerol` (entity_kinds plasmid|bacterial_strain; strain,
  medium, batch, glycerol_pct **on the stock**) and `bacterial_pellet` (culture volume, OD600,
  induction; aliquots carry volume via core quantity), both `statuses: tube`. Included by `molbio`.
- `cell_culture` (**version 2**, includes `base`, `nucleic_acids`) — entity `cell_line` (species,
  cell_type, source, genotype, `parental` ref → cell_line with `lineage: true`); stocks `cell_vial`
  (passage, medium, freezing_medium, cells_per_vial, dmso_pct, and since v2 `selection`, `mycoplasma`
  enum not_tested/negative/positive default not_tested, `mycoplasma_tested_on`; `statuses: cryo`),
  `cell_pellet` (cell_count, passage, transfection, harvested_on; a plasmid records a transient
  transfection, `statuses: tube`) and `cell_biosample` (a biosample taken from a cell line;
  `entity_kinds: [cell_line]`, `statuses: tube`; `sample_type` enum tissue/blood/dna/rna/serum/other,
  tissue, collected_on, storage_temp_c; the origin is the core `source_aliquot_id`). Included by
  `molbio` and `cellbio`. The v1 → v2 step is `migration/presets/cell_culture/1-to-2/`.
- `immuno` (**version 1**, includes `base`) — entity `antibody` (`named_fields`; host, clonality enum
  monoclonal/polyclonal/recombinant, clone, isotype, reactivity, conjugate, applications, rrid) and
  stock `antibody_lot` (supplier, catalog_no, lot_no, concentration_mg_ml, recommended_dilution,
  received_on, expires_on; `statuses: tube`). Rule `IMM01`: a lot cannot expire before it was
  received. Included by `cellbio`.
- `viral_vectors` (**version 1**, includes `base`, `nucleic_acids`) — entity `virus` (`named_fields`;
  virus_type enum aav/lentivirus/retrovirus/adenovirus/other, serotype, `payload` ref → plasmid
  **without** `lineage: true` — a plain field, not a descent edge, promoter, biosafety_level) and stock
  `virus_prep` (titer, titer_unit enum vg_ml/tu_ml/pfu_ml, made_on, made_by; `statuses: tube`). Rule
  `VIR01`: a recorded titer is positive. Included by `cellbio`.
- `biosamples` (**version 1**, includes `base`) — stock `animal_biosample` (`statuses: tube`;
  `sample_type` enum tissue/blood/dna/rna/serum/other, tissue, collected_on, storage_temp_c; the
  origin is the core `source_aliquot_id`), `entity_kinds` = the organism line kinds
  (`fly_line`, `worm_strain`, `fish_line`, `mouse_strain`). It is the **animal side** of the biosample
  model and is included by `angenet` only, loaded together with the organism parts that own those
  entity kinds: a stock's `entity_kinds` may name only entity kinds the same ledger loads, and the
  loader refuses any other (`'<name>' is not an entity kind`) — there is no optional switch. The
  **cell side** is `cell_biosample` in `cell_culture`, which is why `cellbio` needs no biosamples
  part (design §2.1, ticket 28 option (a), no core change).
- `molbio` (a **bundle**, **version 3**) — `include: [base, nucleic_acids, bacteria, proteins,
  cell_culture]`, no kinds or rules of its own. It keeps the name existing ledgers already list,
  and every kind/table name of a molbio ledger is unchanged by the parts split; the new tables of
  that step are the `nucleic_acids` `entity_oligo`/`stock_oligo_stock` pair (and base v3's
  `box_incubator`). Ticket 28's `cell_culture` 2 adds `stock_cell_biosample` to a molbio ledger that
  runs that step.
  Molbio has no per-tube fields: all aliquots of one stock are the same, so a property belongs on
  the stock kind. The bundled rules moved with their tables and kept their ids: `MB01` (plasmid
  sequence) → `nucleic_acids`, `MB02` (glycerol percentage) → `bacteria`, `MB03`/`MB04` (protein
  preps/constructs) → `proteins`, `MB05` (DMSO) → `cell_culture`. Spec 3 removed the
  per-stock-kind tube-field schema key entirely, so a molbio-1 ledger is upgraded by
  `migration/presets/molbio/1-to-2/` (and then `migration/spec/2-to-3/`), and a molbio-2 ledger by
  `migration/presets/molbio/2-to-3/` (plus `migration/presets/base/2-to-3/` for base's counter).
- `library` (**version 2**, includes `base`) — entity `book` (title, authors, isbn pattern, year);
  stock `copy` (acquired_on, condition enum, `statuses: tube`). Exists to prove generality; used in
  tests.

- `cellbio` (a **bundle**, **version 1**) — `include: [base, nucleic_acids, cell_culture, immuno,
  viral_vectors]`, no kinds/mixins/rules/status sets of its own. `proteins` arrives through
  `nucleic_acids`; `biosamples` is deliberately absent (it names the organism line kinds of the
  `angenet` parts). One ledger can list `presets: [molbio, cellbio]`: the shared parts load once, and
  the kind set is the union (molbio brings `bacteria`, cellbio brings `immuno`/`viral_vectors`).
- `drosophila` (**version 1**, includes `base`) — entity `fly_line` (`named_fields`; balancers,
  stock_centre, stock_number, flybase_id, `parental` ref → fly_line with `lineage: true`); stock
  `fly_culture` (`statuses: fly`; a vial or cross vial with temperature_c, generation, food_type,
  virgins_collected_on, selection_marker and `dam`/`sire` ref → aliquot with `lineage: true`). It owns
  the status set `fly` (alive → crossed/discarded/lost; crossed → discarded/lost; discarded/lost
  final; alive and crossed are in stock). Included by `angenet`.
- `c_elegans` (**version 1**, includes `base`) — entity `worm_strain` (`named_fields`; allele,
  lab_prefix, wormbase_id, source, `parental` → worm_strain with `lineage: true`); stocks
  `worm_culture` (`statuses: worm`; a plate with temperature_c, plate_type, generation, stage) and
  `worm_cryo` (`statuses: cryo`; frozen_on, method, recovery_tested_on, recovery_ok). It owns the
  status set `worm` (growing ↔ starved; both → contaminated/discarded; contaminated → discarded;
  discarded final; growing and starved are in stock). Worm selfers keep a single-parent strain
  lineage, so a culture has no `dam`/`sire`. Included by `angenet`.
- `zebrafish` (**version 1**, includes `base`) — entity `fish_line` (`named_fields`; zfin_name,
  allele, background, `parental` → fish_line with `lineage: true`); stocks `fish_group`
  (`statuses: animal`; a tank or a tagged fish with birth_on, sex enum f/m/u, genotype,
  density_per_l and `dam`/`sire` → aliquot), `fish_clutch` (`statuses: animal`; set_up_on,
  embryo_count, larvae_count, `dam`/`sire`) and `fish_sperm_cryo` (`statuses: cryo`; frozen_on,
  method, recovery_tested_on, recovery_ok). There is no fish embryo cryo kind. Included by
  `angenet`.
- `mouse` (**version 1**, includes `base`) — entity `mouse_strain` (`named_fields`; background,
  supplier, mgi_id, `parental` → mouse_strain with `lineage: true`); stocks `mouse_cohort`
  (`statuses: animal`; one mouse per aliquot: birth_on, sex enum f/m/u, genotype and `dam`/`sire` →
  aliquot), `mouse_sperm_cryo` and `mouse_embryo_cryo` (`statuses: cryo`; frozen_on, method,
  recovery_tested_on, recovery_ok; the embryo kind also has `embryo_stage`); box kind `cage`
  (slotless, capacity 5). Included by `angenet`.
- `angenet` (a **bundle**, **version 1**) — `include: [base, nucleic_acids, biosamples, drosophila,
  c_elegans, zebrafish, mouse]`, no kinds/mixins/rules/status sets of its own. `proteins` arrives
  through `nucleic_acids`; `biosamples` is listed because `animal_biosample.entity_kinds` name the
  four organism line kinds, and only this union loads them (see the biosample split above). Every
  organism line is an entity, its colony a stock, and an aliquot is one animal or one housing unit's
  population; a cross is its progeny stock's `dam`/`sire` refs, and a litter is a `mouse_cohort` with
  `genotype` NULL whose split stocks point back through `parent_stock_id`. `presets/README.md` has
  the catalogue, the include graph and the litter recipe; `docs/preset-parts.md` §3 is the model.
