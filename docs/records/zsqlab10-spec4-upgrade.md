# zsqlab10 to spec 4 / molbio 3 (wave-10 upgrade record)

**Date**: 2026-10-29 · **Ledger**: `/Users/maxim/Documents/Lab/Dolt/zsqlab_10_gammaPFD-Fiber_2026.06.25`
(Dolt 2.3.5, launchd `dolt sql-server` on `127.0.0.1:13308`) · **Status**: done; upgraded on branch
`tetramark-upgrade-spec4` and fast-forwarded into the ledger's `main`. Dolt backup taken first; a full
rehearsal on a copy produced the identical fingerprint and counts. Nothing pushed.

This runs the wave-10 steps of [the delivery record](wave10-delivery.md) on the one real ledger, after
[the wave-9 migration](zsqlab10-migration.md) left it at spec 3 / molbio 2 / base 1.

## Result

`main` = `tetramark-upgrade-spec4` at `28jls7aeh8vd5h1774lgm67pt2vjjo0o` (fast-forward). Five commits,
one per counter bump, each with its restore tag:

| # | Commit | Message | Tag |
|---|---|---|---|
| 1 | `du7uajspac9nlrj5rq2ktb0eg3s2a83k` | `migrate(ledger): spec 3 -> 4` | `pre-migrate-spec-3` |
| 2 | `klhhrlestgc9v8p0v7uc2uup2eupbthb` | `migrate(ledger): base 1 -> 2` | `pre-migrate-base-1` |
| 3 | `90uu480qth5q7ecmckl3ui7g1stvhee8` | `migrate(ledger): base 2 -> 3` | `pre-migrate-base-2` |
| 4 | `3dpac4op217ut2lfdbl51o17tbqfpc2f` | `migrate(ledger): molbio 2 -> 3` | `pre-migrate-molbio-2` |
| 5 | `28jls7aeh8vd5h1774lgm67pt2vjjo0o` | `migrate(ledger): cell_culture 1 -> 2` | `pre-migrate-cell_culture-1` |

| Item | Before | After |
|---|---|---|
| `ledger_meta` | spec 3, `{"base":1,"molbio":2}`, 0.2.0 | **spec 4**, **`{"bacteria":1,"base":3,"cell_culture":2,"molbio":3,"nucleic_acids":1,"proteins":1}`**, 0.3.0 |
| fingerprint | `0371d4048c0f21f288fe99c52de48bf4beeff4f01f8e698b7528eb4b965dcbb8` | `9846f20b0f1dc3cf68a193b5e09a442a633ddb32b719287f865d2fb38c4e5366` |
| entity (15) | strain 1, plasmid 7, construct 7 | `bacterial_strain` 1, `plasmid` 7, `protein_construct` 7 |
| stock (39) | bacterial 14, pellet 6, plasmid_prep 7, protein_prep 12 | `bacterial_glycerol` 14, `bacterial_pellet` 6, `plasmid_prep` 7, `protein_prep` 12 |
| aliquot / events / alias / box | 43 (32 stored, 11 pending) / 43 / 46 / 6 | unchanged |
| tables / views | 27 / 22 | **31 / 26** (adds `entity_oligo`, `stock_oligo_stock`, `box_incubator`, `stock_cell_biosample`) |
| `ledger.yaml` | had `statuses: [pending, stored, missing, consumed]` | key removed (file and `ledger_meta.ledger_yaml`) |
| stock views | `aliquots_stored`, `quantity_stored`, five-value `status` | `aliquots_in_stock`, `aliquots_total`, `quantity_in_stock` + `unit`, `status` ∈ active/depleted/empty; old column names absent (`information_schema.columns` count 0) |
| `aliquot_event` | no `forced` | `forced tinyint(1) NOT NULL DEFAULT 0`; the global `aliquot.status` CHECK is gone |
| `tm check` | 32 tests, exit 0, 3 info gaps | **36 tests, exit 0, same 3 info gaps** (L07/L08/L09) |
| `tm schema plan` | — | `schema is up to date` |

The ledger's own `kinds/boxes.yaml` needed no edit (it only adds a box kind); `rules/legacy.yaml` was
already status-compatible (L01 compares `status = 'stored'`, which is still a `tube` status) and
references none of the removed view columns. Preset copies under `<ledger>/presets/` are now the
current wave-10 files (base v3, molbio v3, the four parts, cell_culture v2).

