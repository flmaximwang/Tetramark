# 24 record · zsqlab10 moved to spec 3 / molbio 2

**Date**: 2026-10-28 · **Ledger**: `/Users/maxim/Documents/Lab/Dolt/zsqlab_10_gammaPFD-Fiber_2026.06.25`
(Dolt 2.3.5, launchd `dolt sql-server` on `127.0.0.1:13308`) · **Status**: done; migrated on branch
`tetramark-upgrade` and fast-forwarded into the ledger's `main`. Backup taken first and restore-proven;
nothing pushed.

This was the first real run of `migration/` (ticket 24). The procedure was rehearsed on a restored copy
of the actual backup before any write; rehearsal and real run produced the same fingerprint and counts.

## Result

`tetramark-upgrade` = `main` (`qscm59hdnc…`) plus four commits; `main` now points at the same tip
(`o7734qkqrt40gnfqlrger5vdtu8spg4r`), fast-forward, so no `-b` is needed any more.

| # | Commit | Message | Tag created before it |
|---|---|---|---|
| 1 | `jrb0qrqpqs27q9c7epb78gn2c6lmmjrr` | `chore(ledger): vendor preset copies` (empty marker, see below) | — |
| 2 | `cqjimtipot1o0hsq16brom9vvpp46epu` | `migrate(ledger): spec 1 -> 2` | `pre-migrate-spec-1` |
| 3 | `msrp09eo8e6c87c50mrfkpgoj1n4kug8` | `migrate(ledger): molbio 1 -> 2` | `pre-migrate-molbio-1` |
| 4 | `o7734qkqrt40gnfqlrger5vdtu8spg4r` | `migrate(ledger): spec 2 -> 3` | `pre-migrate-spec-2` |

| Item | Before | After |
|---|---|---|
| `ledger_meta` | spec 1, `{"base":1,"molbio":1}`, 0.1.1 | **spec 3**, **`{"base":1,"molbio":2}`**, 0.2.0 |
| fingerprint | `b3c07281…` | `0371d4048c0f21f288fe99c52de48bf4beeff4f01f8e698b7528eb4b965dcbb8` |
| entity (15) | `construct` 7, `plasmid` 7, `strain` 1 | `protein_construct` 7, `plasmid` 7, `bacterial_strain` 1 |
| stock (39) | `bacterial` 14, `pellet` 6, `plasmid_prep` 7, `protein_prep` 12 | `bacterial_glycerol` 14, `bacterial_pellet` 6, `plasmid_prep` 7, `protein_prep` 12 |
| aliquot / events / alias / box / slot | 43 (32 stored, 11 pending) / 43 / 46 / 6 / 162 | unchanged |
| extension tables | `aliquot_bacterial` 14 rows | none (only core `aliquot`, `aliquot_event`) |
| `glycerol_pct` | 15.00 in `aliquot_bacterial` | 15.00 on all 14 `stock_bacterial_glycerol` rows |
| tables / views | 25 / 19 | 27 / 22 |
| `tm check` | 29 tests, 3 info gaps, exit 0 | 32 tests, same 3 info gaps (L07 2, L08 12, L09 3), exit 0 |
| `tm schema plan` | — | `schema is up to date` |

`v_stock_*` cross-check: for every kind view, `aliquots_stored`/`aliquots_total` agree with
`SELECT stock_id, status, COUNT(*) FROM aliquot GROUP BY 1,2` (0 disagreeing rows; totals 32 stored /
43 overall); `quantity_stored`/`unit` equal the stored-aliquot quantity sum per stock (e.g.
`v_stock_bacterial_pellet`: KR03 4/4 = 2000 ml, KR06 0/2 = NULL, `pending`).

## How it ran: two tool versions

A spec-1/spec-2 ledger whose preset copy still declares the removed `aliquot_fields` key cannot be
opened by 0.2.0+, so the old-format steps ran from the last checkout that accepts it:

| Steps | Tree | Version |
|---|---|---|
| backup, preset seed, `spec/1-to-2`, rules rewrite, `molbio 1-to-2` | wave-9-era commit `3ec57d5` | 0.1.2 |
| `spec/2-to-3`, all post-checks | 0.2.0 tree | 0.2.0 |

Every step ran `--dry-run` first (verified to write nothing), then applied on branch
`tetramark-upgrade`, through the `ServerDriver`; the server was never stopped, `main` was never
written. The ledger's own `kinds/boxes.yaml` needed no edit; `rules/legacy.yaml` was rewritten by hand
(`pellet`→`bacterial_pellet`, `bacterial`→`bacterial_glycerol`,
`entity_construct`→`entity_protein_construct`, meanings unchanged).

## Deviations recorded

- The preset copies and the rules rewrite are **filesystem** files, not Dolt content (`dolt_status`
  stays clean), so they cannot be a normal migration commit. The seed is recorded as one **empty**
  Dolt commit (step 1) and in this record; the rules rewrite is recorded here only.
- `migration/spec/2-to-3/README.md`'s verification query was corrected: Dolt 2.3.5's `LIKE` does not
  honour the backslash escape, so the extension-table probe uses `REGEXP 'aliquot_[a-z]'`.
- `tm --dry-run` is a root option (`tm --dry-run … backup`), not a subcommand option.

## Restore points

- Pre-migration Dolt backup (DB only, not re-synced): `…/Dolt/backups/zsqlab_10_gammaPFD-Fiber_2026.06.25`.
  A rollback must also restore the filesystem files: v1 presets via
  `git show 1163cb6:src/tetramark/presets/{molbio,base}.yaml`, rules per the mapping above.
- Per step: `pre-migrate-spec-1`, `pre-migrate-molbio-1`, `pre-migrate-spec-2`.
- Full redo: reset `main` to `qscm59hdnc…` and re-run the steps; the backup and the three tags stay.
