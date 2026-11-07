# 7. Driver layer

0.5.0 is **embedded by default** (P14). Every SQL statement runs through
:class:`~tetramark.driver.embedded.EmbeddedDriver`, which drives `dolt sql` in the ledger directory.
There is no `tm server start|stop|status`, no pid/port management and no user-facing driver choice.
A ledger needs only the `dolt` binary next to it — no server to start before the first command.

The embedded driver keeps **one long-lived `dolt sql -r json` session per ledger** (since
0.8.44.1). Dolt's engine startup is the dominant cost of a batch (~0.3 s per process), so the first
batch pays it once and later batches are written to the live session's stdin and framed with a unique
end marker — single-digit milliseconds each. The session is private to the driver object and closed
by `Ledger.close()`. If a script fails, Dolt exits; the driver drops the session and the next call
starts a clean one, which is exactly the state one-process-per-batch always had. JSON work uses the
session; `-r csv` column probes stay one-shot processes (they are rare, and a second long-lived
session would see a different snapshot). Remote statements (`DOLT_PULL`/`FETCH`/`PUSH`/`CLONE`/
`REMOTE`) also run one-shot after closing the registry: Dolt's manifest rewrite refuses to co-exist
with the open session. Because Dolt's manifest is writable from one process at a time, a driver
closes other drivers' sessions on the same ledger before using its own; peers restart lazily, so the
common single-driver case keeps the warm session.

**Opt-in server backend (Tetramark-0i8.8).** `driver/server.py` adds
:class:`~tetramark.driver.server.ServerDriver`, a second `Driver` implementation that starts one
real `dolt sql-server` per ledger and speaks MySQL through PyMySQL. It is selected **only** when the
effective environment sets `TETRAMARK_DRIVER=server`; the default installation and every `tm`
command keep using the embedded driver, and an install without `pymysql` never imports the module.
The server backend exists for the test suite: the ledger tests run through it so the full real-Dolt
suite fits under five minutes without mocking Dolt, while the `embedded_driver` budget tests switch
to the embedded driver in the same session. Plain `uv run pytest` is that run; `scripts/test-lanes.sh
embedded` is the default-backend regression run; see
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

Branch selection uses Dolt's **revision database** `` `db/branch` ``. The driver prefixes every
statement batch with ``USE `db/branch`;`` (the session is long-lived, so the prefix re-selects the
branch each time) and verifies the branch exists first. That is a session-scoped checkout: it
never moves the branch the repository has checked out, so other sessions (and a user-run server)
keep seeing the original branch, while this command's reads, writes, transactions and `DOLT_COMMIT`
all target the branch tip. A missing branch raises a usage error in the driver constructor; it is
never created implicitly. (`dolt sql --help` on the supported Dolt 2.3.5 has no `--branch`-style
flag, and `CALL DOLT_CHECKOUT` is forbidden because it moves the repository's working set for every
process.)

**User-run `dolt sql-server`.** If a `dolt sql-server` happens to be running on the ledger directory,
Dolt's CLI auto-routes `dolt sql` through it. That is Dolt's behaviour, not something Tetramark
detects, requires or manages: it neither starts, stops nor detects a server, running one is still not
a supported multi-writer mode (below), and no optional dependency (PyMySQL) is needed for the routed
read path. What Tetramark does own is the *result contract*: a routed read must return the same
Python types as the local engine (Tetramark-2gz).

Dolt has no flag that disables the routing, and its routed JSON result sets render columns
differently: integer columns come back as strings (`"1"` instead of `1`), while DECIMAL is a string
in both modes and CSV output is identical. `EmbeddedDriver.query` / `query_many` therefore read the
`Type` column of the `SHOW COLUMNS` probe that already runs inside the driver's long-lived JSON
session (a per-call, uniquely named temporary table that is dropped again after the probe) and
restore Python `int` for cells whose probed type is in the SQL integer family
(`tinyint`, `smallint`, `mediumint`, `int`, `integer`, `bigint`; optional display width and
`unsigned`) and whose value is a string of ASCII signed digits. Nothing else is coerced: a
`TEXT`/`VARCHAR` value `"123"` stays a string, DECIMAL and date/time values keep the shape the local
engine gives them, `NULL` stays `None`, booleans and floats are untouched, and an unsigned `BIGINT`
above `2**63` becomes an exact Python `int`. The local engine already returns integers, so the pass
is idempotent there. The probe's result sets are located by generated marker rows, not by a fixed
offset: a routed server prints an extra `{}` result line for the temporary-table DDL and blank-line
separators, so offset alignment cannot work under routing. Because the session is long-lived, every
probe uses fresh temporary-table names and drops them at the end of the call.

The read contract, in full:

- `query()` (default `fill_columns=True`) and every all-`SELECT`/`WITH` `query_many()` batch run in
  the driver's one long-lived `dolt sql -r json` session; the same-session probe supplies both column
  names and types, so NULL columns stay `None` and no second process is started per read.
- `fill_columns=False` still runs that same-session probe for types (so integers stay integers) but
  skips the NULL filling: a `NULL` column is absent, exactly as before.
- A pending transaction preview (`Driver.transaction()` + `query`) appends the same temp-table probe
  *after* its `ROLLBACK`, in the same live session and against the same committed schema, so
  previewed integer columns are typed too; the buffer stays pending and its previewed insert stays
  rolled back.
- Branch sessions run the probe inside the same revision-database session, so a branch read has the
  same types as the checked-out read.
- **Residual:** reads with no type metadata are returned exactly as Dolt reports them, with no
  heuristic coercion — non-`SELECT` statements (`SHOW`, `DESCRIBE`, `CALL`, `EXPLAIN`) and statements
  the probe rejects (for example duplicate output column names). A routed `SHOW`/probe-rejected read
  can therefore still show a numeric-looking text value; this is deliberate and tested, because
  guessing from the value would corrupt real text columns.
- **Measured boundary:** the probe's temp-table `Type` is authoritative for base columns and
  integer-typed expressions, but lossy for float/double-derived expressions, where the local engine's
  JSON shape depends on the underlying expression type (a `SUM` over an integer column probes as
  `double` yet the local engine renders an integer JSON number, while a `SUM` over a `DECIMAL`
  probes as `double` yet renders a decimal string). Those columns are left exactly as Dolt reports
  them; Tetramark's schema never creates float/double/bit/json/time columns, and the product's
  `SUM`-derived count columns cast to `int` in Python where they are reported.

**One writer per ledger** (P11). A ledger is writable from one process at a time: Dolt's manifest
is opened read-only in every other process while a session lives, and the embedded driver's session
(nor a second command) does not close the read → validate → write race between two commands.
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