## How it ran: two trees

The wave-10 step chain assumes the preset copies match the counter being bumped, and each preset
step *vendors the repository's current copies before opening the ledger*. Two consequences had to be
handled:

1. **`spec/3-to-4` first, with the right copies.** The spec-4 loader refuses a stock kind without
   `statuses:`, so the pre-upgrade copies cannot be opened at all. The frozen v2 copies shipped with
   the historical steps were placed by hand first (as the step's README requires):
   `base/1-to-2/base-2.yaml` → `presets/base.yaml`, `molbio/1-to-2/molbio-2.yaml` → `presets/molbio.yaml`.
2. **`base 1 -> 2` ran from the tree of its own era.** That step is content-only: its plan only
   recreates views, assuming the base-v2/molbio-v2 model. Run from the 0.3.0 tree it would vendor
   molbio v3 and try to create views over tables (`entity_oligo`, `stock_cell_biosample`, …) its plan
   does not create — the first rehearsal failed exactly there. It was run from the wave-10-ticket-25
   tree (`6faa557`, base v2 / molbio v2, tool 0.2.0), which is the state it was tested against.

| Steps | Tree | Version |
|---|---|---|
| backup, rehearsals, `spec/3-to-4`, `base/2-to-3`, `molbio/2-to-3`, `cell_culture/1-to-2` | `main` at `73ccc24` (+ docs commit `f4a2ce5`) | 0.3.0 |
| `base/1-to-2` | `6faa557` (wave-10 ticket 25) | 0.2.0 |

Every step ran `--dry-run` first on the real ledger after a full rehearsal on a copy; all work happened
on branch `tetramark-upgrade-spec4` through the `ServerDriver` (server never stopped, `main` never
written until the fast-forward). The rehearsal and the real run produced the same fingerprint
(`9846f20b…`), the same counters, the same counts and the same `tm check` result.

## Deviations recorded

- **Preset copies are filesystem files, not Dolt content**, so placing them is not a migration commit
  (it is a documented precondition of `spec/3-to-4` and part of this record).
- **`base/1-to-2` from the earlier tree** (section above) — a real limitation of running a content-only
  step after the repository moved on; noted for future pre-0.3.0 base counters.
- `tetramark_version` ends at `0.3.0` (the last step writes it); the earlier `base/1-to-2` commit
  temporarily recorded `0.2.0`, as its tree did.
- The 0.2.0 tool can no longer open this ledger (spec-4 preset copies + no `statuses:` key); that is
  the intended cut-over. Use 0.3.0+ from here.
- The three informational rules (L07 protein constructs without a stock, L08/L09 preps without a
  concentration) report the same counts as before the upgrade; they never fail `tm check`.

## Restore points and redo

- Pre-upgrade Dolt backup (DB only):
  `…/Dolt/backups/zsqlab_10_gammaPFD-Fiber_2026.06.25.pre-spec4-20261029` (721 KB, synced 2026-10-29 11:34).
  A rollback must also restore the older filesystem preset copies (they are not Dolt content).
- Per step: the five `pre-migrate-*` tags listed above (`pre-migrate-spec-3` is the pre-upgrade tip
  `o7734qkqrt40gnfqlrger5vdtu8spg4r`).
- Full redo: reset `main` to `o7734qkqrt40gnfqlrger5vdtu8spg4r`, restore the v2 preset copies, then run
  the five steps in the order above.
- The wave-9 branch `tetramark-upgrade` and the older `tetramark` branch are untouched; the new
  `tetramark-upgrade-spec4` branch stays as the audit trail.

## Verification (commands run on `main` after the fast-forward)

```sh
dolt sql -q "SELECT \`key\`, \`value\` FROM \`ledger_meta\` WHERE \`key\` IN ('spec_version','preset_versions')"
tm -w /Users/maxim/Documents/Lab/Dolt -l zsqlab_10_gammaPFD-Fiber_2026.06.25 check          # OK: 36 passed, 3 info
tm -w /Users/maxim/Documents/Lab/Dolt -l zsqlab_10_gammaPFD-Fiber_2026.06.25 schema plan    # schema is up to date
dolt status                                                                                 # clean
```
