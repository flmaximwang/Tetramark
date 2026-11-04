# 7. Driver layer

0.5.0 is **embedded by default** (P14). Every SQL statement runs through
:class:`~tetramark.driver.embedded.EmbeddedDriver`, which executes `dolt sql -r json -q` as a
subprocess in the ledger directory. There is no `tm server start|stop|status`, no pid/port
management and no user-facing driver choice. A ledger needs only the `dolt` binary next to it — no
long-lived process, no data directory, no server to start before the first command.

**Opt-in server backend (Tetramark-0i8.8).** `driver/server.py` adds
:class:`~tetramark.driver.server.ServerDriver`, a second `Driver` implementation that starts one
real `dolt sql-server` per ledger and speaks MySQL through PyMySQL. It is selected **only** when the
effective environment sets `TETRAMARK_DRIVER=server`; the default installation and every `tm`
command keep using the embedded driver, and an install without `pymysql` never imports the module.
The server backend exists for the test lane: the same real-Dolt tests run through it so the full
suite fits under five minutes without mocking Dolt. `scripts/test-lanes.sh full` is that lane and
`scripts/test-lanes.sh embedded` is the default-backend regression run; see
`docs/guides/test-harness.md`. Server sessions still make exactly one `DOLT_COMMIT` per write
command, and DDL keeps Dolt's implicit-commit semantics.

The `Driver` abstract base class (`driver/base.py`) is deliberately **kept**: `query(sql, params) ->
list[dict]`, `query_many(statements) -> list[list[dict]]`, `execute(sql, params)`,
`execute_many(statements)`, a `transaction()`
context, `commit(message, author)` and a `database` property. `query_many` runs independent read-only
statements in **one** `dolt sql` process (one JSON run for the rows plus at most one CSV run for the
column headers JSON omits); `tm find` uses it so a search costs one round trip instead of one process
per kind and per hit. `execute_many` is the write-side counterpart: it runs a batch of writing
statements in order in **one** `dolt sql` process, so a schema change (init, adopt, a migration step,
`tm schema apply`) costs one process instead of one per DDL statement; the batch keeps the statement
order, and a failing statement is named in the error without re-running the DDL that already ran. A
server mode is opt-in (`TETRAMARK_DRIVER=server`, above) and adds a second
implementation without touching the core; the default remains `EmbeddedDriver`.

Branch selection uses Dolt's **revision database** `` `db/branch` ``. Each `dolt sql` invocation is
its own process, i.e. its own session, so the driver prefixes every statement batch with
``USE `db/branch`;`` and verifies the branch exists first. That is a session-scoped checkout: it
never moves the branch the repository has checked out, so other sessions (and a user-run server)
keep seeing the original branch, while this command's reads, writes, transactions and `DOLT_COMMIT`
all target the branch tip. A missing branch raises a usage error in the driver constructor; it is
never created implicitly. (`dolt sql --help` on the supported Dolt 2.3.5 has no `--branch`-style
flag, and `CALL DOLT_CHECKOUT` is forbidden because it moves the repository's working set for every
process.)

**User-run `dolt sql-server`.** If a `dolt sql-server` happens to be running on the ledger directory,
Dolt's CLI auto-routes `dolt sql` through it. That is Dolt's behaviour, not something Tetramark
detects, requires or manages: 0.5.0 neither starts nor stops a server, and running one is not a
supported multi-writer mode (below).

**One writer per ledger** (P11). The embedded driver's reads are separate `dolt sql` processes, so
Dolt's own noms/LOCK does not close the read → validate → write race between two commands.
`core/writelock.py` adds that with `fcntl.flock` on `<ledger>/.dolt/tetramark.lock`:

- a CLI write command takes the lock **before** opening the ledger (config, schema, `ledger_meta`)
  and holds it through the command body, its transaction and its Dolt commit; the Python API's
  `WriteSession` takes it around the transaction + commit, and callers that read before writing can
  wrap the whole step in `with ledger.hold_write_lock():`;
- a second writer retries every 50 ms and gives up after `DEFAULT_LOCK_TIMEOUT = 10.0` seconds
  (override: `$TETRAMARK_LOCK_TIMEOUT`, seconds as a float). It then raises
  `LedgerBusyError` (a `UsageError`, exit code 2):
  `ledger busy: pid 4711 (tm stock add) is writing (waited 10.0s; set TETRAMARK_LOCK_TIMEOUT=<seconds>
  to wait longer)`;
- the lock file is created when missing and left in place after release; it records one JSON line
  (`pid`, `command`, `since`, `host`) so a waiter or `tm status` can name the writer;
- **reads never lock**; `tm status` only probes the lock (non-blocking, non-creating) and `--dry-run`
  writes nothing, so neither creates a lock file;
- re-entrancy is per `Ledger` object: a `WriteSession` inside a `hold_write_lock()` scope joins the
  outer lock instead of deadlocking against itself.

Target Dolt ≥ 2.3.5. All SQL must work on 2.3.5 (no multi-statement trigger bodies, no REGEXP in CHECK).
