# 8. Presets

Presets are YAML schema documents shipped with Tetramark and also kept in the repository's `presets/`
directory. The wheel packages that directory as data (<site-packages>/tetramark/presets, P8), so a
bare `--preset NAME` works from an installed tool with no checkout and no configuration. A **preset**
is a single YAML file containing kinds, mixins and rules; a preset given by path may also be a
directory containing `preset.yaml` plus optional `kinds/*.yaml` and `rules/*.yaml`. `preset:` is the
name, `version:` an integer, `include:` a list of bare preset names (path includes cannot be copied
into a self-contained ledger, see below).

A **part** is a preset meant to be included: it owns a coherent slice of the model (`base`,
`nucleic_acids`, `bacteria`, `proteins`, `cell_culture`, …) and includes only the parts it
references. A **bundle** is a preset whose only content is `include:` — no kinds, mixins or rules —
so one bundle can assemble several parts and two bundles can share them. Includes are
loaded once each, so listing `molbio` and `cellbio` in one ledger loads the shared parts once; kind
names remain one global namespace. A part never includes a bundle. `presets/README.md` has the
part/bundle table, the include graph and the rule for adding a part.

- **Selecting.** `tm init PATH --preset ...` (and `tm adopt PATH --preset ...`) is repeatable. A
  value containing `/` (or `\`) or ending in `.yaml`/`.yml` is a **path**, used as-is from the
  caller's cwd; it needs no preset directory. Anything else is a bare **name**, resolved in this
  order (P8): (1) `$TETRAMARK_PRESETS_DIR` when set — used verbatim, so a wrong value fails loudly
  instead of silently falling back; (2) the presets bundled with tetramark (the checkout's
  `presets/`, or `<site-packages>/tetramark/presets` in an installed wheel). When neither exists the
  command is a usage error naming `$TETRAMARK_PRESETS_DIR` and the expected path. The 0.4
  `presets_dir` workspace setting is gone; a bare name needs no configuration. A preset's own name
  (its `preset:` field, else its file stem/directory name) must match `[A-Za-z][A-Za-z0-9._-]*`: it
  is the filename of the ledger's copy, so a path-like name is rejected.
- **Copying.** `tm init` (and `tm adopt`) copies the selected preset **and its `include:` closure**
  into the ledger: `<ledger>/presets/<name>.yaml` for a YAML preset, `<ledger>/presets/<name>/`
  (with `preset.yaml`, `kinds/` and `rules/`) for a directory preset. The copies are byte-identical
  to the sources. Every `include:` must be a bare preset name that matches the included preset's own
  `preset:` field; a path include or a mismatching name is rejected before anything is written,
  because the flattened copy could not resolve it. `include:` entries inside a copy then resolve
  against the copy directory, so the closure is copied too and the ledger is self-contained. After
  writing the copy, `tm init`/`tm adopt` reload the ledger from its own `presets/` and fail (rolling
  the ledger back) unless that copy reproduces exactly the planned schema.
- **Resolution.** A ledger opens presets **only** from `<ledger>/presets/`. `ledger.yaml` lists bare
  names (`presets: [molbio]`) and `ledger_meta.preset_versions` is written exactly as before. If a
  copy is missing — including an `include:` target such as `base` — opening fails with the
  expected file path and says that `migration/` or a manual copy fixes it; there is
  no fallback to `$TETRAMARK_PRESETS_DIR` or the bundled presets, because a silent fallback would
  let the ledger drift from its recorded `preset_versions`. Changing a source preset after init
  therefore does **not** change an existing ledger — replacing its `<ledger>/presets/` files is an
  explicit schema change. The copies live on disk next to `ledger.yaml` and are **not** stored in
  Dolt, so cloning a ledger elsewhere (or restoring a database without its directory) must copy
  `<ledger>/presets/` along with `ledger.yaml`.
- **Inspecting.** `tm presets list` lists the presets a bare name resolves from
  (`$TETRAMARK_PRESETS_DIR` or the bundled copies); `tm presets show NAME|PATH` prints one
  include-resolved preset. `tm config` exposes only `author`; there is no `presets_dir` setting.
- **Versions.** A preset or part bumps its `version:` — and gets exactly one
  `migration/presets/<name>/N-to-N+1/` folder — whenever it changes the stored shape **or**
  relaxes or tightens a **write check**, even when no DDL moves and no stored value changes: two
  ledgers recording the same `preset_versions` must accept the same writes, so a bound one copy
  enforces and another does not needs its own counter (base 5 → 6 drops the slotless `capacity`,
  which was enforced on writes). A **read-only** change — `lineage: true` on a `ref` field, say —
  bumps nothing: it moves no DDL and no stored value and validates no write differently, so it only
  makes the ledger's fingerprint stale and is refreshed by `tm schema apply` — a meta refresh, not
  a step.

Every stock kind uses the same fixed aliquot status set (§1.1); a preset never declares `statuses:`
or a status set. Life-cycle detail that used to be a status — an animal's fate, a culture's
condition — is an optional enum stock property (for example `fate` or `condition`) or an event
message. The shipped organism parts carry `fate` (euthanized, found_dead, transferred, cancelled)
on the mouse cohort and the fish group/clutch, `fate` (discarded, lost) on the fly culture and
`condition` (growing, starved, contaminated) on the worm culture. Each of them is **optional**: no
`required:` flag and no default, so a stock that has not recorded the detail simply leaves it NULL.

Preset catalogue:

- `base` (**version 6**) — box kinds `grid9x9` (A–J!I × 1–9), `grid8x12` (A–H × 1–12), `rack` (named),
  `shelf`/`freezer`/`drawer` (slotless, unbounded) and `incubator` (slotless, unbounded);
  entity kind `item` (name, category, description); stock kind `lot` (supplier, catalog_no,
  received_on, expires_on).
  Version 6 drops the slotless `capacity` of `shelf`/`freezer`/`drawer` (12/6/1): how many racks
  sit on a shelf, how many shelves a freezer has and how many boxes a drawer takes is a property of
  that piece of furniture, not of the kind. The bound was enforced on writes, so a base-5 ledger
  refused the (N+1)th tube or child box (`box 'X' is full (capacity N aliquots + child boxes)`).
  The hand-run step is `migration/presets/base/5-to-6/`: a README-only folder with a frozen
  `base-6.yaml` that replaces the ledger's copy, and one `tm schema apply` that writes the counter,
  the fingerprint, `tetramark_version` and the `ledger.yaml` copy — no DDL and no row change,
  because the capacity was never stored. `migration/presets/base/4-to-5/` installs the frozen
  `base-5.yaml` (base 5, capacities and all), so the ordered 4 → 5 → 6 path stays reproducible.
  Version 5 drops the `status_sets:` block and the per-kind `statuses:` keys: every aliquot now
  uses the fixed status set of §1.1, so `base` declares no status vocabulary. Version 4 renamed the
  `grid96` kind to `grid8x12` with the same geometry (spec 5, ticket 30); the hand-run step is
  `migration/presets/base/3-to-4/`.
- `proteins` (**version 4**, includes `base`) — entity `protein_construct` (sequence, length_aa,
  mature_sequence, molecular_weight_da, pI, extinction coefficients as computed, tag) and stock
  `protein_prep` (concentration_mg_ml, buffer, purification_id, purity_pct). It also owns the
  `sequence_fields` mixin (nucleotide `sequence` + `length_bp`), which `plasmid` (`nucleic_acids`)
  loads through this part. Version 2 (ticket rq9) validates the alphabets: `protein_construct`'s
  `mature_sequence` carries `pattern: [ACDEFGHIKLMNPQRSTVWYXBZUO*]+` (IUPAC amino-acid codes plus
  the ambiguity/stop symbols) and the mixin's nucleotide `sequence` carries
  `pattern: [ACGTURYKMSWBDHVN]+`. Patterns are validation-only — the app checks them after sequence
  normalisation and the generated `dolt_tests` mirror them — so the step
  `migration/presets/proteins/1-to-2/` changes no DDL but makes `tm check` report existing rows
  outside the alphabet. Version 3 drops the `statuses:` key: the prep's aliquots use the fixed
  status set. Version 4 (ticket `Tetramark-3ht`) stops `protein_construct` from loading the
  nucleotide mixin: a construct's `sequence` is its full-length **amino-acid** sequence (the legacy
  `entity_constructs.sequence`), so it gets its own `sequence` with the amino-acid pattern and the
  computed length column is renamed `length_bp` → `length_aa`; `plasmid` keeps the nucleotide
  `sequence_fields`. The step is `migration/presets/proteins/3-to-4/` (an in-place rename, no data
  change). Included by the `molbio` bundle; `nucleic_acids` includes it because a plasmid points
  at the protein design it expresses.
- `nucleic_acids` (**version 3**, includes `base`, `proteins`) — entities `plasmid` (sequence,
  backbone, resistance, ref `construct` → protein_construct with `lineage: true`) and `oligo`
  (sequence, direction fwd/rev, target, modification, purification, tm_c, `length_nt`/`gc_pct`
  computed from the sequence); stocks `plasmid_prep` (concentration_ng_ul, prep_method,
  elution_buffer) and `oligo_stock` (concentration_um, solvent). Version 2
  (ticket rq9) adds `pattern: [ACGTURYKMSWBDHVN]+` to `oligo.sequence`; its step is
  `migration/presets/nucleic_acids/1-to-2/`, which requires proteins already at 2. Version 3 drops
  the `statuses:` keys: the preps and oligo tubes use the fixed status set. Included by `molbio`,
  `cellbio` and `angenet`.
- `bacteria` (**version 2**, includes `base`, `nucleic_acids`) — entity `bacterial_strain` (genotype,
  species, source); stocks `bacterial_glycerol` (entity_kinds plasmid|bacterial_strain; strain,
  medium, batch, glycerol_pct **on the stock**) and `bacterial_pellet` (culture volume, OD600,
  induction; aliquots carry volume via core quantity). Version 2 drops the `statuses:` keys: both
  stocks use the fixed status set. Included by `molbio`.
- `cell_culture` (**version 3**, includes `base`, `nucleic_acids`) — entity `cell_line` (species,
  cell_type, source, genotype, `parental` ref → cell_line with `lineage: true`); stocks `cell_vial`
  (passage, medium, freezing_medium, cells_per_vial, dmso_pct, and since v2 `selection`, `mycoplasma`
  enum not_tested/negative/positive default not_tested, `mycoplasma_tested_on`),
  `cell_pellet` (cell_count, passage, transfection, harvested_on; a plasmid records a transient
  transfection) and `cell_biosample` (a biosample taken from a cell line; `entity_kinds: [cell_line]`;
  `sample_type` enum tissue/blood/dna/rna/serum/other,
  tissue, collected_on, storage_temp_c; the origin is the core `source_aliquot_id`). Version 3
  drops the `statuses:` keys — a cryovial is `stored` in the freezer and `consumed` once thawed,
  like every other aliquot; there is no cryo vocabulary. Included by `molbio` and `cellbio`. The
  v1 → v2 step is `migration/presets/cell_culture/1-to-2/`.
- `immuno` (**version 2**, includes `base`) — entity `antibody` (`named_fields`; host, clonality enum
  monoclonal/polyclonal/recombinant, clone, isotype, reactivity, conjugate, applications, rrid) and
  stock `antibody_lot` (supplier, catalog_no, lot_no, concentration_mg_ml, recommended_dilution,
  received_on, expires_on). Rule `IMM01`: a lot cannot expire before it was
  received. Version 2 drops the `statuses:` key: the lot uses the fixed status set. Included by
  `cellbio`.
- `viral_vectors` (**version 2**, includes `base`, `nucleic_acids`) — entity `virus` (`named_fields`;
  virus_type enum aav/lentivirus/retrovirus/adenovirus/other, serotype, `payload` ref → plasmid
  **without** `lineage: true` — a plain field, not a descent edge, promoter, biosafety_level) and stock
  `virus_prep` (titer, titer_unit enum vg_ml/tu_ml/pfu_ml, made_on, made_by). Rule
  `VIR01`: a recorded titer is positive. Version 2 drops the `statuses:` key: the prep uses the
  fixed status set. Included by `cellbio`.
- `biosamples` (**version 2**, includes `base`) — stock `animal_biosample` (`sample_type` enum
  tissue/blood/dna/rna/serum/other, tissue, collected_on, storage_temp_c; the
  origin is the core `source_aliquot_id`), `entity_kinds` = the organism line kinds
  (`fly_line`, `worm_strain`, `fish_line`, `mouse_strain`). It is the **animal side** of the biosample
  model and is included by `angenet` only, loaded together with the organism parts that own those
  entity kinds: a stock's `entity_kinds` may name only entity kinds the same ledger loads, and the
  loader refuses any other (`'<name>' is not an entity kind`) — there is no optional switch. The
  **cell side** is `cell_biosample` in `cell_culture`, which is why `cellbio` needs no biosamples
  part (design §2.1, ticket 28 option (a), no core change). Version 2 drops the `statuses:` key:
  the sample uses the fixed status set.
- `molbio` (a **bundle**, **version 3**) — `include: [base, nucleic_acids, bacteria, proteins,
  cell_culture]`, no kinds or rules of its own. It keeps the name existing ledgers already list,
  and every kind/table name of a molbio ledger is unchanged by the parts split; the new tables of
  that step are the `nucleic_acids` `entity_oligo`/`stock_oligo_stock` pair (and base v3's
  `box_incubator`). Ticket 28's `cell_culture` 2 adds `stock_cell_biosample` to a molbio ledger that
  runs that step.
  Molbio has no per-tube fields: aliquots are equal portions of one stock, so a property belongs on
  the stock kind. The bundled rules moved with their tables and kept their ids: `MB01` (plasmid
  sequence) → `nucleic_acids`, `MB02` (glycerol percentage) → `bacteria`, `MB03`/`MB04` (protein
  preps/constructs) → `proteins`, `MB05` (DMSO) → `cell_culture`. Spec 3 removed the
  per-stock-kind tube-field schema key entirely, so a molbio-1 ledger is upgraded by
  `migration/presets/molbio/1-to-2/` (and then `migration/spec/2-to-3/`), and a molbio-2 ledger by
  `migration/presets/molbio/2-to-3/` (plus `migration/presets/base/2-to-3/` for base's counter).
- `library` (**version 3**, includes `base`) — entity `book` (title, authors, isbn pattern, year);
  stock `copy` (acquired_on, condition enum). Exists to prove generality; used in
  tests. Version 3 drops the `statuses:` key: the copy uses the fixed status set.

- `cellbio` (a **bundle**, **version 1**) — `include: [base, nucleic_acids, cell_culture, immuno,
  viral_vectors]`, no kinds/mixins/rules of its own. `proteins` arrives through
  `nucleic_acids`; `biosamples` is deliberately absent (it names the organism line kinds of the
  `angenet` parts). One ledger can list `presets: [molbio, cellbio]`: the shared parts load once, and
  the kind set is the union (molbio brings `bacteria`, cellbio brings `immuno`/`viral_vectors`).
- `drosophila` (**version 2**, includes `base`) — entity `fly_line` (`named_fields`; balancers,
  stock_centre, stock_number, flybase_id, `parental` ref → fly_line with `lineage: true`); stock
  `fly_culture` (a vial or cross vial with temperature_c, generation, food_type,
  virgins_collected_on, selection_marker, the optional `fate` enum (discarded, lost) and
  `dam`/`sire` ref → aliquot with `lineage: true`).
  Fly detail (crossed, discarded, lost) is a stock property or event message, not a status.
  Included by `angenet`.
- `c_elegans` (**version 2**, includes `base`) — entity `worm_strain` (`named_fields`; allele,
  lab_prefix, wormbase_id, source, `parental` → worm_strain with `lineage: true`); stocks
  `worm_culture` (a plate with temperature_c, plate_type, generation, stage and the optional
  `condition` enum (growing, starved, contaminated)) and
  `worm_cryo` (frozen_on, method, recovery_tested_on, recovery_ok). Worm detail (growing, starved,
  contaminated, discarded) is a stock property or event message, not a status. Worm selfers keep a
  single-parent strain lineage, so a culture has no `dam`/`sire`. Included by `angenet`.
- `zebrafish` (**version 2**, includes `base`) — entity `fish_line` (`named_fields`; zfin_name,
  allele, background, `parental` → fish_line with `lineage: true`); stocks `fish_group`
  (a tank or a tagged fish with birth_on, sex enum f/m/u, genotype,
  density_per_l, the optional `fate` enum (euthanized, found_dead, transferred, cancelled)
  and `dam`/`sire` → aliquot), `fish_clutch` (set_up_on,
  embryo_count, larvae_count, the same optional `fate` enum, `dam`/`sire`) and `fish_sperm_cryo`
  (frozen_on, method, recovery_tested_on, recovery_ok). There is no fish embryo cryo kind.
  Included by `angenet`.
- `mouse` (**version 2**, includes `base`) — entity `mouse_strain` (`named_fields`; background,
  supplier, mgi_id, `parental` → mouse_strain with `lineage: true`); stocks `mouse_cohort`
  (one individually tracked mouse per stock: birth_on, sex enum f/m/u, genotype, the optional
  `fate` enum (euthanized, found_dead, transferred, cancelled) and `dam`/`sire` →
  aliquot), `mouse_sperm_cryo` and `mouse_embryo_cryo` (frozen_on, method,
  recovery_tested_on, recovery_ok; the embryo kind also has `embryo_stage`); box kind `cage`
  (slotless, capacity 5). Included by `angenet`.
- `angenet` (a **bundle**, **version 1**) — `include: [base, nucleic_acids, biosamples, drosophila,
  c_elegans, zebrafish, mouse]`, no kinds/mixins/rules of its own. `proteins` arrives
  through `nucleic_acids`; `biosamples` is listed because `animal_biosample.entity_kinds` name the
  four organism line kinds, and only this union loads them (see the biosample split above). Every
  organism line is an entity and its colony is a stock; aliquots are equal portions of that stock
  (§1.1), so an organism that needs individual tracking is its own stock, not its own aliquot. A
  cross is its progeny stock's `dam`/`sire` refs, and a litter is a `mouse_cohort` with `genotype`
  NULL whose split stocks point back through `parent_stock_id`. An individually tracked animal is a
  stock with one aliquot (quantity 1, its ear tag/tag in the core aliquot `label`, its cage as the
  box); a group-counted population is a stock whose aliquots are equal housing units (quantity =
  headcount, unit `individuals`, the vial/plate/tank itself the aliquot in a rack, incubator or
  shelf), with its life-cycle detail in stock properties or event messages. `presets/README.md` has
  the catalogue, the include graph and the colony/cross/litter/cryo recipes.
