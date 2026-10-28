# 27 · Split molbio into parts (molbio 2 → 3)
Depends on: 22, 25 · Design: `docs/preset-parts.md` §2 · Issue: git-bug `0a9acec`

Goal: turn molbio into a bundle of reusable parts, so cellbio and angenet can share them without
duplicate kind names.

## Decisions (settled, do not re-open)

- New flat part files in `presets/`, each `version: 1`:
  - `nucleic_acids`: `plasmid`, `plasmid_prep` (moved from molbio, unchanged except as below), plus the
    new `oligo` entity and `oligo_stock` stock (fields in design §2.1).
  - `bacteria`: `bacterial_strain`, `bacterial_glycerol`, `bacterial_pellet`; `include: [base, nucleic_acids]`.
  - `proteins`: `protein_construct`, `protein_prep`; `include: [base]`. `plasmid.construct` refers to
    `protein_construct`, so decide which part includes which and record why. Preferred: `nucleic_acids`
    includes `proteins`, because the plasmid references the construct. A part never includes a bundle.
  - `cell_culture`: `cell_line`, `cell_vial`, `cell_pellet`; `include: [base, nucleic_acids]`.
- `base` gains the box kind `incubator` (slotless) and, if ticket 25 hasn't already added them, the
  status sets `tube`, `cryo` and `animal`. Bump `base`'s `version:` and add its step.
- `molbio.yaml` becomes a bundle: `include: [base, nucleic_acids, bacteria, proteins, cell_culture]`, no
  kinds, `version: 3`. Mixins move to the part that uses them (`sequence_fields` → `nucleic_acids`, also
  used by `protein_construct`: put it in the lowest part that both include, and record where). Rules MB01–MB04
  move to the part owning their tables, keeping their ids.
- Every part's `description:` says what it covers and which bundles include it. New kinds end with
  "Adapted from BioManager (MIT)."
- Migration `migration/presets/molbio/2-to-3/`: no table renames. Add the `oligo` / `oligo_stock` tables,
  replace the ledger's preset copies with molbio v3 + all parts, and update `preset_versions`
  (molbio → 3, plus a key per part at 1). Use ticket 21's helper.
- `presets/README.md` (new): the part/bundle table from design §2, the naming rule (entity = what it is,
  stock = physical form), how to add a part, and the "not modelled" list from design §1.

## Files

- `presets/{base,molbio,nucleic_acids,bacteria,proteins,cell_culture}.yaml`, `presets/README.md`
- `migration/presets/molbio/2-to-3/`, `migration/presets/base/N-to-N+1/`
- tests that assert molbio's file layout or `preset_versions`
- `docs/spec.md` §4 (parts and bundles), `docs/migration.md` (preset names), `skills/tetramark/SKILL.md`

## Constraints

- Every table name and kind name in a molbio ledger is unchanged, apart from the new oligo tables.
- Real `dolt`, no mocks. Do not commit; leave the tree dirty. Do not touch real ledgers; to check the
  path zsqlab10 will take, test on a copy (`spec/3-to-4`, then `molbio 2-to-3`).

## Verify

- `tm init --preset molbio` → the same kind tables as molbio v2, plus `entity_oligo` and
  `stock_oligo_stock`; `tm check` passes; `preset_versions` lists molbio 3 and each part.
- `tm init` with `presets: [molbio, cellbio]` loads without duplicate errors once ticket 28 exists; until
  then, test `[molbio]` plus a test-only preset that also includes `cell_culture`.
- A molbio-v2 fixture ledger with data migrates to v3 with every row kept and `tm check` clean.
