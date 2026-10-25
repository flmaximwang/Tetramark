# Tickets

These tickets are historical. Package and CLI names in the text predate the Tetramark rename; the current package is `tetramark` and the CLI is `tm`.

Read `docs/spec.md` first; section numbers below refer to it. Each ticket: implement, add pytest tests
(real `dolt`, no mocks), `uv run pytest` green, stay inside the listed files where possible.

| # | Ticket | Depends on | Wave |
|---|---|---|---|
| 01 | Scaffold & test harness | — | 1 |
| 02 | Driver layer & server control | 01 | 2 |
| 03 | Schema model & loader | 01 | 2 |
| 04 | ID strategies | 01 | 2 |
| 05 | DDL & view generation | 03 | 3 |
| 06 | Ledger init & open | 02, 04, 05 | 4 |
| 07 | Built-in presets | 05, 06 | 5 |
| 08 | Validation → dolt_tests & `ledger check` | 06 | 5 |
| 09 | Schema plan/apply | 06 | 5 |
| 10 | Object operations (entity/stock/aliquot/box/alias) | 06 | 5 |
| 11 | Read side: find, box show, ls, history | 10 | 6 |
| 12 | CSV import/export | 10 | 6 |
| 13 | Remote, backup, author config | 06 | 5 |
| 14 | Skills, migration rulebook, README | 07, 08, 09, 11, 12 | 7 |

Tickets in the same wave are independent and may run in parallel (separate git worktrees).
