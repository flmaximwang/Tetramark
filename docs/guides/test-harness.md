# Test harness — one command, two real backends, hard wall-clock limits

Status: **0.6.1** (Tetramark-0i8). Every test still runs against the real `dolt` binary — never a
mock. **`uv run pytest` is the whole suite**: ledger tests use a managed `dolt sql-server`, the
`embedded_driver` process-budget tests switch to the embedded one-`dolt sql`-process backend in the
same session, and the session fails itself if it takes more than **300 s**. The product itself still
defaults to the embedded driver.

## 1. Cost model

* **Embedded driver (default in the product).** Dolt engine startup dominates a batch (~0.3 s per
  `dolt sql` process), so since 0.8.44.1 the driver keeps **one long-lived `dolt sql -r json`
  session per ledger**: the first batch pays the startup once, later batches are written to the live
  session and take **single-digit milliseconds** (measured: first 0.3 s, subsequent ~1 ms). CSV
  column probes stay one-shot processes. Tetramark-0i8.x removed every avoidable process (init 3, schema plan 2,
  list 2, show 2 batches, a write session 2, open 1, …), which brought the suite from 1643 s to ~13 min. That
  is the floor of this backend: the suite still executes thousands of processes and this machine
  tops out at 17–23 `dolt sql` starts/s. Since Tetramark-aut a `SELECT`/`WITH` read (or batch) is
  **one** process, not two: the same script prints each statement's columns through an empty
  temporary table (`SHOW COLUMNS`), replacing the `-r csv` header run that restored `NULL` columns.
  The add-form also caches box rows/free slots per session and lists completions with their kind
  in one query (choosing a kind used to cost one process per listed object).
* **Server driver (default in the test harness).** The same statements run over the MySQL protocol
  against one `dolt sql-server` per ledger: **~0.3 s once** for the server, then **~0.3 ms per
  statement**. That is what makes the *whole* suite — including the embedded budget tests — fit in
  **about 3.5 minutes (212 s)** on the reference machine. The data is identical: same Dolt binary, same `.dolt`
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

- **Session wall clock — 300 s.** `tests/conftest.py` starts a timer on the pytest controller in
  `pytest_configure` (first), collects every test's call duration, and in a `trylast`
  `pytest_sessionfinish` — after xdist's worker teardown and pytest's own tmp_path cleanup, which
  are part of the run — prints the elapsed time and the 25 slowest tests, sets the exit status
  non-zero, and fails the run even if every test passed. (Before Tetramark-aut the check ran
  before the tmp_path plugin deleted old `pytest-N` directories, so a loaded machine could spend
  minutes there unseen: a 158 s fast lane exited 0.) `TETRAMARK_SUITE_BUDGET_SECONDS=<seconds>` overrides the budget for
  profiling; an empty value disables the guard.
- **Fast lane — 30 s.** The same guard selects 30 s when the mark expression contains `fast`.
- **Per-test timeout — 60 s.** `pytest-timeout` is in the dev group and
  `[tool.pytest.ini_options] timeout = 60` makes a hung Dolt server or deadlocked fixture fail the
  run instead of stalling it. A test that legitimately needs longer raises it locally with
  `@pytest.mark.timeout(<seconds>)` **and** a comment explaining why; do not pad the global value.
- **No skips to save time.** The 88 `embedded_driver` budget tests are not skipped under the server
  default: they run with the embedded driver in the same session. The only allowed skip is an
  optional external artifact (currently the 0.4.2 lab-ledger snapshot when it is absent).
- **Concurrency — 16 xdist workers.** The suite's time goes to waiting on `dolt sql` subprocesses
  and `dolt sql-server` round trips rather than to Python CPU: ten workers (`-n auto` on the
  10-core reference host) left only ~2.4 cores busy and the full suite stayed around 274 s.
  Sixteen workers keep the same 1997 tests and every budget, but overlap more of those waits:
  measured **253 s** idle (273.7 s at 10 workers, 265 s at 14). Twenty workers reached ~250 s but
  starved the pinned migration drift-guard test past its unchanged 60 s timeout, so 16 is the
  settled value; a host with a different core count may pass its own `-n`. This is a scheduling
  control only — it changes no test, assertion, marker or timeout.
