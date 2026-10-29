# Wave 10 delivery record (tickets 25–29, release 0.3.0)

**Date**: 2026-10-29 · **Scope**: preset parts, status sets, lineage, `cellbio` and `angenet` bundles ·
**Source issue**: git-bug `0a9acec` ("BioManager Presets", closed with this release) · **Design**: the
wave-10 design doc, retired with the wave (its model now lives in [`spec/08-presets.md`](../spec/08-presets.md)
and [`presets/README.md`](../../presets/README.md))
· **Plan**: [`plans/wave10/plan.json`](../../plans/wave10/plan.json) (loomerto, three views next to it).

All five tickets are merged on `main`; `spec_version` is **4**, the release is **0.3.0**.

## What shipped

| # | Change | Merge commit |
|---|---|---|
| 25 | Status sets per stock kind with `to:` transitions, `--force --note` + `aliquot_event.forced`, in-stock stock views (`aliquots_in_stock`/`aliquots_total`/`quantity_in_stock` + `unit`, `status` ∈ active/depleted/empty), spec `3-to-4`, base/library preset steps | `6552b9b` |
| 26 | `lineage: true` on `ref` fields (fingerprint-visible, no DDL) and `tm lineage ID [--up\|--down] [--depth N] [--json] [--format text\|dot]` | `a635762` |
| 27 | molbio becomes a v3 bundle over the v1 parts `nucleic_acids`, `bacteria`, `proteins`, `cell_culture`; new `oligo`/`oligo_stock`; base v3 (`incubator`); `molbio/2-to-3` + `base/2-to-3`; `presets/README.md` | `902f6d0` |
| 28 | `immuno`, `viral_vectors`, `biosamples` parts; `cellbio` v1; `cell_culture` v2 (`selection`, `mycoplasma`, `mycoplasma_tested_on`, `cell_biosample`) + `1-to-2` | `f027bd9` |
| 29 | `drosophila`, `c_elegans`, `zebrafish`, `mouse` parts and the `angenet` v1 bundle; litter/cross/colony/cryo recipes | `49e168a` |

No core-table change was needed for the bundles. `aliquot` remained a generic tube; the organism
model lives in preset kinds (`fly_line`/`fly_culture`, `worm_strain`/`worm_culture`/`worm_cryo`,
`fish_line`/`fish_group`/`fish_clutch`/`fish_sperm_cryo`, `mouse_strain`/`mouse_cohort`/`mouse_sperm_cryo`/
`mouse_embryo_cryo`, box `cage`). A litter is a `mouse_cohort` with `genotype` NULL; a cross is its
progeny stock with `dam`/`sire` refs to aliquots.

## Decisions worth remembering

- **molbio stays version 2 in ticket 25.** The single `migration/presets/molbio/2-to-3` (ticket 27)
  installs the parts model *and* the status sets, so an existing ledger runs one preset step, not two.
- **Status sets live in presets** (`status_sets:` top level, `statuses:` on every stock kind); the
  ledger-wide `ledger.yaml statuses:` key is gone and the loader refuses it with a pointer at
  `migration/spec/3-to-4`.
- **Views show what is in stock**: ticket 23's per-status counts and `total_quantity` were replaced by
  `aliquots_in_stock`/`aliquots_total`/`quantity_in_stock` + `unit` and a three-value `status`.
- **Lineage edges** come from the core links (`entity`, `parent_stock_id`, `source_aliquot_id`) plus
  `lineage: true` refs; an aliquot named by a relation gets one upstream edge to its owning stock with
  the same `via`, so `dam`/`sire` chains reach the parent stock.
- **`sequence_fields` lives in `proteins`**; `nucleic_acids` includes `proteins`, because a plasmid
  points at its protein construct.
- **Biosamples were split** (option a, no core change): `cell_biosample` in `cell_culture`,
  `animal_biosample` in `biosamples` (loadable only with the four organism parts — `entity_kinds` may
  only name kinds the same ledger loads).
- **Historical preset steps ship frozen copies** (`migration/presets/*/N-to-N+1/*-N.yaml`) and place
  them with `vendor_preset_files`; a bundle step records the part counters it announces with
  `MigrationStep.preset_versions_extra`. The repository `presets/` only ever holds the current version.
- Pre-spec-4 ledgers stay readable **only** through the migration path (`allow_legacy_statuses`: the
  legacy `statuses:` key and the implied spec-3 default set); normal `tm` reads refuse them.

## Upgrade order for an existing ledger

A ledger at spec 3 / molbio 2 (the shape 0.2.0 left, e.g. zsqlab10):

1. back up the ledger directory;
2. `migration/spec/3-to-4/migrate.py --ledger PATH` (refuses a custom `statuses:` list or an
   aliquot whose status is outside its stock kind's set, listing the rows);
3. `migration/presets/base/2-to-3/`, `migration/presets/molbio/2-to-3/`,
   `migration/presets/cell_culture/1-to-2/` (the three preset steps are independent; run them in any
   order, each is idempotent and only its own counter moves);
4. `tm check` and `tm schema plan` (plan must be empty).

A pre-0.2.0 ledger (spec 1/2, molbio 1) still needs the 0.2.0-era checkout for `molbio/1-to-2` and
`spec/1-to-2`/`2-to-3`; `docs/guides/migrating-a-legacy-ledger.md` and the step READMEs carry that
sequence. No real ledger was migrated in this wave.

## Verification

- Final full suite on `main` after the last merge: **844 passed, 1 skipped** (real Dolt, `-n 4`,
  725 s). The skip is the pre-existing legacy-fixture drift guard: the checked-in molbio-1 preset copy
  now carries the spec-4 `statuses:` key, which the 0.2.0-era builder cannot load; rebuilding that
  fixture is a follow-up (the checked-in ledger itself is untouched and its migration is covered).
- Acceptance (fresh workspace, repo presets, real `tm`): `tm init` + `tm check` + `tm schema plan` for
  `molbio` (22 kinds, 8 rules, 27 checks), `cellbio` (23/9/28), `angenet` (30/6/31) and
  `[molbio, cellbio]` (26/10/31) — every plan reported *schema is up to date*.
- Ticket-level evidence (targeted suites, manual CLI smoke on copies) is in the merge commits and the
  loomerto plan's block notes.

## Open items

1. **zsqlab10 follow-up (needs the user's go-ahead)**: run the upgrade order above on a copy, compare
   the row counts/view cross-checks as in [`zsqlab10-migration.md`](zsqlab10-migration.md), then do the
   real run on `<ledger>` with a pre-migration backup and a restore tag.
2. **git-bug `0a9acec`**: closed on 2026-10-29 with a comment pointing at 0.3.0 and this record.
3. **Legacy-fixture drift guard**: rebuild `tests/data/ledgers/molbio-v1` (or teach the builder to strip
   the spec-4 keys) so the guard stops skipping — small, separate task.
4. **Wave 11 (ticket 30, box views)** is unblocked: it needs spec 4 and the parts/preset model from this
   wave.
