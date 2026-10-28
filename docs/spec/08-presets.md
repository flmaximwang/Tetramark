# 8. Presets
Presets are YAML schema documents, not package data. They live in the repository's `presets/`
directory (`base.yaml`, `library.yaml`, `molbio.yaml`); the wheel ships no `*.yaml` files. A
**preset** is a single YAML file containing kinds, mixins and rules; a preset given by path may also
be a directory containing `preset.yaml` plus optional `kinds/*.yaml` and `rules/*.yaml`. `preset:`
is the name, `version:` an integer, `include:` a list of bare preset names (path includes cannot be
copied into a self-contained ledger, see below).

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

- `base` — box kinds `grid9x9` (A–J!I × 1–9), `grid96` (A–H × 1–12), `rack` (named), `shelf`/`freezer`/`drawer`
  (slotless); entity kind `item` (name, category, description); stock kind `lot` (supplier, catalog_no,
  received_on, expires_on).
- `molbio` (includes `base`, **version 2**) — derived from zsqlab08/10: entities `protein_construct`
  (protein: sequence, molecular_weight_da, extinction coefficients as computed), `plasmid` (sequence,
  backbone, resistance, ref protein_construct), `bacterial_strain` (genotype, species), `cell_line`
  (species, cell_type, source, genotype, `parental` ref → cell_line); stocks `bacterial_glycerol`
  (entity_kinds plasmid|bacterial_strain; strain, medium, glycerol_pct **on the stock**),
  `plasmid_prep` (concentration_ng_ul, prep_method), `protein_prep` (concentration_mg_ml, buffer,
  purification_id), `bacterial_pellet` (culture volume, OD600, induction; aliquots carry volume via
  core quantity), `cell_vial` (passage, medium, freezing_medium, cells_per_vial, dmso_pct) and
  `cell_pellet` (cell_count, passage, transfection, harvested_on; a plasmid records a transient
  transfection). Molbio 2 has no per-tube fields: all aliquots of one stock are the same, so a
  property belongs on the stock kind. Spec 3 removed the per-stock-kind tube-field schema key
  entirely, so a molbio-1 ledger is upgraded by `migration/presets/molbio/1-to-2/` and then
  `migration/spec/2-to-3/`.
- `library` (includes `base`) — entity `book` (title, authors, isbn pattern, year); stock `copy`
  (acquired_on, condition enum). Exists to prove generality; used in tests.

---