- **Failure investigation.** A budget breach (fast lane > 30 s, full suite > 300 s) or a 60 s
  per-test timeout is a failed gate, not an exemption — a loaded machine never turns it into a pass.
  Read the slowest-25 report the session prints (test, setup/call/teardown, module) and count
  `dolt sql-server` processes before and after the run (`pgrep -f 'dolt sql-server'`) so the logs
  show whether the time went to code, to a leaked/extra server, or to contention. Separate a code
  defect from demonstrated resource contention; then fix the defect or manage only your own test
  workload (do not overlap expensive suites, use the supported concurrency controls without
  weakening the gate, or retry once resources are free). Never raise a budget or the global
  timeout, add `xfail`, skip or deselect required tests, or shrink the suite to manufacture a green
  run.
- **The budget guard is tested with an injected clock, never with a real sleep.** The guard reads
  the module attribute `tests/conftest.py::_suite_clock`, which defaults to `time.monotonic`; a test
  replaces **that attribute only** (never the global `time.monotonic`/`time.sleep`, which pytest,
  xdist, PyMySQL and the Dolt supervision code read). The regression in
  `tests/test_suite_budget.py` therefore installs a fake clock in the inner session and asserts the
  exact banner `SUITE TIME BUDGET EXCEEDED: 5.0s > 3s`: the elapsed time is the fake clock's, so a
  loaded machine cannot turn the test red. Real time still decides the production behaviour: the
  real clock and the 30 s / 300 s / 60 s budgets are unchanged, and the inner session's own
  subprocess ceilings (110 s / 120 s) stay as they are.
- **Whole-session-finish coverage is proved from both sides.** The guard is `trylast`, so it runs
  after every other `sessionfinish` — xdist's worker teardown and pytest's tmp_path cleanup
  included. The positive regression advances the fake clock in a plain plugin hook (which pluggy
  calls before the `trylast` guard) and expects the run to fail; the ordering negative control
  advances it in a `hookwrapper`, whose post-yield code runs after the guard has measured, and
  expects the run to stay green. Dropping `trylast` from the guard turns the first test red and
  leaves the second green — the pair discriminates the ordering instead of asserting a constant.

Measured after Tetramark-aut (reference machine, 10-core macOS, `-n 16 --dist worksteal`, idle,
2026-11-07): the full suite took **253.05 s internal (253 s wall) with 1995 passed, 2 skipped,
660 `dolt sql-server` starts, 0 leftover servers and a slowest test call of ~30 s**, and the fast
lane takes **12.8 s with 1009 passed** — both inside their unchanged 30 s / 300 s / 60 s limits.
Idle wall time at ten workers was already inside the budget (273.68 s on clean base `87958e9`,
272.44 s on the change tree) and the concurrency change lowers it further (265 s at 14 workers).
What also changed is the duplicate build work: fixture-setup/build counts fell **13 → 6**
(source 3→1, exported 3→1, template 3→1, read_ledger 2→1; `rebuilt_molbio_v1` and
`rebuilt_spec4_boxes` were already 1→1), with exactly one real shared build per key per session.
A budget breach is still a failed gate: the concurrency value is a measured scheduling control,
not an exemption.

## 3. Server lifecycle

- the server starts **lazily** on the first statement, on a free loopback port, and is polled until
  PyMySQL can connect;
- `driver.close()` closes the connection but keeps the server in a small **LRU cache keyed by the
  ledger's resolved path** (`$TETRAMARK_SERVER_CACHE`, default 6; `0` disables reuse and kills the
  server on close). Without the cache every `tm` command in a doc test would pay the 0.3 s start;
- **the cache is an idle-reuse policy, not a bound on live servers.** `_enforce_cache_cap` evicts
  idle entries, but active references may overrun the cap, `_release` does not shrink them, a
  server created with `cache=0` is never cached, and legacy fixture builders start independent
  servers outside the `ServerDriver` cache. Count actual `dolt sql-server` processes when
  investigating load — never reason from "workers × cache";
- the cache validates the directory's `(st_dev, st_ino)` and Tetramark's own
  `<ledger>/.dolt/tetramark-server.json` sidecar (path + inode), so a copied or replaced directory
  never reuses another path's server. Dolt's own `.dolt/sql-server.info` stays in place so a plain
  `dolt sql` in that directory auto-routes — that is Dolt's documented behaviour and it keeps
  subprocess helpers (raw `dolt sql`, CLI subprocesses) working;
- a subprocess (CLI, migration script) **borrows** the parent's server through that sidecar instead
  of failing to start a second one (a database directory allows only one `dolt sql-server`);
- **ownership is per session.** A session stops only the servers it started: every owned server is
  killed on eviction, on `pytest_sessionfinish`/`pytest_unconfigure`, and at interpreter exit. A
  green run leaves **zero** `dolt sql-server` children;
