# 28 · `cellbio` bundle: `immuno`, `viral_vectors`, `biosamples`
Depends on: 26, 27 · Design: `docs/preset-parts.md` §2, §2.1 · Issue: git-bug `0a9acec`

Goal: a preset for cell-biology labs, assembled from shared parts plus three new ones.

## Decisions (settled, do not re-open)

- New parts, each `version: 1`, fields as in design §2.1, descriptions ending with "Adapted from
  BioManager (MIT).":
  - `immuno`: `antibody` (entity), `antibody_lot` (stock, `statuses: tube`).
  - `viral_vectors`: `virus` (entity; `payload` ref → plasmid, **without** `lineage`), `virus_prep` (stock,
    `statuses: tube`); `include: [base, nucleic_acids]`.
  - `biosamples`: `biosample` (stock, `statuses: tube`). It must accept cell lines (cellbio) and
    animal lines (angenet) as its entity, yet each bundle loads only some of those kinds. First check
    what the loader does with an `entity_kinds` entry that isn't loaded. Then pick the smallest option
    and record it in the part's description and in `presets/README.md`: (a) split into `cell_biosample`
    (in `cell_culture`) and `animal_biosample` (in `biosamples`, used by angenet only); or (b) a loader
    change letting `entity_kinds` skip unloaded kinds. Prefer (a), which needs no core change. The sample's
    origin uses core `source_aliquot_id`.
- `cell_culture` (version bump + `migration/presets/cell_culture/1-to-2/`): `cell_vial` gains
  `selection`, `mycoplasma` (enum not_tested/negative/positive, default not_tested) and
  `mycoplasma_tested_on`, plus `statuses: cryo` if ticket 25 didn't set it. `cell_line.parental` has
  `lineage: true` (ticket 26).
- Bundle `presets/cellbio.yaml`, `version: 1`:
  `include: [base, nucleic_acids, cell_culture, immuno, viral_vectors, biosamples]` (with `biosamples`
  replaced by the option (a) kinds if chosen).
- Rules: add one only where obviously useful (e.g. `antibody_lot.expires_on >= received_on`, a positive
  `virus_prep.titer`).

## Files

- `presets/{immuno,viral_vectors,biosamples,cellbio,cell_culture}.yaml`, `presets/README.md`
- `migration/presets/cell_culture/1-to-2/`
- tests: init + add one object of each new kind + `tm check`; a ledger listing `[molbio, cellbio]`

## Constraints

- No core change unless option (b) is chosen. In that case keep the loader change minimal and list it in
  the report.
- Real `dolt`, no mocks. Do not commit; leave the tree dirty.

## Verify

- `tm init --preset cellbio` creates the expected tables and passes `tm check`. `tm lineage <virus>`
  doesn't show `payload` as an edge, and `tm lineage <cell_line>` shows `parental`.
- `presets: [molbio, cellbio]` loads (shared parts once) and passes `tm check`.
- A biosample made from a cell vial aliquot (`source_aliquot_id`) appears under it in `tm lineage`.
