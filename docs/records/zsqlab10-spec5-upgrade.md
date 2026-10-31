# zsqlab10 to spec 5 / base 4 (2026-11-01)

**Ledger**: `/Users/maxim/Documents/Lab/Dolt/zsqlab_10_gammaPFD-Fiber_2026.06.25`
(Dolt 2.3.5 via the launchd `dolt sql-server` on `127.0.0.1:13308`) · **Tool**: `tm` 0.4.0,
installed from annotated tag `v0.4.0` · **Status**: done, cut over to `main`; nothing pushed.

Follows the [spec-4 upgrade](zsqlab10-spec4-upgrade.md) and closes open item 1 of the
[wave-11 delivery record](wave11-delivery.md). Ran the two steps in the order the
[4-to-5 README](../../migration/spec/4-to-5/README.md) requires, with the operator's OK.

## Result

`main` fast-forwarded from `28jls7aeh8vd5h1774lgm67pt2vjjo0o` to
`72f5d5gd4pfollkkl75f3ps6frar2lro` (`CALL DOLT_MERGE('tetramark-spec5','--ff-only')`,
`fast_forward=1`, 0 conflicts). History stays linear: 91 commits reachable from `main`, 0 with a
second parent.

| # | Commit | Message | Tag created before the write |
|---|---|---|---|
| 1 | `2vp248nfnb0ccb6ifo14l6cmmjah18vn` | `migrate(ledger): spec 4 -> 5` | `pre-migrate-spec-4` → `28jls7aeh8…` |
| 2 | `72f5d5gd4pfollkkl75f3ps6frar2lro` | `migrate(ledger): base 3 -> 4` | `pre-migrate-base-3` → `2vp248nfnb…` |

Cut-over tag: `tetramark-v0.4.0` → `72f5d5gd4p…`
(`tetramark formatted ledger, spec 5, base 4`).

| Item | Before | After |
|---|---|---|
| `ledger_meta` | spec 4, `{"bacteria":1,"base":3,"cell_culture":2,"molbio":3,"nucleic_acids":1,"proteins":1}`, 0.3.0 | **spec 5**, **base 4** (same other counters), **0.4.0** |
| box views | `v_box_grid9x9`, `v_box_box_slotless`, one per box kind | one per **box**: `v_box_wfl-10-01`, `v_box_wfl-10-02`, `v_box_wfl-4c-1`, `v_box_wfl-80-big`, `v_box_wfl_-20_1`, `v_box_wfl_4_1` (plus `v_box_slots`) |
| box table kinds | `grid96` present (empty) | `grid8x12` (rename + FK/check/kind default) |
| positions | 43 aliquots (32 in stock, 11 pending) | unchanged; the step cleared **0** positions and wrote 0 move events |
| entity / stock / aliquot / box / alias / events | 15 / 39 / 43 / 6 / 46 / 43 | unchanged |
| `tm check` | 37 tests, `core_box_views` FAIL (spec-4 ledger under a 0.4.0 tool), 3 info | **39 tests, 0 FAIL**, same 3 info (L07/L08/L09) |
| `tm schema plan` | 17 pending steps | `schema is up to date` |
| `dolt_tests` | spec-4 set | 39 generated tests from the spec-5 schema, in the step commit |

## How it ran

1. `uv tool install` of the repo at tag `v0.4.0` (the machine had 0.1.2); `tm --version` = 0.4.0.
2. Dolt backup: `backups/zsqlab_10_gammaPFD-Fiber_2026.06.25.pre-spec5-20261031`.
3. **Rehearsal on a copy**: `dolt backup restore` of that backup into `/tmp/zsq10-rehearsal/reh`
   (plus a copy of the ledger's `presets/`, `kinds/`, `rules/`, `ledger.yaml`), then both steps
   from the repo at `main`: `uo771kdtm8…` (spec 4 → 5) and `8ic2ndr4aq…` (base 3 → 4), `tm check`
   39/39, plan up to date.
4. **Dry-runs on the real ledger**: `spec/4-to-5` 53 statements, `base/3-to-4` 11 statements,
   nothing written, no refusals.
5. Applied on branch `tetramark-spec5` (created from `main`), then `tm check` / `tm schema plan`
   on the branch, then the fast-forward and the tag.

Both steps ran through the **server driver**, so the running server was never stopped and `main`
was not written until the fast-forward.

## Deviations and notes

- **Preset copies are files, not Dolt content.** The base step replaced the ledger's own
  `presets/base.yaml` (v3 → v4) and the other copies it vendors. Between the base step and the
  cut-over, opening `main` (still spec 4) reports 17 pending steps — expected; the cut-over
  resolved it. `tm check` on `main` after the fast-forward is the proof.
- The spec-4 ↔ 0.4.0 mismatch made `core_box_views` fail before the upgrade; that was the intended
  cut-over signal, not a data problem.
- `dolt_log` reachable from `main` is 91 commits (59 at the end of the 2026-10-26 renumber, plus
  the wave-10 spec-4 steps, these two, and normal ledger activity since). Still linear.
- The Dolt CLI on the host is 2.6.1 while the server is 2.3.5; the rehearsal used the embedded
  driver (2.6.1) only as a copy, the real run used the server. Results matched.

## Restore points and redo

- Pre-upgrade backup: `…/Dolt/backups/zsqlab_10_gammaPFD-Fiber_2026.06.25.pre-spec5-20261031`.
  A rollback must also restore the base-3 preset copy
  (`migration/presets/base/2-to-3/base-3.yaml` → `<ledger>/presets/base.yaml`, and the molbio v3 /
  part copies the base step vendors).
- Per step: tags `pre-migrate-spec-4` (`28jls7aeh8…`) and `pre-migrate-base-3` (`2vp248nfnb…`).
- Full rollback to spec 4: `CALL DOLT_RESET('--hard','pre-migrate-spec-4')` on `main`, then restore
  the v3 preset copies.
- The branch `tetramark-spec5` stays at `72f5d5gd4p…` as the audit trail.

## Not done

- `dolt push` to `origin` — still unreachable (10.10.74.242); needs explicit consent when it is.
- Branch cleanup (`tetramark`, `tetramark-upgrade`, `tetramark-upgrade-spec4`,
  `tetramark-spec5`) — kept as audit trails pending a decision.
