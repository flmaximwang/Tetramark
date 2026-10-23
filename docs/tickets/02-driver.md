# 02 · Driver layer & server control
Depends on: 01  · Spec: §7, §1.1, §6 (workspace/server commands)

- `driver/base.py`: `Driver` protocol (`query`, `execute`, `transaction()`, `commit(message, author)`, `database`).
  Commit = `CALL DOLT_ADD('-A'); CALL DOLT_COMMIT('-m', msg, '--author', author)`; refuse non-ASCII message (raise).
  "Nothing to commit" is not an error (return None).
- `driver/embedded.py`: `dolt sql -r json -q` subprocess in the ledger dir; parameters bound safely
  (implement a small, tested literal-escaping function — strings, None, numbers, Decimal, dates, bools);
  transaction = batch statements into one `dolt sql` invocation wrapped in START TRANSACTION/COMMIT;
  detect errors from stderr/stdout even with exit 0 → raise `LedgerError` with dolt's message.
- `driver/server.py`: PyMySQL, `127.0.0.1`, no TLS, autocommit off, `USE <db>`; unreachable → `UnreachableError`.
- `driver/workspace.py`: `Workspace` (load/create `workspace.yaml`: host, port default 13308, default_ledger,
  author), `list_ledgers()`, `start(daemon)`, `stop()`, `status()` porting `dolt_start.sh`/`dolt_stop.sh`
  (pid+log in data_dir, readiness probe ≤10 s, port-busy check), `driver_for(ledger)` → Server if up else Embedded.
- CLI: `ledger workspace init [PATH] [--port]`, `ledger server start [-d]|stop|status`.

Tests: both drivers pass the same contract test (create table, insert with tricky strings incl. quotes/CJK/backslash,
query back, rollback inside transaction, commit appears in `dolt_log`); server tests use a random free port and
are marked so they can be skipped; error-with-exit-0 detection.
