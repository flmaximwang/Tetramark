# 24 report · zsqlab10 migrated to spec 3 / molbio 2

**Date**: 2026-10-28 · **Ledger**: `/Users/maxim/Documents/Lab/Dolt/zsqlab_10_gammaPFD-Fiber_2026.06.25`
(Dolt 2.3.5, served by the launchd `dolt sql-server` on `127.0.0.1:13308`) · **Status**: migrated on
branch **`tetramark-upgrade`**, pre-migration backup taken and restore-proven, `main` and `tetramark`
untouched, nothing pushed. **Only remaining act: the user fast-forwards `main` to
`tetramark-upgrade`** (§10).

This is the first real run of `migration/`. The procedure is in
[`24-migrate-zsqlab10.md`](24-migrate-zsqlab10.md); the rehearsal was done on a restored copy of the
actual backup before any write to the real ledger (both runs produced the same final fingerprint and
counts — §8.4).

---

## 1. Outcome in one line

`tetramark-upgrade` = `main` (`qscm59hdnc…`) + `chore(ledger): vendor preset copies` + the three
migration commits; `ledger_meta` is now `spec_version 3`, `preset_versions {"base": 1, "molbio": 2}`,
`tetramark_version 0.2.0`; every row is preserved (15 entities / 39 stocks / 43 aliquots / 43
`aliquot_event` / 46 aliases / 6 boxes / 162 slots), only the molbio names changed, no
`aliquot_<kind>` table remains, `glycerol_pct = 15.00` on all 14 bacterial-glycerol stocks,
`v_stock_*` cross-checks clean, `tm check` exit 0 with only the three pre-existing info-level gaps.

## 2. Tool versions and why two checkouts

| Steps | Checkout | Tetramark | Reason |
|---|---|---|---|
| backup, preset seeding, `tetramark-upgrade`, `spec/1-to-2`, rules rewrite, `molbio 1-to-2` | `.dsh/gitworktree/w24-legacy` @ `3ec57d5` | **0.1.2** | a spec-1/spec-2 ledger whose preset still declares the removed `aliquot_fields` key cannot be opened by 0.2.0+; `3ec57d5` is the last tree that accepts it (wave 9 through ticket 22) |
| `spec/2-to-3`, all post-migration verification, `tm check` | `.dsh/gitworktree/w24` @ `bcde6fb` | **0.2.0** | spec 3 / molbio 2 tree (tickets 23 + release) |

Both worktrees are git worktrees of this repo and share the real ledger's Dolt repository through the
server; edits under `<ledger>/{presets,rules}` are **filesystem** files and are not branch-scoped.

Every step opened the real ledger through the **`ServerDriver`** (pgrep finds the launchd
`dolt sql-server`; the server was never stopped). The rehearsal ran **embedded** on the restored copy.

## 3. Backup (before any write) and restore proof

Done first, through the running server, so nothing was touched before the backup existed:

```sql
CALL DOLT_BACKUP('add',  'zsqlab_10_gammaPFD-Fiber_2026_06_25',
                 'file:///Users/maxim/Documents/Lab/Dolt/backups/zsqlab_10_gammaPFD-Fiber_2026.06.25');
CALL DOLT_BACKUP('sync', 'zsqlab_10_gammaPFD-Fiber_2026_06_25');
```

- `dolt_backups` lists it; `tm backup ls` shows it; `tm --dry-run … backup` prints
  `CALL DOLT_BACKUP('sync', …)`; the real `tm … backup` sync succeeded again (idempotent).
- Backup dir: 652 KB, `manifest`/`LOCK`/`*.darc`/`oldgen` present. **Not deleted, not re-synced after
  the migration** (it stays the pre-migration restore point).
