# 24 · Migrate zsqlab10 to spec 3 / molbio 2
Depends on: 20–23 · Issue: git-bug `2f6a0d5`

Goal: bring the real ledger
`/Users/maxim/Documents/Lab/Dolt/zsqlab_10_gammaPFD-Fiber_2026.06.25` (today `spec_version 1`,
`preset_versions {"base": 1, "molbio": 1}`, no local `presets/` copy) up to date by running the
`migration/` steps. This is the first real use of them, so any friction found here gets fixed in the
step's README or script.

## Steps

1. Stop any server holding the ledger. Take a Dolt backup (`tm` backup/remote, or a directory copy to
   a scratch place). Create and switch to the branch `tetramark-upgrade`. All writes go there; the
   user reviews and merges.
2. Seed `<ledger>/presets/{molbio,base}.yaml` with the **v1** content (`git show 1163cb6:src/tetramark/presets/<name>.yaml`),
   because ticket 20 makes a ledger resolve presets only from its own copy. Commit it as
   `chore(ledger): vendor preset copies`.
3. `migration/spec/1-to-2` (dry-run first, then apply).
4. Rewrite the ledger's hand-written rules and local kinds that use old names. Known hits in
   `rules/legacy.yaml`: `s.kind = 'pellet'` (4×), `p.kind <> 'bacterial'`, `entity_construct` (2×);
   run the grep from `migration/presets/molbio/1-to-2/README.md` for the complete list. Keep each
   rule's meaning; only names change.
5. `migration/presets/molbio/1-to-2` (dry-run, then apply). Expected: 14 bacterial aliquots, each the
   only aliquot of its stock, so the `glycerol_pct` move cannot conflict.
6. `migration/spec/2-to-3` (dry-run, then apply).
7. `tm check` with no FAIL.

## Verify (write the numbers into a short report)

- Row counts unchanged: entity 15, stock 39 by kind (protein_prep 12, bacterial_glycerol 14,
  plasmid_prep 7, bacterial_pellet 6; recount live first, these are from 2026-10-28), aliquot 43,
  `aliquot_event` 43 + nothing deleted, alias count unchanged, boxes unchanged.
- `entity.kind` / `stock.kind` hold only new names; no `aliquot_*` extension table is left;
  `stock_bacterial_glycerol.glycerol_pct` = 15 for all 14 rows.
- `v_stock_bacterial_pellet` shows `aliquots_stored` / `aliquots_total` per pellet matching
  `SELECT stock_id, status, COUNT(*) FROM aliquot GROUP BY 1,2`.
- `ledger_meta`: `spec_version 3`, `preset_versions {"base": 1, "molbio": 2}`.
- Commit list on `tetramark-upgrade`: one commit per step, plus the rules rewrite and the preset seed.

## Constraints

- Only branch `tetramark-upgrade`. Never write to `main`, never push, never delete the backup.
- If a step refuses or fails, stop. Report the message, the last good commit and the reset command.
  Do not hand-patch the database around a migration script; fix the script (in the repo) instead.
- Leave the Tetramark repo tree dirty for review (no commit).
