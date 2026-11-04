# Test harness — real Dolt, embedded by default, a server lane for speed

Status: **0.6.0** (Tetramark-0i8.8). Every integration test still runs against the real `dolt`
binary — never a mock. The default backend is the embedded driver (one `dolt sql` subprocess per
batch); an opt-in **server lane** runs the same tests through a managed `dolt sql-server`, which is
how the full suite fits in the five-minute budget. `scripts/test-lanes.sh full` is that lane;
`scripts/test-lanes.sh embedded` proves the default backend is still green.

## 1. Cost model

* **Embedded driver (default).** A batch of SQL is one `dolt sql` subprocess in the ledger
  directory. On an idle machine that is roughly **60–100 ms per process** (0.2–0.4 s under
  ten-worker load), because Dolt starts a process and opens the database for every batch. The
  Tetramark-0i8.x tickets removed every avoidable process (init is 3, schema plan 2, list/show 2,
  a write session 2, open 1, …), which brought the suite from 1643 s to ~13 min. That is the floor
  of this backend: the suite still executes thousands of processes and the machine tops out around
  17–23 `dolt sql` starts per second.
* **Server driver (opt-in).** The same statements run over the MySQL protocol against one
  `dolt sql-server` per ledger: **~0.3 s once** for the server, then **~0.3 ms per statement**. The
  full real-Dolt suite then fits under the 300 s ceiling. Nothing about the data is different: the
  server is the same Dolt binary on the same `.dolt` directory.

Selection is per ledger operation, not a global switch in the product. The test harness defaults
`TETRAMARK_DRIVER=server` in `tests/conftest.py`, so a plain `pytest`/`uv run pytest` is the fast
real-Dolt server lane; the product itself still defaults to the embedded driver:

```sh
pytest -q                                  # server lane (the conftest default; ~135 s)
TETRAMARK_DRIVER=embedded pytest -q        # the embedded/default-backend lane (~12 min)
scripts/test-lanes.sh embedded             # same, with the lane script's reporting
```

The effective environment is the `env=` mapping a caller passes to `init_ledger`/`adopt_ledger`/
`open()` when there is one (tests always pass one), else `os.environ`. A CLI subprocess inherits the
variable from the test process; an in-process `CliRunner` command sees `os.environ` directly.

Server lifecycle details that matter when debugging:

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
  interpreter exit. A green run leaves **zero** `dolt sql-server` children; the lane script checks
  with `pgrep` and fails if one survives.

## 2. The lanes and their budgets

| Lane | Command | Budget | What belongs there |
|---|---|---|---|
| **fast** | `pytest -q -m fast` | **seconds** (0.5.0 baseline 2.38 s) | Dolt-free unit tests: resolution, YAML/schema model, id strategies, renderers, CLI help/panels, error messages |
| **full** | `pytest` (conftest defaults `TETRAMARK_DRIVER=server`) or `scripts/test-lanes.sh full` | **≤ 300 s** hard ceiling: the real-Dolt suite through `dolt sql-server` | everything, including every real-Dolt integration flow |
| **embedded** | `TETRAMARK_DRIVER=embedded pytest` or `scripts/test-lanes.sh embedded` | measured 732 s (12:11) on the reference machine; optional gate via `EMBEDDED_LANE_BUDGET_SECONDS` | the same tests through the default one-process-per-batch backend; run before a release to prove the default path |
| **release** | fast + full + embedded + the heavy migration/lab subset | green, no new failures | `tests/test_migrate.py`, the read-only 0.4.2 lab-ledger check when `.dsh/tm-l8g/lab-snapshot` exists, docs flows, and `scripts/test-lanes.sh` |

Rules:

- **Never put a database test on the fast lane.** If it opens a `Ledger`, calls `init_ledger`/`open`,
  or shells out to `dolt`, it is not `fast`.
- **The default `full` lane is the server lane.** It is still real Dolt and still exercises every
  flow; it is the lane the five-minute requirement talks about. Measured on the reference machine
  after Tetramark-0i8.8: **144 s** server lane (1219 passed, 89 embedded-budget skips, 424 server
  starts, 0 leftovers) and **732 s** embedded lane (1307 passed). The embedded lane is the same test
  content on the slower default backend.
- **`-n auto`, not `-n 4`.** The suite is dominated by independent ledger directories and processes;
  `-n auto --dist worksteal` is the configured default (`addopts`). Measure on the target machine
  instead of copying a worker count from another one.
- **`scripts/test-lanes.sh` is the enforcement point.** The repository has no CI, so this script —
  run before a merge and before a release — turns the budgets into a failing exit code. It prints
  wall time and the number of `dolt sql-server` starts reported by the workers
  (`TETRAMARK_SERVER_STATS`), then checks for leftover servers (`LEFTOVER_CHECK=0` disables that
  check on a machine that legitimately runs an unrelated server). In sandboxes where `uv run` does
  not work, point it at a venv pytest:
  `PYTEST=/path/to/.venv/bin/pytest scripts/test-lanes.sh full`.
- **The wall time is content-bound, not worker-bound.** A single test can still dominate its file
  under `--dist worksteal`; optimise the content, not the worker count.

## 3. Fixtures and layout