- Restore verified **twice** into scratch data dirs:
  `dolt --data-dir <scratch> backup restore file:///Users/maxim/Documents/Lab/Dolt/backups/zsqlab_10_gammaPFD-Fiber_2026.06.25 zsqlab_10_gammaPFD-Fiber_2026.06.25`
  → `main @ qscm59hdnc9kg33m7o11embp1ph44271`, branches `{main, tetramark}`, tags
  `{legacy-final, tetramark-v0.1.0}`, `spec_version 1`, `preset_versions {"base":1,"molbio":1}`,
  15/39/43/43/46/6/162, `aliquot_bacterial` 14 rows.
- A restored Dolt backup carries **no filesystem files**: the scratch ledger needed `ledger.yaml`,
  `kinds/`, `rules/` and `presets/` copied from the real ledger (the rehearsal used the v1 preset
  copies, §5 step 2). A rollback therefore has to restore database **and** files, not just the backup.

## 4. Pre-state (read live, 2026-10-28 ~14:56)

| Item | Value |
|---|---|
| `ledger_meta` | `spec_version 1`, `preset_versions {"base":1,"molbio":1}`, `tetramark_version 0.1.1`, `created_with 0.1.0`, `adopted_from_commit vkeie5pn…` |
| branch / HEAD | `main` @ `qscm59hdnc9kg33m7o11embp1ph44271` |
| branches / tags | `main`, `tetramark` @ `6ep54qs1u7dirmkv3oa031vr0b0bseo9`; `legacy-final` @ `g88gc1i…`, `tetramark-v0.1.0` @ `6ep54qs…` |
| entity 15 | `construct` 7, `plasmid` 7, `strain` 1 |
| stock 39 | `bacterial` 14, `pellet` 6, `plasmid_prep` 7, `protein_prep` 12 |
| aliquot 43 | stored 32, pending 11; `aliquot_event` 43 (all `create`) |
| alias 46 · box 6 (`box_slotless` 4, `grid9x9` 2) · slot 162 | |
| tables / views | 25 / 19 |
| `aliquot_bacterial` | 14 rows, every one `glycerol_pct = 15.00`; every bacterial stock has exactly one |
| ledger files | `kinds/boxes.yaml` (`box_slotless` only, unaffected by the renames) and `rules/legacy.yaml` with old molbio names: L01–L04 `s.kind = 'pellet'`, L02 `p.kind <> 'bacterial'`, L05/L07 `entity_construct` |
| no local `presets/` dir | ticket 20 requires one; `tm` from 0.2.0 cannot open this ledger until it exists |

`dolt_status` was clean; `tm check` (0.1.2, read-only) was exit 0: 29 tests, 3 info-level failures
(L07 = 2, L08 = 12, L09 = 3 — the documented accepted migration gaps).

## 5. The run, step by step (real ledger)

All migration steps were run dry-run first (every dry run wrote nothing: HEAD, tags, views and preset
files were checked unchanged afterwards), then applied. Steps 1–2 ran from `w24-legacy` (0.1.2),
step 3 from `w24` (0.2.0), each with `--branch tetramark-upgrade`.

```sh
LEDGER=/Users/maxim/Documents/Lab/Dolt/zsqlab_10_gammaPFD-Fiber_2026.06.25
WS=/Users/maxim/Documents/Lab/Dolt

# 0) branch from the current main HEAD; main is never written or checked out
uv run tm -w $WS -l zsqlab_10_gammaPFD-Fiber_2026.06.25 branch create tetramark-upgrade

# 1) seed the ledger's own v1 preset copies (filesystem; see §6)
git show 1163cb6:src/tetramark/presets/molbio.yaml > $LEDGER/presets/molbio.yaml
git show 1163cb6:src/tetramark/presets/base.yaml   > $LEDGER/presets/base.yaml

# 2) 0.1.2 checkout
uv run migration/spec/1-to-2/migrate.py --ledger $LEDGER --branch tetramark-upgrade --dry-run
uv run migration/spec/1-to-2/migrate.py --ledger $LEDGER --branch tetramark-upgrade
#    rules/legacy.yaml rewritten by hand (filesystem, not Dolt content — §6)
uv run migration/presets/molbio/1-to-2/migrate.py --ledger $LEDGER --branch tetramark-upgrade --dry-run
uv run migration/presets/molbio/1-to-2/migrate.py --ledger $LEDGER --branch tetramark-upgrade

# 3) 0.2.0 checkout
uv run migration/spec/2-to-3/migrate.py --ledger $LEDGER --branch tetramark-upgrade --dry-run
uv run migration/spec/2-to-3/migrate.py --ledger $LEDGER --branch tetramark-upgrade
```

