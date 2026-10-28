# 22 · molbio v2: kind renames, cell kinds, `glycerol_pct` moves to the stock
Depends on: 20, 21 · Spec: §4, §5 · Issue: git-bug `2f6a0d5`

Goal: clearer kind names, kinds for eukaryotic cells, and no tube-level properties in molbio, plus
the matching `migration/presets/molbio/1-to-2/` step.

## Decisions (settled, do not re-open)

The principle behind them: **all aliquots of one stock are the same.** They differ only in location,
quantity, unit, status and label. Tubes that differ in a property (concentration, freeze-thaw cycles,
glycerol %) are different stocks. Properties specific to a type of material live on the stock kind.

Renames (`presets/molbio.yaml`, bump `version: 2`):

| object | old | new |
|---|---|---|
| entity | `construct` | `protein_construct` |
| entity | `strain` | `bacterial_strain` |
| stock | `bacterial` | `bacterial_glycerol` |
| stock | `pellet` | `bacterial_pellet` |

Also update every `ref to:` (plasmid's `construct` field → `to: protein_construct`; keep the column
name `construct`), every `entity_kinds:` list, all descriptions, and the MB01–MB04 SQL table names.

Move `bacterial.aliquot_fields.glycerol_pct` → a **stock** field on `bacterial_glycerol` (decimal 5,2,
default 15). MB02 then checks `stock_bacterial_glycerol`. molbio has no `aliquot_fields` left
(ticket 23 removes the feature itself).

New kinds:

- `cell_line` (entity): `mixins: [named_fields]`; `species` (string 64); `cell_type` (string 64, cell
  type / tissue); `source` (string 128, ATCC, collaborator…); `genotype` (text, modifications /
  markers); `parental` (ref → `cell_line`, for derived or stable lines; verify that the loader accepts a
  self-ref and fix it if not). A stable line expressing a plasmid is its own `cell_line` with `parental`
  set, not a stock pointing at a plasmid.
- `cell_vial` (stock, a cryovial): `entity_kinds: [cell_line]`; `passage` (int); `medium` (string 64);
  `freezing_medium` (string 64); `cells_per_vial` (int); `dmso_pct` (decimal 5,2, default 10).
- `cell_pellet` (stock): `entity_kinds: [cell_line, plasmid]` (a plasmid only for transient
  transfection / transduction harvests); `cell_count` (int); `passage` (int); `transfection` (string
  128, reagent/method or virus); `harvested_on` (date).

Write descriptions in the same style as the existing kinds. Add a rule only where it obviously helps
(e.g. `dmso_pct` between 0 and 100). Keep it minimal.

Migration step `migration/presets/molbio/1-to-2/`:

- `renames.yaml` with the four renames above, applied through ticket 21's hook.
- Copy `glycerol_pct` from `aliquot_bacterial` into the new `stock_bacterial_glycerol.glycerol_pct`
  column. **Refuse** if aliquots of one stock disagree on the value, and list the offending stocks.
  Then drop the `glycerol_pct` aliquot column / `aliquot_bacterial` table.
- Add the new kind tables (additive) and replace `<ledger>/presets/molbio.yaml` (and `base.yaml` if it
  changed) with the repo copies. Bump `preset_versions.molbio` to 2.
- README manual-steps section: the old→new name table, plus
  `grep -nE "\b(construct|strain|bacterial|pellet)\b|entity_construct|entity_strain|stock_bacterial\b|stock_pellet|aliquot_bacterial" <ledger>/kinds <ledger>/rules`,
  with the instruction to rewrite those hand-written rules and local kinds before running `tm check`.

## Files

- `presets/molbio.yaml`
- `migration/presets/molbio/1-to-2/{README.md,renames.yaml,migrate.py}`
- tests touching molbio kind names (`grep -rn "construct\|pellet\|bacterial\|strain" tests/`), `tests/test_migrate.py`
- `docs/migration.md` (the kind mapping tables in §3/§6, pellet section), `README.md`, `skills/tetramark/SKILL.md`, `docs/spec.md` examples

## Constraints

- `library.yaml` and `base.yaml` are unchanged unless they reference a renamed kind.
- No `renamed_from` in any preset.
- Real `dolt`, no mocks. Do not commit; leave the tree dirty. Do not touch real ledgers.

## Verify

- A fresh `tm init --preset molbio` produces `entity_protein_construct`, `entity_bacterial_strain`,
  `entity_cell_line`, `stock_bacterial_glycerol` (with `glycerol_pct`), `stock_bacterial_pellet`,
  `stock_cell_vial`, `stock_cell_pellet`, and no `aliquot_*` extension table. `tm check` passes.
- Migration test: init with molbio v1 (fixture copy of today's file), add data of every old kind,
  including bacterial aliquots with `glycerol_pct` → run `1-to-2` → all rows kept under the new names,
  `kind` values rewritten, `glycerol_pct` now on the stock, `preset_versions.molbio == 2`, `tm check`
  clean. The conflicting-glycerol case refuses and writes nothing.
