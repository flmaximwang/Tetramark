# Tickets

Tickets 01–14 are the original implementation wave; ticket texts predate the Tetramark rename
(package `tetramark`, CLI `tm`).

Read `docs/spec.md` first; section numbers below refer to it.

Tickets 15–19 are the **test-harness v2 wave** (server-backed pytest fixtures, **done**); their design is in
`docs/test-server-harness.md`, not in `docs/spec.md`. They are tracked here to keep one ticket index.

| # | Ticket | Depends on | Wave |
|---|---|---|---|
| 15 | Server harness infra (`init_ledger_on_server` + fixtures) | — | 8 |
| 16 | Migrate batch A (objects/csv/cli_objects/read_side) | 15 | 8 |
| 17 | Migrate batch B (validation/ledger_init/docs) | 15 | 8 |
| 18 | Migrate schema_diff | 15 | 8 |
| 19 | Acceptance, markers, docs | 15–18 | 8 |

Wave 8 result: full suite 758.27 s → 154.83 s (`--dist load`: 141.50 s), 614 passed; see
`docs/test-server-harness.md` §9b.

Tickets 20–24 are the **aliquot-model wave** (from git-bug issue `2f6a0d5`, "Wrong aliquot schema"):
aliquots become generic tubes, type-specific properties live on stocks, presets leave the package, and
upgrade steps live in `migration/`, keyed by the version counter they bump. Each ticket lists its
settled decisions; do not re-open them.

| # | Ticket | Depends on | Wave |
|---|---|---|---|
| 20 | Presets live in `presets/`, copied into each ledger on init | — | 9 |
| 21 | Migration groundwork: `migration/` layout, renames hook, `spec/1-to-2` | — | 9 |
| 22 | molbio v2: kind renames, cell kinds, `glycerol_pct` on the stock | 20, 21 | 9 |
| 23 | Generic aliquots: remove `aliquot_fields`, aliquot counts in `v_stock_<kind>` (spec 3) | 21, 22 | 9 |
| 24 | Migrate zsqlab10 to spec 3 / molbio 2 | 20–23 | 9 |

20 and 21 can run in parallel; 22–24 are sequential.

Tickets 25–29 are the **preset-parts wave** (from git-bug issue `0a9acec`, "BioManager Presets"). It
covers status sets per stock kind with transition rules, `tm lineage`, presets split into shared parts,
and the new `cellbio` and `angenet` bundles. The design is in `docs/preset-parts.md`. It starts after
wave 9.

| # | Ticket | Depends on | Wave |
|---|---|---|---|
| 25 | Status sets per stock kind, transition rules, in-stock views (spec 4) | 23 | 10 |
| 26 | `lineage: true` on ref fields and `tm lineage` | 23 | 10 |
| 27 | Split molbio into parts (molbio 2 → 3) | 22, 25 | 10 |
| 28 | `cellbio` bundle: `immuno`, `viral_vectors`, `biosamples` | 26, 27 | 10 |
| 29 | `angenet` bundle: drosophila, c_elegans, zebrafish, mouse | 25–28 | 10 |

25 and 26 can run in parallel; 27–29 are sequential.

Each ticket: implement, add pytest tests (real `dolt`, no mocks), stay inside the listed files where
possible, leave the working tree dirty for review (no commit). Tickets in the same wave are
independent and may run in parallel (separate git worktrees/copies).