```
tests/conftest.py
  dolt_binary / dolt_env / dolt_run / dolt_conf   isolated real-Dolt environment
  isolated_env(root, home)                        DOLT_ROOT_PATH, HOME, XDG_*, GIT_*
  template_ledgers                                session LedgerTemplates (per worker)
  embedded_ledger_factory / ledger_factory        copy a session template (or real init)
  make_ledger / dolt_repo                         bare Dolt databases for adopt/raw tests
  pytest_sessionfinish / pytest_unconfigure       kill every cached dolt sql-server
  pytest_collection_modifyitems                   skip embedded_driver budgets on the server lane
tests/data/presets/                               preset fixtures used by the templates
tests/data/ledgers/                               checked-in historical databases + builders
tests/test_server_driver.py                       server-backend contract, cache, cleanup, borrowing
```

- Use `ledger_factory([...])` for "a normal ledger of this shape"; it returns an opened `Ledger`.
  Under the server lane that `Ledger` talks to a `dolt sql-server`, exactly like `tm` would.
- **Session templates are built once per pytest worker and then copied.** `LedgerTemplates.ledger()`
  closes the `Ledger` `init_ledger` returns and (on the server lane) stops its cached server before
  publishing the path, so `install()` always copies a **closed** directory: no server is mutating
  it and no discovery file points at a live port.
- Use `dolt_repo` / `make_ledger` when the test is about a bare database or `tm adopt`.
- Isolated Dolt config: `DOLT_ROOT_PATH`, `HOME`, `XDG_CONFIG_HOME` and the `GIT_CONFIG_*`
  variables point into `tmp_path`, so the developer's global Dolt/Git configuration is never read or
  written. No ledger-selection environment variable exists.
- The lab directory `~/Documents/Lab/Dolt` is only ever **read** in the optional read-only snapshot
  test (copy it under `tmp_path` first if a test needs to write).

## 4. Markers and selection

- `fast` — Dolt-free; the only mark the fast lane runs. The module owns it with
  `pytestmark = pytest.mark.fast` (or per-test decorators) and the module must not import/execute
  Dolt work at collection time.
- `embedded` — the test exercises the embedded driver explicitly and needs the real `dolt` binary
  (historical selection mark; `-m embedded` still works).
- `embedded_driver` — the test **pins the embedded driver's process budget** by counting
  `EmbeddedDriver._invoke` calls (`tests/test_*_round_trips*.py`, `test_find_batch.py`,
  `test_driver_query_many.py`, `test_driver_contract.py`). Those counts are meaningless on the server
  lane, which has no per-statement process, so `tests/conftest.py` skips the marked tests when
  `TETRAMARK_DRIVER=server`. The embedded lane still enforces every budget.
- Default addopts: `-ra -n auto --dist worksteal`.

Common commands:

```sh
uv run pytest -q -m fast                         # fast lane (seconds)
TETRAMARK_DRIVER=server uv run pytest            # full lane, server backend (the 300 s lane)
uv run pytest                                    # full lane, embedded default (~13 min)
uv run pytest -n0 tests/test_status.py           # one file, no xdist
uv run pytest -q -m embedded                     # real-Dolt integration set
uv run pytest -k "backup or lock"                # focused by keyword
```

## 5. Rules for adding tests

1. **Keep the fast lane fast.** New pure-Python tests are marked fast; a test that needs Dolt is not.
2. **Reuse the session templates.** A test that just needs a ledger uses `ledger_factory`; only a
   test about init/adopt/preset generation pays for a real init (`real_init=True`).
3. **Justify real-Dolt cost.** One end-to-end flow per behaviour is enough; extend an existing flow
   instead of adding a second ledger. Put the cost in the ticket report when adding a new module.
4. **Docs are executable.** `tests/test_docs_commands.py` runs the flows documented in the README,
   the command reference, the migration recipe and the schema skill; when a doc example changes, that
   test changes with it.
5. **Never weaken a test to make a lane green.** Adapt paths/flags (e.g. `-l PATH` instead of
   `--workspace`) and fixture names; keep the assertion.
6. **Never mock Dolt** and never hand-fake a database directory; copy a template, a fixture or run a
   real init. The server lane still starts the real `dolt sql-server` — no MySQL mock, no faked rows.
7. **Database tests never manage a server by hand.** They use the driver (embedded or server, chosen
   by the env). The only server lifecycle code is `tetramark/driver/server.py`; tests assert its
   cleanup through `tests/test_server_driver.py`.
8. **No workspace markers.** A test must not create `workspace.yaml`; the standalone-ledger guard
   test greps for it. Resolution tests use stub directories and belong to the fast lane.
9. **Isolate the environment.** Use the fixtures; never rely on the developer's Dolt/Git config or on
   `~/Documents/Lab`.
10. **Clean up.** Every opened `Ledger` is closed (the factory does it); no `dolt` child processes
    survive a green run. The lane script checks for leftover `dolt sql-server` processes; the pytest
    hooks also kill cached ones from a failed run.

## 6. Release acceptance

Before a release run and record:

- `uv run pytest -q -m fast` — green, seconds;
- `scripts/test-lanes.sh full` — green, wall time inside the 300 s server-lane budget, no leftover
  `dolt sql-server`;
- `scripts/test-lanes.sh embedded` — green (the default backend, ~13 min);
- the heavy migration/lab subset — green: `tests/test_migrate.py` and the read-only 0.4.2 snapshot
  test (skipped cleanly when the snapshot directory is absent);
- `scripts/test-lanes.sh` (fast + full) — exit 0.
