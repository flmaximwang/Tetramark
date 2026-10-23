# 07 · Built-in presets
Depends on: 05, 06  · Spec: §8

Write `src/doltledger/presets/{base,molbio,library}/preset.yaml` per §8. Derive molbio fields from the old
zsqlab schemas — read (do not modify) `/Users/maxim/Documents/Lab/Dolt/cli/zsqlab08_build.sql`,
`zsqlab10_build.sql`, `zsqlab10_stock_pellets.sql`, `zsqlab10_pellet_aliquots_1_create_load.sql` and README §对象.
Include the extinction-coefficient computed columns from zsqlab08 (translate the generated-column expressions).
Add 2–4 meaningful custom rules per preset (severity mix). Each kind and field gets a `description`.

Tests: each preset loads, inits a ledger, and passes `ledger check` empty (once 08 lands, else just init);
`library` end-to-end: add book, copy stock, aliquot in a box (use raw SQL if 10 not merged yet — keep it minimal).