| # | Commit on `tetramark-upgrade` | Message | Tag before it |
|---|---|---|---|
| — | `qscm59hdnc9kg33m7o11embp1ph44271` | (main HEAD, unchanged) | |
| 1 | `jrb0qrqpqs27q9c7epb78gn2c6lmmjrr` | `chore(ledger): vendor preset copies` | (no tag) |
| 2 | `cqjimtipot1o0hsq16brom9vvpp46epu` | `migrate(ledger): spec 1 -> 2` | `pre-migrate-spec-1` → `jrb0qrq…` |
| 3 | `msrp09eo8e6c87c50mrfkpgoj1n4kug8` | `migrate(ledger): molbio 1 -> 2` | `pre-migrate-molbio-1` → `cqjimti…` |
| 4 | `o7734qkqrt40gnfqlrger5vdtu8spg4r` | `migrate(ledger): spec 2 -> 3` | `pre-migrate-spec-2` → `msrp09e…` |

Branch/tag state after: `tetramark-upgrade @ o7734qk…`, `main @ qscm59hdnc…` (unchanged),
`tetramark @ 6ep54qs…` (unchanged); tags `legacy-final`, `tetramark-v0.1.0` plus the three
`pre-migrate-*` restore points. Nothing was pushed; no remote exists/was used.

Notes:
- **Step 1 commit is empty on purpose.** Ticket 24 step 2 says to commit the vendor of the preset
  copies as `chore(ledger): vendor preset copies`, but preset files are not Dolt content, so
  `dolt_status` stays clean after the copy. It was recorded as one **empty** Dolt commit on the
  branch (`CALL DOLT_COMMIT('-m', msg, '--allow-empty')`, Dolt 2.3.5 accepts this) so the audit trail
  has the documented commit and `pre-migrate-spec-1` has a stable target. Deviation from "the only
  Dolt commits are the migration commits": there are **four** Dolt commits, one of which contains no
  data change.
- **The rules rewrite is not a commit at all** — `rules/legacy.yaml` is a filesystem file; it is
  recorded here, not in Dolt history. (Same for the preset copies themselves.)
- Molbio splice order (visible in the dry run and applied in that order):
  `ALTER TABLE stock_bacterial_glycerol ADD COLUMN glycerol_pct DECIMAL(5,2) DEFAULT 15` →
  `UPDATE … JOIN aliquot … JOIN aliquot_bacterial … SET s.glycerol_pct = e.glycerol_pct` →
  `DROP TABLE IF EXISTS aliquot_bacterial`.
- The molbio step's `after_commit` hook replaced `<ledger>/presets/molbio.yaml` with the v2 copy and
  skipped `base.yaml` (already byte-identical: blob `ba679877…` = `git show 1163cb6:…/base.yaml`), then
  the runner reopened the ledger from its own copy and verified the fingerprint before reporting
  success.

## 6. Filesystem acts (not Dolt content)

| File | Before | After | How |
|---|---|---|---|
| `<ledger>/presets/molbio.yaml` | absent | **v2** (`preset: molbio`, `version: 2`, 11202 B) | seeded v1 first (§5 step 1: blob `6c25d42…`), then replaced by the molbio step's post-commit hook |
| `<ledger>/presets/base.yaml` | absent | **v1**, unchanged (3123 B, blob `ba679877…`) | seeded v1; the hook skipped it |
| `<ledger>/rules/legacy.yaml` | original with v1 names | rewritten, names only | hand edit; `diff` = 26 changed lines, all old→new names |
| `<ledger>/kinds/boxes.yaml` | `box_slotless` only | **unchanged** | — |
| `<ledger>/ledger.yaml` | — | **unchanged** | — |