- a run that is *killed* (SIGKILL, a cancelled agent command, a crashed worker) cannot clean up, and
  its servers are reparented to pid 1. Each new session therefore starts with
  `tetramark.driver.server.reap_orphaned_servers()`, which touches only those reparented servers: a
  server in Tetramark's exact argv shape is stopped when its ledger lies under pytest's per-user
  temp root or its own sidecar names an owner pid that has exited. A server whose owner is alive is
  never a candidate, whoever owns it — the reaper never stops another live session's work. Left
  alone, leaked servers starved later runs into timeouts that leaked more servers (issue #68: 100
  live servers, 641 s).

### Routed-parity regression (Tetramark-2gz)

`tests/test_driver_routed_parity.py` is the one test module that deliberately runs a **user-run
`dolt sql-server`** while it reads through the *embedded* driver. It proves the spec 7 read contract
against the real binary: a routed `dolt sql` JSON result set renders integer columns as strings, and
the embedded driver must still return the same Python types as the local engine.

- The fixture starts the server through the product's `ServerDriver` (the only server lifecycle
  code) on a free loopback port; it is stopped via `ServerDriver.discard()`, and the session hook
  still runs `shutdown_all_servers()` as a backstop. It is not a hand-managed `dolt sql-server`
  process, so rule 7 below still holds.
