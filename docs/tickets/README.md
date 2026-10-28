# Tickets

This index lists **open work only**. Everything delivered through **0.2.0** (waves 1–9, tickets 01–24)
is no longer kept as tickets: its behaviour is the [specification](../spec/README.md), its migration
procedure is [the migration guide](../guides/migrating-a-legacy-ledger.md) plus
[`migration/`](../../migration/README.md), and the one real-data migration has a
[record](../records/zsqlab10-migration.md).

Each ticket: implement, add pytest tests (real `dolt`, no mocks), stay inside the listed files, leave the
working tree dirty for review (no commit). Tickets in the same wave are independent and may run in
parallel (separate git worktrees); review each diff before dispatching the next.

## Wave 10 · preset parts, statuses, lineage (tickets 25–29)

Design: [`preset-parts.md`](../preset-parts.md) (source of truth) · handover:
[`HANDOVER-wave10.md`](HANDOVER-wave10.md) · source issue: git-bug `0a9acec` · starts after wave 9
(released as 0.2.0).

| # | Ticket | Depends on |
|---|---|---|
| 25 | Status sets per stock kind, transition rules, in-stock views (spec 4) | 23 |
| 26 | `lineage: true` on ref fields and `tm lineage` | 23 |
| 27 | Split molbio into parts (molbio 2 → 3) | 22, 25 |
| 28 | `cellbio` bundle: `immuno`, `viral_vectors`, `biosamples` | 26, 27 |
| 29 | `angenet` bundle: drosophila, c_elegans, zebrafish, mouse | 25–28 |

25 and 26 can run in parallel; 27–29 are sequential.

## Wave 11 · box views (ticket 30)

Source issue: git-bug `20b350c` (and `d16a3bd` for the box-schema split) · follows wave 10.

| # | Ticket | Depends on |
|---|---|---|
| 30 | One view per box, not per box kind (spec 5) | 25–29 |
