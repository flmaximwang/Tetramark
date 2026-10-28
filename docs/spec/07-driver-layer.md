# 7. Driver layer
`Driver` interface: `query(sql, params) -> list[dict]`, `execute(sql, params)`, `transaction()` context,
`commit(message, author)`, `database` property. Two implementations:
- `ServerDriver` — PyMySQL to `dolt sql-server` (`127.0.0.1`, no TLS). Default when the workspace server is up.
- `EmbeddedDriver` — `dolt sql -r json -q` subprocess in the ledger dir. Used when no server runs and in tests.
  Must detect errors from output, not only exit codes (dolt can exit 0 on error).

Optional branch selection (`ServerDriver(..., branch=...)`, `EmbeddedDriver(..., branch=...)`) uses Dolt's
**revision database** `` `db/branch` ``; it is a session-scoped checkout and never moves the branch the
repository has checked out (so parallel sessions and the server keep seeing the original branch):

- Server: after `USE \`db\``, the driver verifies the branch in `dolt_branches`, then executes
  `USE \`db/branch\`` on its own connection. Reads, writes, transactions and `DOLT_COMMIT` all target that
  revision database, so commits land on the branch tip.
- Embedded: `dolt sql --help` on the installed Dolt 2.3.5 has **no** `--branch`-style flag (verified; open
  question Q-03), and `CALL DOLT_CHECKOUT` is forbidden because it moves the repository's working set for
  every process. Each `dolt sql` invocation is its own process, i.e. its own session, so the driver prefixes
  every statement batch with `USE \`db/branch\`;`. That is the same session-scoped mechanism as the server,
  and the driver verifies the branch exists first.

A missing branch raises a usage error in the driver constructor; it is never created implicitly. The
checked-out branch of the repository is never changed by either driver.

`tm server start|stop|status` ports `dolt_start.sh`/`dolt_stop.sh`: pid/log in data_dir, readiness
probe, port-busy detection, lists ledgers (subdirs with `.dolt`).

Target Dolt ≥ 2.3.5. All SQL must work on 2.3.5 (no multi-statement trigger bodies, no REGEXP in CHECK).

---