- Lifecycle ordering matters since 0.8.44.1: the embedded driver holds one writable session for the
  ledger, so the test closes it (`driver.close()`) before the test-owned server starts, then opens a
  fresh embedded driver whose `dolt sql` session auto-routes through that server, and closes it
  again before `discard()`. A server cannot start on a directory an embedded session holds, and the
  real-world case is the opposite order anyway (the user's server is already alive when `tm` runs).
- Routing is **asserted, never assumed**: a raw `dolt sql -r json` run of an INTEGER column must
  return a string, otherwise the test fails (a skip is not allowed — it would hide a broken
  fixture). While the server is still starting, Dolt silently falls back to the local engine, so the
  guard is polled.
- Every case runs twice on the same data: before the server exists (local engine) and while routed,
  and the test compares values *and* Python types; after the fixture exits the server is gone and
  the reads are compared once more. Cases cover INT/BIGINT/`BIGINT UNSIGNED > 2**63`/tinyint/negative
  integers, numeric-looking TEXT, DECIMAL, `NULL` filling, empty results, `COUNT` aggregates,
  aliased expressions, `query_many`, `fill_columns=False`, pending-transaction previews, a branch
  session, the documented non-`SELECT`/probe-rejected residual, and the one-process invariant.
- Because the fixture owns the server and tears it down, a green run still leaves **zero** `dolt
  sql-server` children; the module is part of the ordinary full suite (no extra marker or lane).

## 4. Fixtures and layout

```
tests/conftest.py
  dolt_binary / dolt_env / dolt_run / dolt_conf   isolated real-Dolt environment
  isolated_env(root, home)                        DOLT_ROOT_PATH, HOME, XDG_*, GIT_*
  template_ledgers                                session LedgerTemplates (per worker)
  shared_builds / SharedBuilds                    expensive fixture builds once per session
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
- **Session templates are built once per pytest session and then copied.** All xdist workers share
  `<session basetemp>/ledger-templates/`; the first worker that needs a shape builds it under an
  exclusive `flock` and writes `<key>.ready`, the others wait and copy. `LedgerTemplates.ledger()`
  closes the `Ledger` `init_ledger` returns and (with the server backend) stops its cached server before
  publishing the path, so `install()` always copies a **closed** directory: no server is mutating
  it and no discovery file points at a live port.
- **Expensive module fixtures are built once per session, then copied per worker.** `SharedBuilds`
  uses the template protocol for any directory-shaped fixture under
  `<session basetemp>/shared-builds/`: the first caller takes an exclusive `flock`, builds into a
  temporary sibling, publishes the complete directory with one atomic `os.rename`, and only then
  writes `<key>.ready`; an interrupted publisher leaves no marker and the next caller rebuilds. The
  published directory is **closed** (no cached server, no discovery file pointing at a live port)
  and carries its original IDs and expected-value metadata as JSON *inside* the same directory, so
  payload and metadata always come from one build. Each worker `shutil.copytree`s the closed
  directory and reopens its own copy. The migration builders (`rebuilt_molbio_v1`,
  `rebuilt_spec4_boxes`) still run their pinned legacy code and original checks inside the session
  clock, and their original `pytest.skip` behaviour is preserved: every caller skips with the same
  message, and a skip is never cached.
- **Budget tests may build their fixture data through the server and then reopen the same ledger
  embedded.** Building rows is setup, not the read under test; the process counts are cleared after
  setup. `tests/test_list_round_trips.py`, `test_csv_round_trips.py` and
  `test_lineage_round_trips.py` follow this pattern: **one populated ledger per session** (not one
  per worker), published closed (the builder stops its cached server) and copied per worker, then
  reopened embedded for the read-only tests to share.
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
- Default addopts: `-ra -n 16 --dist worksteal`.

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
3. **Share expensive builds.** A module- or session-scoped fixture that costs more than ~5 s uses
   the `shared_builds` helper, which builds once per session and copies per worker. A plain
   module-scoped fixture is rebuilt once per xdist worker, and under `--dist worksteal` one module's
   tests can be spread over several workers.
4. **Justify real-Dolt cost.** One end-to-end flow per behaviour is enough; extend an existing flow
   instead of adding a second ledger. Share populated fixtures across read-only tests.
5. **Docs are executable.** `tests/test_docs_commands.py` runs the flows documented in the README,
   the command reference, the migration recipe and the schema skill; when a doc example changes, that
   test changes with it.
6. **Investigate before changing code.** Reproduce a failure from the actual logs and the
   slowest-25 report; distinguish a code defect from demonstrated resource contention before editing
   anything. **Never weaken a test to make a lane green:** adapt paths/flags (e.g. `-l PATH` instead
   of `--workspace`) and fixture names, keep the assertion, and do not re-add skips, add `xfail` or
   widen budgets/timeouts.
7. **Do not rely on `xdist_group`.** `pytest-xdist` ignores it under the configured
   `--dist worksteal` (only `loadgroup` understands grouping), so it claims an isolation that does
   not happen. Use the shared-build helper or an explicit fixture dependency instead.
8. **Never mock Dolt** and never hand-fake a database directory; copy a template, a fixture or run a
   real init. The server default still starts the real `dolt sql-server` — no MySQL mock, no faked
   rows.
9. **Database tests never manage a server by hand.** They use the driver (embedded or server, chosen
   by the marker/environment), never start or stop a `dolt sql-server` themselves, and never touch a
   server they do not own. A fixture that publishes a build publishes a **closed** directory only.
   The only server lifecycle code is `tetramark/driver/server.py`; tests assert its cleanup through
   `tests/test_server_driver.py`. The one exception is the routed-parity fixture described in §3: it
   needs a server that the *embedded* driver is not talking to, so it starts one through
   `ServerDriver` and stops it with `discard()` — still the product's lifecycle code, never a
   hand-spawned process.
10. **No workspace markers.** A test must not create `workspace.yaml`; the standalone-ledger guard
    test greps for it. Resolution tests use stub directories and belong to the fast lane.
11. **Isolate the environment.** Use the fixtures; never rely on the developer's Dolt/Git config or on
    `~/Documents/Lab`.
12. **Clean up.** Every opened `Ledger` is closed (the factory does it); no `dolt` child processes
    survive a green run. The pytest hooks kill cached servers; `scripts/test-lanes.sh` checks for
    leftovers.
11. **Test time-dependent harness behaviour through the isolated test clock.** The harness exposes
    one seam for that: `tests/conftest.py::_suite_clock`, a module attribute defaulting to
    `time.monotonic`. Replace **that attribute**, never the global `time.monotonic` or `time.sleep`
    — patching the globals reaches pytest, xdist, PyMySQL and the Dolt supervision code, which all
    read real time, and it turns a timing test into a race.
12. **Ship an ordering assertion with a meaningful negative control.** Every assertion about *when* a
    hook runs needs a counterpart that goes the other way (the clock moves before the guard → the
    run fails; after the guard → the run stays green). Without it a test can be red for a reason
    that has nothing to do with the behaviour it names.
13. **Do not use real sleeps for ordering assertions.** A `time.sleep(5)` in a session-finish hook
    makes the test cost five seconds of wall clock and lets a loaded machine turn it red; it also
    says nothing about ordering that an injected clock does not say better.

## 7. Release acceptance

Before a release run and record:

- `uv run pytest` — green, **inside the 300 s guard** (reference: the idle measurement in section 2),
  no leftover `dolt sql-server`;
- `uv run pytest -q -m fast` — green, inside the 30 s guard;
- `TETRAMARK_DRIVER=embedded uv run pytest` — green (the product's default backend, ~12 min);
- the heavy migration/lab subset is part of the full run; the 0.4.2 snapshot test skips cleanly when
  its directory is absent;
- `scripts/test-lanes.sh` (fast + full, with stats and the leftover check) — exit 0.