Rewrite mapping (each rule keeps its meaning; only names changed):

| Rule | old | new |
|---|---|---|
| L01–L04 `s.kind` | `pellet` | `bacterial_pellet` |
| L02 `p.kind` | `bacterial` | `bacterial_glycerol` |
| L05, L07 table | `entity_construct` | `entity_protein_construct` |

The step README greps are all clean on the ledger (`kinds/` + `rules/`): old molbio names,
`aliquot_*`/`aliquot_fields`, `alias_list`/`occupant_alias_list`, hand-written `v_stock_*` SQL.
The pre-migration `kinds/`, `rules/` and `ledger.yaml` were also snapshotted to
`/tmp/zsqlab10/pre-backup-files/` (scratch only) before the edit.

## 7. Post-state (verified on `tetramark-upgrade`, 0.2.0)

| Item | Before | After |
|---|---|---|
| entity | 15 | **15** — `protein_construct` 7, `plasmid` 7, `bacterial_strain` 1 |
| stock | 39 | **39** — `bacterial_glycerol` 14, `bacterial_pellet` 6, `plasmid_prep` 7, `protein_prep` 12 |
| aliquot | 43 (stored 32, pending 11) | **43** (stored 32, pending 11) |
| `aliquot_event` | 43 (all `create`) | **43** (all `create`) |
| alias · box · slot | 46 · 6 · 162 | **46 · 6 · 162** |
| `aliquot_<kind>` extension tables | `aliquot_bacterial` 14 rows | **none** (only core `aliquot`, `aliquot_event`) |
| `stock_bacterial_glycerol.glycerol_pct` | (was `aliquot_bacterial.glycerol_pct`) | **15.00 on all 14**, min = max = 15.00 |
| base tables / generated views | 25 / 19 | **27 / 22** (25 − `aliquot_bacterial` + `cell_line`/`cell_vial`/`cell_pellet`; +3 views) |
| `ledger_meta` | spec 1, `{base:1, molbio:1}`, 0.1.1 | **spec 3**, **`{"base": 1, "molbio": 2}`**, **0.2.0** |
| fingerprint | `b3c07281…` (spec-1 tool) | **`0371d4048c0f21f288fe99c52de48bf4beeff4f01f8e698b7528eb4b965dcbb8`** |
| `dolt_status` | clean | **clean** (no uncommitted working set on the branch; `main` clean too) |
| `tm schema show -b tetramark-upgrade` | — | no fingerprint warning |
| `tm schema plan -b tetramark-upgrade` | — | `schema is up to date` |

