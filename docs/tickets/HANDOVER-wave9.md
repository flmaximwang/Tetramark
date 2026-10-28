# HANDOVER · Wave 9 (aliquot model), tickets 20–24

**Date**: 2026-10-28 · **Status**: design settled with the user, tickets written, **nothing implemented,
nothing committed** · **Source issue**: git-bug `2f6a0d5` "Wrong aliquot schema" (comment #2 links the
tickets)

---

## 1. What the user reported

After migrating zsqlab10, aliquots of every stock type share one core `aliquot` table and one
`aliquot_event`. molbio has no aliquot kind. The user's position: "aliquots must be stock-specific,
because different types of stocks have different properties."

## 2. What we concluded (the grilling outcome)

The model was not wrong; it was under-specified. The settled principle:

> **All aliquots of one stock are the same.** They differ only in location, quantity, unit, status and
> label. Tubes that differ in a property (concentration, freeze-thaw cycles, glycerol %) are
> **different stocks**. Type-specific properties belong on the **stock kind**.

That changes how the issue gets fixed:

| Topic | Decision |
|---|---|
| Aliquot as its own `object: aliquot` kind | **No**: stock kind → tube type is one-to-one, so a standalone kind adds machinery and buys nothing |
| `aliquot_fields` (per-stock-kind tube tables) | **Removed from Tetramark entirely**; molbio's only use (`glycerol_pct`) moves to the stock |
| `aliquot_event` | **Stays one shared table** (rationale in ticket 23 §2) |
| `v_aliquot_<kind>` views | **Not wanted** |
| `v_stock_<kind>` | Gains `entity_kind`, `entity_name`, `aliquots_stored`, `aliquots_total`, `quantity_stored`, `unit`, `status`. The user needs to see which design a stock belongs to and how many tubes remain |
| Same names for entities and stocks | **No**: entity ↔ stock is many-to-many (`bacterial_pellet` holds plasmid *or* strain). Naming rule: entity kind = what it is; stock kind = physical form |
| molbio renames | `construct→protein_construct`, `strain→bacterial_strain`, `bacterial→bacterial_glycerol`, `pellet→bacterial_pellet` |
| New molbio kinds | `cell_line` (entity, `parental` self-ref), `cell_vial`, `cell_pellet` (stocks); fields in ticket 22 |
| Presets | Leave the wheel; live in repo `presets/`; `tm init` **copies** them into `<ledger>/presets/`; bare names via `presets_dir` in workspace config; ledgers resolve **only** their own copy |
| Migrations | Repo `migration/`, keyed by **the counter that changed**: `spec/N-to-N+1/`, `presets/<name>/N-to-N+1/`. Rule: whoever bumps a version adds exactly one folder. Run by hand: `uv run migration/…/migrate.py --ledger PATH [--dry-run]` |
| Renames mechanism | `renames.yaml` fed to the existing diff planner; **no `renamed_from` in presets** |
| Hand-written rule SQL in ledgers | Not auto-rewritten; step README gives a grep, then `tm check` with no FAIL |
| `tm upgrade` (docs/issues/001) | Kept separate; it may absorb `migration/` later |
| Release | All of wave 9 ships together as **0.2.0** (one zsqlab10 migration, not two) |

Every ticket repeats its own decisions under "Decisions (settled, do not re-open)". If an agent wants to
change one, it must escalate to the user, not decide.

## 3. Dispatch plan

| # | Ticket | Depends on | Can run with |
|---|---|---|---|
| 20 | `20-presets-out-of-package.md` | — | 21 |
| 21 | `21-migration-groundwork.md` | — | 20 |
| 22 | `22-molbio-renames-cell-kinds.md` | 20, 21 | — |
| 23 | `23-generic-aliquots-stock-views.md` | 21, 22 | — |
| 24 | `24-migrate-zsqlab10.md` | 20–23 | — |

- The user wants tickets **delegated to agents**. The manager does not implement.
- House convention (`docs/tickets/README.md`): real `dolt` tests, no mocks, stay in the listed files,
  **leave the tree dirty, no commit**. Run 20 and 21 in separate worktrees, and merge both before 22.
- Review each diff before dispatching the next ticket. 22 and 23 build on 21's helper
  (`src/tetramark/migrate.py`) and its rename-map parameter in `schema/diff.py`.

## 4. Facts already verified (don't re-derive)

- Ledger counters live in `ledger_meta`: `spec_version`, `preset_versions` (JSON), `tetramark_version`.
  Tool `SPEC_VERSION = 2` (`src/tetramark/schema/model.py:58`).
- Kind names are one global namespace (`schema/loader.py:443`). Kind renames via the planner already
  rename tables, rename `aliquot_<kind>` tables and rewrite core `kind` values (`schema/diff.py:1024`, `:960`).
- Core `entity` has a `name` column, so `entity_name` in views is a plain join.
- The loader already accepts presets by path (`schema/loader.py:197`).
- **zsqlab10** (`/Users/maxim/Documents/Lab/Dolt/zsqlab_10_gammaPFD-Fiber_2026.06.25`, server
  127.0.0.1:13308 per `workspace.yaml`):
  - `spec_version 1`: **one behind the tool already** (0.1.2 made spec 2); hence `spec/1-to-2` in ticket 21.
  - `preset_versions {"base": 1, "molbio": 1}`, no local `presets/` copy.
  - entity 15 (construct 7, plasmid 7, strain 1); stock 39 (bacterial 14, protein_prep 12,
    plasmid_prep 7, pellet 6); aliquot 43; `aliquot_event` 43 (all `create`); `aliquot_bacterial` 14 rows,
    each the only aliquot of its stock (so the `glycerol_pct` move cannot conflict).
  - `rules/legacy.yaml` hard-codes old names: `s.kind = 'pellet'` ×4, `p.kind <> 'bacterial'`,
    `entity_construct` ×2.
  - Branches: `main`, `tetramark`; tags `legacy-final`, `tetramark-v0.1.0`. Migration history is in
    `/Users/maxim/Documents/Lab/Dolt/migration/zsqlab10/` (MIGRATION-RECORD.md).

## 5. Risks and gotchas

- **zsqlab10 is unusable with the new `tm` from ticket 20 until ticket 24** (no local preset copy;
  no fallback by design). Tell the user, and don't release between 20 and 24.
- Ticket 24 writes to a **real lab ledger**: branch `tetramark-upgrade` only, backup first, never
  `main`, never push. The user merges it. If a step refuses, stop and fix the script, never hand-patch
  the DB.
- `git-bug termui` holds the git-bug lock while open. If `git-bug` says "already locked", ask the user
  to close it; don't kill the process.
- Root `uv.lock` was already modified (uncommitted) before this session. It isn't ours, so leave it.

## 6. Done when

- Tickets 20–23 merged, full suite green, `grep -rn aliquot_fields src/` returns only the loader's
  rejection message.
- Ticket 24 report shows unchanged row counts, only new kind names, `spec_version 3`,
  `preset_versions.molbio 2`, `tm check` with no FAIL, and the user has merged `tetramark-upgrade`.
- 0.2.0 released; git-bug `2f6a0d5` closed with a comment pointing to the release.

## 7. Files from this session (uncommitted)

- `docs/tickets/20-…` to `24-…`, `docs/tickets/README.md` (wave 9 index), this file.
- git-bug `2f6a0d5` comment #2 (local git-bug data; not pushed).
