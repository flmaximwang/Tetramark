# Test harness — one command, two real backends, hard wall-clock limits

Status: **0.6.1** (Tetramark-0i8). Every test still runs against the real `dolt` binary — never a
mock. **`uv run pytest` is the whole suite**: ledger tests use a managed `dolt sql-server`, the
`embedded_driver` process-budget tests switch to the embedded one-`dolt sql`-process backend in the
same session, and the session fails itself if it takes more than **300 s**. The product itself still
defaults to the embedded driver.

## 1. Cost model

* **Embedded driver (default in the product).** A batch of SQL is one `dolt sql` subprocess in the
  ledger directory: roughly **60–100 ms per process** on an idle machine, 0.2–0.4 s under
  ten-worker load. Tetramark-0i8.x removed every avoidable process (init 3, schema plan 2,
  list/show 2, a write session 2, open 1, …), which brought the suite from 1643 s to ~13 min. That
  is the floor of this backend: the suite still executes thousands of processes and this machine
  tops out at 17–23 `dolt sql` starts/s.
* **Server driver (default in the test harness).** The same statements run over the MySQL protocol
  against one `dolt sql-server` per ledger: **~0.3 s once** for the server, then **~0.3 ms per
  statement**. That is what makes the *whole* suite — including the embedded budget tests — fit in
  **under three minutes (177–189 s)** on the reference machine. The data is identical: same Dolt binary, same `.dolt`
  directory.

The backend is selected per ledger operation from the effective environment: the `env=` mapping a
caller passes to `init_ledger`/`adopt_ledger`/`open()` when there is one (tests always pass one),
else `os.environ`. `tests/conftest.py` defaults the session to `TETRAMARK_DRIVER=server` and an
autouse fixture switches `embedded_driver`-marked tests to `embedded` (and back) before their other
fixtures run.

## 2. One command and its limits

```sh
uv run pytest                 # THE full suite: 300 s wall-clock ceiling, 60 s per test
uv run pytest -q -m fast      # Dolt-free unit tests: 30 s ceiling
```

The limits are enforced by the suite itself, not by a wrapper:

- **Session wall clock — 300 s.** `tests/conftest.py` starts a timer on the pytest controller at
  `pytest_sessionstart`, collects every test's call duration, and on `pytest_sessionfinish` prints
  the elapsed time and the 25 slowest tests, sets the exit status non-zero, and fails the run even
  if every test passed. `TETRAMARK_SUITE_BUDGET_SECONDS=<seconds>` overrides the budget for
  profiling; an empty value disables the guard.
- **Fast lane — 30 s.** The same guard selects 30 s when the mark expression contains `fast`.
- **Per-test timeout — 60 s.** `pytest-timeout` is in the dev group and
  `[tool.pytest.ini_options] timeout = 60` makes a hung Dolt server or deadlocked fixture fail the
  run instead of stalling it. A test that legitimately needs longer raises it locally with
  `@pytest.mark.timeout(<seconds>)` **and** a comment explaining why; do not pad the global value.
- **No skips to save time.** The 88 `embedded_driver` budget tests are not skipped under the server
  default: they run with the embedded driver in the same session. The only allowed skip is an
  optional external artifact (currently the 0.4.2 lab-ledger snapshot when it is absent).

Measured after Tetramark-0i8 (reference machine, 10-core macOS, `-n auto --dist worksteal`):
**177–189 s, 1337 passed, 1 skipped, 0 leftover `dolt sql-server`** (two final runs: 188.8 s, 187.9 s).
The fast lane is ~2.5 s (729 passed).

## 3. Server lifecycle

- the server starts **lazily** on the first statement, on a free loopback port, and is polled until
  PyMySQL can connect;
- `driver.close()` closes the connection but keeps the server in a small **LRU cache keyed by the
  ledger's resolved path** (`$TETRAMARK_SERVER_CACHE`, default 6; `0` disables reuse and kills the
  server on close). Without the cache every `tm` command in a doc test would pay the 0.3 s start;
- the cache validates the directory's `(st_dev, st_ino)` and Tetramark's own
  `<ledger>/.dolt/tetramark-server.json` sidecar (path + inode), so a copied or replaced directory
  never reuses another path's server. Dolt's own `.dolt/sql-server.info` stays in place so a plain
  `dolt sql` in that directory auto-routes — that is Dolt's documented behaviour and it keeps
  subprocess helpers (raw `dolt sql`, CLI subprocesses) working;
- a subprocess (CLI, migration script) **borrows** the parent's server through that sidecar instead
  of failing to start a second one (a database directory allows only one `dolt sql-server`);
- every owned server is killed on eviction, on `pytest_sessionfinish`/`pytest_unconfigure`, and at
  interpreter exit. A green run leaves **zero** `dolt sql-server` children.

## 4. Fixtures and layout

```
tests/conftest.py
  dolt_binary / dolt_env / dolt_run / dolt_conf   isolated real-Dolt environment
  isolated_env(root, home)                        DOLT_ROOT_PATH, HOME, XDG_*, GIT_*
  template_ledgers                                session LedgerTemplates (per worker)
  embedded_ledger_factory / ledger_factory        copy a session template (or real init)
  make_ledger / dolt_repo                         bare Dolt databases for adopt/raw tests
  autouse _backend_for_test                       server by default, embedded for embedded_driver
  pytest_sessionstart / pytest_runtest_logreport  wall-clock budget and slowest-test report
  pytest_sessionfinish / pytest_unconfigure       kill every cached dolt sql-server
tests/data/presets/                               preset fixtures used by the templates
tests/data/ledgers/                               checked-in historical databases + builders
tests/test_server_driver.py                       server-backend contract, cache, cleanup, borrowing
```