`v_stock_bacterial_pellet` (the ticket's explicit cross-check) — view vs
`SELECT stock_id, status, COUNT(*) FROM aliquot GROUP BY 1,2`:

| stock | entity_kind | entity_name | aliquots_stored | aliquots_total | quantity_stored | unit | status | aliquot rows (status ×n) |
|---|---|---|---|---|---|---|---|---|
| 1026KR02 | plasmid | pZET28.102a_γPFD_HisProbe-1.3 | 1 | 1 | 500 | ml | active | stored ×1 |
| 1026KR03 | plasmid | pZET043-gammaPFD_wt | 4 | 4 | 2000 | ml | active | stored ×4 |
| 1026KR04 | plasmid | pZET28.102a_γPFD_HisProbe-1.1 | 1 | 1 | 500 | ml | active | stored ×1 |
| 1026KR05 | plasmid | pZET28.102a_γPFD_HisProbe-1.4 | 1 | 1 | 500 | ml | active | stored ×1 |
| 1026KR06 | plasmid | pZET28.102a_gammaPFD.CIKCH.99-103 | 0 | 2 | NULL | NULL | pending | pending ×2 |
| 1026KR07 | plasmid | pZET28.102a_γPFD_HisProbe-1.2 | 1 | 1 | 500 | ml | active | stored ×1 |

`quantity_stored`/`unit` also equal `SUM(quantity)` over the stock's stored aliquots (`ml`);
`v_stock_status`'s derived status matches. Wider check: for **every** `v_stock_<kind>`
(`bacterial_glycerol` 14, `bacterial_pellet` 6, `plasmid_prep` 7, `protein_prep` 12, `lot` 0,
`cell_vial` 0, `cell_pellet` 0 = 39) there are **0** rows where `aliquots_stored`/`aliquots_total`
disagree with the `aliquot` table; summed across kinds = 32 stored / 43 total = base-table truth.

`tm check -b tetramark-upgrade` (0.2.0): **exit 0**, `OK: 32 test(s) passed, 3 info failure(s)`. The
three info failures are the pre-existing accepted gaps, identical to the pre-migration baseline:
`rule_L07` 2, `rule_L08` 12, `rule_L09` 3. No fail-severity FAIL. `dolt_tests` = 32 and **none** names
an `aliquot_<kind>` extension table or an old v1 kind table.

## 8. Deviations, friction, rehearsal vs real

1. **`tm backup --dry-run` (option after the subcommand) is rejected** by the 0.1.2 tree:
   `--dry-run` is a root option, so it is `tm --dry-run -w … -l … backup`. Used that form; the real
   `tm … backup` then printed `backed up …`.
2. **Dolt 2.3.5's `LIKE` does not honour the backslash escape** (it reads `\_` as a
   single-character wildcard), so `test_query LIKE '%aliquot\_%'` matches every query naming the core
   `aliquot` table (the rehearsal reported 8, the parallel reviewer 7, not 0). The selective form is
   `test_query REGEXP 'aliquot_[a-z]' AND NOT test_query REGEXP 'aliquot_event'` → 0 here.
   Fixed in `w24` (uncommitted, for review with this report):
   - `migration/spec/2-to-3/README.md` verification query switched to the REGEXP form, with one
     sentence noting the LIKE caveat;
   - `migration/spec/2-to-3/migrate.py` `_EXTENSION_LIKE` comment corrected: the code is correct only
     because `CORE_ALIQUOT_TABLES` excludes `aliquot`/`aliquot_event` explicitly, not because of the
     escape.
3. **The "vendor preset copies" commit is empty and the rules rewrite has no commit** — see §5 notes.
   Ticket 24's verify bullet ("commit it as `chore(ledger): vendor preset copies`") assumed a Dolt
   commit for a filesystem act; the run records it as an empty Dolt commit plus this report.
4. **Rehearsal vs real run.** The rehearsal restored the real backup into
   `/tmp/zsqlab10-rehearsal/data/<ledger>`, used the same v1 preset seed and renames, and ran on a
   scratch `tetramark-upgrade` branch with the **embedded** driver (dry-run → apply for each step).
   The real run used the **ServerDriver** against the launchd server. Both produced the same final
   fingerprint `0371d404…`, the same counts/kinds and the same `tm check` result — the chain is
   deterministic. Commit hashes differ (different repositories): rehearsal
   `1778om4s… / 558s8kh… / c5je821… / v5lfs4n…`, real `jrb0qrq… / cqjimti… / msrp09e… / o7734qk…`.
   The rehearsal's scratch `main` stayed at `qscm59…` after all steps, which is the evidence that the
   real `--branch` writes cannot move `main`.
5. **Expected fingerprint warnings before spec 3**: every pre-step open of the spec-1/2 ledger warns
   that the live fingerprint (computed with `spec_version` 1 or 2 in the fingerprint input) differs
   from the stored one. `spec/1-to-2` records the spec-2 fingerprint, `molbio 1-to-2` the molbio-2
   one, and `spec/2-to-3` the spec-3 one; after that `tm schema show -b tetramark-upgrade` is silent.
   Opening `main` still warns (see §10).
