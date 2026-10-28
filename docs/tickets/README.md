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

Each ticket: implement, add pytest tests (real `dolt`, no mocks), stay inside the listed files where
possible, leave the working tree dirty for review (no commit). Tickets in the same wave are
independent and may run in parallel (separate git worktrees/copies).
