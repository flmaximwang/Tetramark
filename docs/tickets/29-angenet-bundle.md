# 29 · `angenet` bundle: drosophila, c_elegans, zebrafish, mouse
Depends on: 25, 26, 27, 28 · Design: `docs/preset-parts.md` §3 · Issue: git-bug `0a9acec`

Goal: an animal-genetics preset. Lines are entities, colonies are stocks, individuals or housing-unit
populations are aliquots, and crosses are progeny stocks that point to their parents.

## Decisions (settled, do not re-open)

- Model (design §3): a stock is a group of organisms of one line treated as **identical** (same cohort or
  cross, birth date, sex, genotype). An aliquot is one animal (individually counted: mouse, tagged fish;
  `label` = tag, box = `cage`) or one housing unit's population (fly vial, worm plate, fish tank; quantity =
  headcount, unit `individuals`).
- Parts, each `version: 1`, with kinds, fields and status sets exactly as in the design §3 table:
  - `drosophila`: `fly_line`; `fly_culture` (`statuses: fly`); the status set `fly`.
  - `c_elegans`: `worm_strain`; `worm_culture` (`statuses: worm`); `worm_cryo` (`statuses: cryo`); the set `worm`.
  - `zebrafish`: `fish_line`; `fish_group`, `fish_clutch` (`statuses: animal`); `fish_sperm_cryo` (`cryo`).
  - `mouse`: `mouse_strain`; `mouse_cohort` (`statuses: animal`); `mouse_sperm_cryo`, `mouse_embryo_cryo`
    (`cryo`); box kind `cage` (slotless, capacity 5).
  - Each organism entity has `named_fields` + `parental` (ref → its own kind, `lineage: true`).
  - Progeny-capable stocks (`fly_culture`, `fish_group`, `fish_clutch`, `mouse_cohort`) have `dam` and
    `sire`: `type: ref`, `to: aliquot`, `lineage: true`.
  - `sex` is an enum (f/m/u for animals; fly cultures don't have it). `genotype` is text, NULL = untyped.
  - Field types: dates → `date`, counts → `int`, temperatures/densities → `decimal`, the rest as
    BioManager has them (text → `string`/`text` with sensible lengths).
- `animal`, `cryo` and `incubator` already live in `base` (tickets 25/27). Don't redefine them.
- Bundle `presets/angenet.yaml`, `version: 1`:
  `include: [base, nucleic_acids, biosamples, drosophila, c_elegans, zebrafish, mouse]` (adapt
  `biosamples` to ticket 28's choice). The bundle and each organism part credit BioManager in
  `description:`.
- The litter workflow needs no core change: litter = a stock with `genotype` NULL and one group aliquot.
  Genotyping creates per genotype × sex stocks with `parent_stock_id` = litter, and the litter aliquot
  goes to a final status. Document it as a recipe in `presets/README.md` and `skills/tetramark/SKILL.md`,
  with the exact `tm` commands.
- Plants, field crops, BioManager's "custom" organism and all time-based tracking are out of scope
  (already in the README's "not modelled" list).

## Files

- `presets/{drosophila,c_elegans,zebrafish,mouse,angenet}.yaml`, `presets/README.md`
- tests: `tests/test_angenet.py` (new)
- `skills/tetramark/SKILL.md` (organism recipes: register a colony, a cross, a litter split, a cryo thaw)

## Constraints

- No core change. If one turns out necessary, stop and report it rather than adding it here.
- Real `dolt`, no mocks. Do not commit; leave the tree dirty.

## Verify (in tests, through the CLI)

- `tm init --preset angenet` passes `tm check`.
- Mouse: strain → a breeding cohort of 2 (one f, one m aliquot in one `cage`) → a litter stock (`dam` /
  `sire` = those aliquots, one group aliquot with quantity 7) → split at genotyping into 2 stocks (with
  `parent_stock_id` = litter); the litter aliquot goes to a final status. `tm lineage <strain> --down`
  shows the chain, and `tm lineage <pup aliquot's stock> --up` reaches both parents.
- `animal` transitions: alive → euthanized works; euthanized → alive is refused without `--force`.
- Fly: a cross vial whose `dam` is a virgins vial aliquot. Worm: `worm_cryo` stored → thawed, then
  thawed → stored is refused.
- `v_stock_mouse_cohort` shows `aliquots_in_stock` dropping as mice are euthanized.
