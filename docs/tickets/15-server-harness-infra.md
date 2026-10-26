# 15 · Server-backed test harness: infrastructure
Depends on: — · Spec: `docs/test-server-harness.md` §4–§5

Goal: make it possible for tests to create a **fresh isolated ledger inside a per-worker
`dolt sql-server`** without touching the embedded default path.

## Scope

0. **`src/tetramark/driver/workspace.py`** — fix the stop latency found while building the
   fixture: when `start()` and `stop()` are called by the same process (tests, REPL), the exited
   `dolt sql-server` stays a zombie; `os.kill(pid, 0)` reports it alive, so `_terminate()` burns the
   full `READY_TIMEOUT` (measured **12.0 s per stop**). Reap the child with
   `os.waitpid(pid, os.WNOHANG)` in both wait loops (`_reap_child`), and add a regression test
   asserting `stop()` is < 5 s after a real server start.
1. `src/tetramark/core/ledger.py`
   - Add `init_ledger_on_server(workspace, name, presets=None, *, id_prefix=None, author=None,
     statuses=None) -> Ledger` per spec §5.1. Reuse `_prepare_init`, `_write_meta`,
     `install_tests`, `resolve_author`; do not duplicate schema/DDL logic.
   - Requires a running workspace server; raise `UnreachableError` otherwise.
   - Use an **admin** PyMySQL connection (no database selected) to `CREATE DATABASE` / on failure
     `DROP DATABASE`; then `ServerDriver(host, port, name)` for DDL + meta + tests + commit.
   - Write `<data_dir>/<name>/ledger.yaml` exactly like the embedded path.
   - Export it in `__all__`; add a short docstring; no changes to `init_ledger` behaviour.
2. `tests/conftest.py`
   - Refactor the current `ledger_factory` body into `embedded_ledger_factory` (fixture name is
     public to tests); keep `ledger_factory` as an alias fixture that returns it, so all existing
     tests are unchanged.
   - Add session fixture `server_workspace` per spec §4: isolated env under `tmp_path_factory`,
     bootstrap bare `dolt init` ledger (so `Workspace.start()` accepts the data dir), free port,
     `workspace.start(daemon=True)`, drop stale `t_<gw>_*` databases, yield the `Workspace`,
     teardown `workspace.stop()` + assert no process remains.
   - Add function fixture `server_ledger_factory` with the same call signature/return as
     `ledger_factory`; unique names (`t_<gw>_<n>` unless `name=` given); track created ledgers;
     teardown closes drivers and `DROP DATABASE` each.
   - Worker id from `PYTEST_XDIST_WORKER` (fallback `gw0`).
   - If server start fails, `pytest.skip("dolt sql-server unavailable")` only for the server path
     (never fail the whole suite), but log clearly.
3. New `tests/test_server_harness.py`
   - `test_server_init_matches_embedded_init`: for `tests/data/presets/base` (and one bundled
     preset e.g. `molbio`), init one ledger embedded + one via `init_ledger_on_server`; assert same
     `schema.fingerprint()`, same sorted table/view names, same `ledger_meta` keys/values
     (`spec_version`, `fingerprint`, `preset_versions`, `ledger_yaml`), same `dolt_log` count and
     `dolt_status` empty.
   - `test_server_factory_isolates_tests`: two ledgers in the same test; writes to one are invisible
     in the other; both are dropped in teardown (`SHOW DATABASES` has no `t_*` residue).
   - `test_server_factory_honours_arguments`: `name=`, `id_prefix=`, `author=`, `statuses=` take
     effect (e.g. `ledger.next_id` prefix, author in `ledger.author`, status vocabulary in schema).
   - `test_server_workspace_stops_cleanly`: after fixture teardown (use a manually-managed helper
     or a nested fixture) the port is closed and `running_pid()` is `None`.

## Constraints

- Only these files may change: `src/tetramark/core/ledger.py`, `tests/conftest.py`,
  `tests/test_server_harness.py`. Do not touch other test files (later tickets do).
- No new dependencies (pymysql is already a dependency).
- Existing full suite must stay green: `PATH="<ps-shim>:$PATH" .venv/bin/python -m pytest -q`.
- Sandbox note: `ps` is blocked; prepend a shim dir containing an executable `ps` that exits 0
  for local runs. Do not add such a shim to the repo.
- Do not commit; leave the working tree dirty for the Lead to review.

## Verify

```bash
PATH="<ps-shim>:$PATH" .venv/bin/python -m pytest tests/test_server_harness.py -q -n0
PATH="<ps-shim>:$PATH" .venv/bin/python -m pytest -q -n auto   # full suite still green
```
Record in the ticket report: new-test durations, full-suite wall, spawn count of a server-ledger
init (use a tiny `subprocess.run` monkeypatch in the new test file, not a PATH wrapper).

## Result

Implemented: `init_ledger_on_server` (admin PyMySQL `CREATE DATABASE` + `ServerDriver` DDL/meta/tests/commit,
cleanup on failure), `server_workspace`/`server_ledger_factory`/`embedded_ledger_factory` fixtures,
`tests/test_server_harness.py` (9 tests, equivalence/isolation/args/failure/stop-latency).
Also fixed `Workspace.stop()` (zombie server made every stop wait 12 s; now reaps with
`os.waitpid(pid, os.WNOHANG)`, measured 0.21 s). Harness file: 9 passed / 19.3 s.