- Use `ledger_factory([...])` for "a normal ledger of this shape"; it returns an opened `Ledger`.
  Under the server default that `Ledger` talks to a `dolt sql-server`, exactly like `tm` would.
- **Session templates are built once per pytest worker and then copied.** `LedgerTemplates.ledger()`
  closes the `Ledger` `init_ledger` returns and (with the server backend) stops its cached server before
  publishing the path, so `install()` always copies a **closed** directory: no server is mutating
  it and no discovery file points at a live port.
- **Budget tests may build their fixture data through the server and then reopen the same ledger
  embedded.** Building rows is setup, not the read under test; the process counts are cleared after
  setup. `tests/test_list_round_trips.py`, `test_csv_round_trips.py` and
  `test_lineage_round_trips.py` follow this pattern: one populated ledger per worker, reopened with
  the embedded driver, read-only tests share it.
- Use `dolt_repo` / `make_ledger` when the test is about a bare database or `tm adopt`.
- Isolated Dolt config: `DOLT_ROOT_PATH`, `HOME`, `XDG_CONFIG_HOME` and the `GIT_CONFIG_*`
  variables point into `tmp_path`, so the developer's global Dolt/Git configuration is never read or
  written. No ledger-selection environment variable exists.
- The lab directory `~/Documents/Lab/Dolt` is only ever **read** in the optional read-only snapshot
  test (copy it under `tmp_path` first if a test needs to write).

## 5. Markers and selection

- `fast` — Dolt-free; the only mark the fast lane runs. The module owns it with
  `pytestmark = pytest.mark.fast` (or per-test decorators) and the module must not import/execute
  Dolt work at collection time.
- `embedded` — the test exercises the embedded driver explicitly and needs the real `dolt` binary
  (historical selection mark; `-m embedded` still works).
- `embedded_driver` — the test **pins the embedded driver's process budget** by counting
  `EmbeddedDriver._invoke` calls (`tests/test_*_round_trips*.py`, `test_find_batch.py`,
  `test_driver_query_many.py`, `test_driver_contract.py`). The autouse fixture switches the backend
  to `embedded` for the test, so those counts are real even though the rest of the session uses the
  server driver. Never replace the marker with a skip.
- Default addopts: `-ra -n auto --dist worksteal`.

Common commands:

```sh
uv run pytest                                    # full suite (both backends, 300 s guard)
uv run pytest -q -m fast                         # fast lane (seconds, 30 s guard)
uv run pytest -n0 tests/test_status.py           # one file, no xdist
uv run pytest -q -m embedded                     # real-Dolt integration set
uv run pytest -k "backup or lock"                # focused by keyword
TETRAMARK_DRIVER=embedded uv run pytest          # whole suite on the default backend (~12 min)
TETRAMARK_SUITE_BUDGET_SECONDS=600 uv run pytest # profiling only
```

`scripts/test-lanes.sh` runs the same plain commands (fast, then full), prints wall times and the
number of `dolt sql-server` processes the workers started, and fails if one survives
(`LEFTOVER_CHECK=0` disables that check on a machine with an unrelated server;
`PYTEST=/path/to/.venv/bin/pytest scripts/test-lanes.sh full` works when `uv run` does not). It does
**not** set `TETRAMARK_DRIVER` and adds no budget of its own: the suite is authoritative.

## 6. Rules for adding tests

1. **Keep the fast lane fast.** New pure-Python tests are marked fast; a test that needs Dolt is not.
2. **Reuse the session templates.** A test that just needs a ledger uses `ledger_factory`; only a
   test about init/adopt/preset generation pays for a real init (`real_init=True`).
3. **Justify real-Dolt cost.** One end-to-end flow per behaviour is enough; extend an existing flow
   instead of adding a second ledger. Share populated fixtures across read-only tests.
4. **Docs are executable.** `tests/test_docs_commands.py` runs the flows documented in the README,
   the command reference, the migration recipe and the schema skill; when a doc example changes, that
   test changes with it.
5. **Never weaken a test to make a lane green.** Adapt paths/flags (e.g. `-l PATH` instead of
   `--workspace`) and fixture names; keep the assertion. Do not re-add skips or widen budgets.
6. **Never mock Dolt** and never hand-fake a database directory; copy a template, a fixture or run a
   real init. The server default still starts the real `dolt sql-server` — no MySQL mock, no faked
   rows.
7. **Database tests never manage a server by hand.** They use the driver (embedded or server, chosen
   by the marker/environment). The only server lifecycle code is `tetramark/driver/server.py`; tests
   assert its cleanup through `tests/test_server_driver.py`.
8. **No workspace markers.** A test must not create `workspace.yaml`; the standalone-ledger guard
   test greps for it. Resolution tests use stub directories and belong to the fast lane.
9. **Isolate the environment.** Use the fixtures; never rely on the developer's Dolt/Git config or on
   `~/Documents/Lab`.
10. **Clean up.** Every opened `Ledger` is closed (the factory does it); no `dolt` child processes
    survive a green run. The pytest hooks kill cached servers; `scripts/test-lanes.sh` checks for
    leftovers.

## 7. Release acceptance

Before a release run and record:

- `uv run pytest` — green, **inside the 300 s guard** (reference: 177–189 s, 1337 passed, 1 optional
  skip), no leftover `dolt sql-server`;
- `uv run pytest -q -m fast` — green, inside the 30 s guard;
- `TETRAMARK_DRIVER=embedded uv run pytest` — green (the product's default backend, ~12 min);
- the heavy migration/lab subset is part of the full run; the 0.4.2 snapshot test skips cleanly when
  its directory is absent;
- `scripts/test-lanes.sh` (fast + full, with stats and the leftover check) — exit 0.