6. **No migration-script fix was needed.** `w24-legacy` is unchanged (`M uv.lock` pre-existed);
   `w24` carries only the two documentation/comment fixes above plus this report.
7. Reviewer confirmation folded in: `git show 1163cb6:src/tetramark/presets/base.yaml` == current
   `presets/base.yaml` (blob `ba679877…`), so the seeded base copy is faithful and `vendor_presets`
   correctly skipped it (the post-commit note only mentioned `molbio.yaml`).

## 9. Restore points / rollback

- Per step: `pre-migrate-spec-1` → `jrb0qrq…` (before any spec write), `pre-migrate-molbio-1` →
  `cqjimti…`, `pre-migrate-spec-2` → `msrp09e…`.
- Full redo without touching `main`: recreate/move `tetramark-upgrade` back to `main`'s head
  `qscm59hdnc9kg33m7o11embp1ph44271` and re-run §5. The pre-migration backup stays as it was.
- Any rollback must also restore the **filesystem** files: v1 presets are recoverable with
  `git show 1163cb6:src/tetramark/presets/{molbio,base}.yaml`; the original `rules/legacy.yaml` is
  the rewritten file with the §6 mapping reversed (a scratch copy is at
  `/tmp/zsqlab10/pre-backup-files/`). `dolt reset` alone does not restore files.

## 10. What the user must do (and the one warning)

1. **Merge promptly** — the on-disk `presets/` and `rules/` already describe the migrated shape, while
   `main` is still spec 1. Fast-forward it with Dolt's own merge (verified client form; the merge
   itself was **not** run by this ticket):

   ```sh
   dolt --host 127.0.0.1 --port 13308 --user root --password '' --no-tls sql -q \
     "USE \`zsqlab_10_gammaPFD-Fiber_2026.06.25\`; CALL DOLT_MERGE('tetramark-upgrade', '--ff-only');"
   ```

   (`tetramark-upgrade` is a direct descendant of `main`, so this is a clean fast-forward; if Dolt's
   stored procedure rejects the `--ff-only` argument, `CALL DOLT_MERGE('tetramark-upgrade')` is the
   same fast-forward. Do not force/`--no-ff`; do not push.)
2. **Until then, use `-b tetramark-upgrade` for everything** that opens this ledger
   (`tm … -b tetramark-upgrade …`): without it, `tm` loads the migrated presets against the old `main`
   database and warns about a fingerprint mismatch; reads that only touch the core tables still work,
   but anything resolving the renamed kinds/`aliquot_bacterial` will fail. `tm check` without `-b`
   runs `main`'s stored (old) tests.
3. After the merge: `tm check` on `main` should be exit 0 with the same three info gaps, and the
   fingerprint warning disappears. The branch `tetramark-upgrade` may then be deleted (optional; the
   three `pre-migrate-*` tags stay as history). Keep the pre-migration backup from §3.

**Residual risk**: the v1 preset copies were seeded into `<ledger>/presets/` before the migration
(needed so 0.1.2 could open the ledger); the migration then replaced `molbio.yaml` with v2. The
backup is a database backup only — if the user ever resets the database to the backup, the v1 preset
files and the original rules must be restored too (commands in §9). `main` was never written, nothing
was pushed, and the backup was not deleted or overwritten.

## 11. Where the evidence is

Raw logs from both runs (scratch, ephemeral): `/tmp/zsqlab10/rehearsal-logs/` and
`/tmp/zsqlab10/real-logs/` (dry-run/apply output for each step, verification queries, `tm check`
before/after, schema show/plan). Key outputs are quoted above. Commands in this report are exactly
what was run, apart from `uv run` → the worktrees' `.venv/bin/python` (same interpreter, no lockfile
change).
