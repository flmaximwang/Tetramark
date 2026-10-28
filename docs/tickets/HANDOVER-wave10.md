# HANDOVER · Wave 10 (preset parts, statuses, lineage), tickets 25–29

**Date**: 2026-10-28 · **Status**: design settled with the user, spec and tickets written, **nothing
implemented** · **Source issue**: git-bug `0a9acec` "BioManager Presets" (the comment links the tickets) ·
**Design**: `docs/preset-parts.md` (source of truth) · **Prerequisite**: wave 9 (tickets 20–24) is
merged — see `docs/tickets/README.md` and `24-report.md`.

---

## 1. What the user asked

Presets for other lab types, based on the data model of
[BioManager](https://github.com/gaspolymerase/biomanager) (Flask, MIT): its organism modules (fly, worm,
fish, mouse, plants) and its inventory lists (cell lines, antibodies, viruses, primers, samples).

## 2. What was decided (details in the design doc)

| Topic | Decision |
|---|---|
| What to borrow | BioManager's kinds and fields only, not its mechanisms (JSON attrs, schedules) |
| Out of scope | Time-based tracking (weights, schedules, due-lists, health, genotype-call history), which may become **plugins** later; plants and field crops ("not familiar"); BioManager's "custom" organism |
| Data | Prototype only, no real data soon; every new preset at `version: 1` |
| Structure | Flat **parts** (`base`, `nucleic_acids`, `bacteria`, `proteins`, `cell_culture`, `immuno`, `viral_vectors`, `biosamples`, `drosophila`, `c_elegans`, `zebrafish`, `mouse`) + include-only **bundles** `molbio`, `cellbio`, `angenet` ("animal genetics", not "genetics") |
| Shared cells | Cell kinds are **shared** by molbio and cellbio through the `cell_culture` part (the user wanted them in both, not moved) |
| Organisms | Colony = stock, individual = aliquot; individuals of one stock are treated as identical (repeatable experiments). Individually counted organisms: aliquot = one animal in a `cage`. Group-counted: aliquot = one vial, plate or tank, with quantity = headcount |
| Mouse frozen stocks | Two kinds: `mouse_sperm_cryo` and `mouse_embryo_cryo` |
| Crosses | Progeny stock with `dam` / `sire` refs to aliquots |
| Litters | A stock without genotype, then split into new stocks at genotyping (`parent_stock_id`), with no core change |
| Statuses | **Not per ledger.** Each stock kind names a `status_set`; each status has `in_stock` and `to:` (transition rules, which the user explicitly wanted). Creation accepts any status; changes follow rules; `--force` + required note + `aliquot_event.forced` |
| Stock status | "The number of aliquots is the real status of a stock": views show `aliquots_in_stock / aliquots_total` + `active / depleted / empty` |
| Lineage | New `tm lineage` (the user first said "phylogenic", which is the wrong term), driven by core links + ref fields flagged `lineage: true` |

The user OK'd one change only after the fact, so verify it with them if it matters: the `animal`
status set lives in **`base`** instead of the organism parts. Set names are global, and both
`zebrafish` and `mouse` need it.

## 3. Dispatch plan

| # | Ticket | Depends on | Parallel with |
|---|---|---|---|
| 25 | `25-status-sets-transitions.md` (spec 4) | 23 | 26 |
| 26 | `26-tm-lineage.md` | 23 | 25 |
| 27 | `27-molbio-into-parts.md` (molbio 2→3) | 22, 25 | — |
| 28 | `28-cellbio-bundle.md` | 26, 27 | — |
| 29 | `29-angenet-bundle.md` | 25–28 | — |

- Delegate; the manager doesn't implement. Same conventions as wave 9: real `dolt`, no mocks, leave the
  tree dirty, no commit, review each diff before dispatching the next.
- **Wait for wave 9 to be merged** before starting 25 or 26. They change views and migrations created
  in 21–23.

## 4. Open choices delegated to tickets (agents must report what they chose)

- **27**: which part includes which for `plasmid.construct → protein_construct`, and where
  `sequence_fields` lives.
- **28**: how `biosample` handles entity kinds that aren't loaded. Preferred: split into `cell_biosample`
  / `animal_biosample` with no core change. Alternative: a minimal loader change.
- **26**: `lineage: true` changes no stored shape, so it gets no migration step; the README of `migration/`
  must say so.

## 5. Facts already verified

- The loader loads each include once (`schema/loader.py`, `_expand`), so diamond includes are safe; kind
  names are global (`loader.py:443`).
- Today `v_stock_status` hard-codes `stored` / `pending` / `missing` / `consumed` (`schema/ddl.py:811`),
  and the ledger vocabulary comes from `ledger.yaml statuses:`.
- BioManager's organism definitions (identity mode, nouns, statuses, line fields) are summarised in the
  design doc §3. The source is `app/organisms.py` `PRESETS`; inventory fields come from
  `app/inventory.py`.

## 6. Done when

- 25–29 merged, full suite green, `tm init` works for `molbio`, `cellbio`, `angenet` and
  `[molbio, cellbio]`, each passing `tm check`.
- A follow-up like ticket 24 (zsqlab10: `spec/3-to-4`, then `molbio 2-to-3`) is proposed to the user and
  run only after they agree.
- git-bug `0a9acec` is closed with a pointer to the release.

## 7. Files from this session (uncommitted)

`docs/preset-parts.md`, `docs/tickets/25-…` to `29-…`, `docs/tickets/README.md` (wave 10 index), this file.
