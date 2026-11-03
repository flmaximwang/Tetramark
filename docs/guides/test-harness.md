# Test harness — embedded-only pytest fixtures

Status: **shipped in 0.5.0**. This page replaces the 0.2.0 *server-backed* harness design: P14
removed `ServerDriver`, the `tm server` commands and the per-worker `dolt sql-server`. The old
server-harness performance numbers are kept as historical context in §2 so they are not mistaken for
today's target.

## 1. Cost model

Everything runs against the real `dolt` binary — no mocks — and every SQL statement is one `dolt sql`
subprocess in the ledger directory. On an idle machine that is **60–100 ms per statement** (0.2–0.4 s
under ten-worker load), because Dolt starts a process and opens the database for each batch.

The consequence for tests is simple: **never run a full `tm init` per test**. A standard init executes
100+ statements (roughly 10 s); a test that does that for every case makes the suite unusable.

The harness therefore builds each **standard ledger shape once per pytest session per xdist worker**
and hands out directory copies:

- `LedgerTemplates` lazily runs one real `init_ledger` for each shape (`tests/data/presets/base`,
  `tests/data/presets/full`, the repository's `presets/base.yaml`);
- `embedded_ledger_factory` / `ledger_factory` `shutil.copytree` the closed ledger into
  `tmp_path/<name>` (≈0.05 s) and rewrite only `name:` in `ledger.yaml`;
- a factory call that asks for a non-standard shape (another `id_prefix`, `author`, preset list,
  `statuses`) keeps the real init path — a template must be exactly what init would have produced;
- tests whose subject *is* what init generates pass `real_init=True`;
- historical shapes the current tool can no longer create (`tests/data/ledgers/molbio-v1`, …) are
  checked in as real Dolt databases and copied; their builders are tested against the fixture itself.

The environment is isolated the same way: `DOLT_ROOT_PATH`, `HOME`, `XDG_CONFIG_HOME` and the
`GIT_CONFIG_*` variables point into `tmp_path`, so the developer's global Dolt/Git configuration is
never read or written. No ledger-selection environment variable exists (`TETRAMARK_LEDGER` /
`TETRAMARK_WORKSPACE` are gone); `$TETRAMARK_PRESETS_DIR` is popped unless a test sets it explicitly.

There is no server, port, pid or lifecycle to manage in 0.5.0, so there are no leaked
`dolt sql-server` processes and no `pgrep` checks. Write-lock coverage uses `fcntl.flock` directly
(same process and a subprocess waiter), which needs no server either.

## 2. The lanes and their budgets

Two lanes, plus a release acceptance run:

| Lane | Command | Budget | What belongs there |
|---|---|---|---|
| **fast** | `pytest -q -m fast` | **seconds** — currently ~2–3 s on an idle machine; keep it that way | Dolt-free unit tests: resolution, YAML/schema model, id strategies, renderers, CLI help/panels, error messages |
| **full** | `pytest` (addopts: `-n auto --dist loadfile`) | measured 0.5.0 baseline **+20 %** on the same machine with `-n auto` | everything, including every real-Dolt integration flow |
| **release** | full lane + the heavy migration/lab subset | green, no new failures | `tests/test_migrate.py`, the read-only 0.4.2 lab-ledger check when `.dsh/tm-l8g/lab-snapshot` exists, docs flows, and `scripts/test-lanes.sh` |

Rules:

- **Never put a database test on the fast lane.** If it opens a `Ledger`, calls `init_ledger`/`open`,
  or shells out to `dolt`, it is not `fast`. A fast test that starts costing seconds is a bug.
- **`-n auto`, not `-n 4`.** The suite is dominated by independent directory copies and per-worker
  subprocesses; on the developer machine `-n 4` is measurably **slower** than `-n auto`, so the
  addopts default stays `-n auto --dist loadfile`. Measure on the target machine rather than copying
  a worker count from another one.
- **Full-lane budget: ≤ measured baseline +20 %.** The 0.5.0 baseline is recorded in ticket .11's
  report (`tm-l8g.11.md`: tests/skips/wall time with `-n auto`) and is filled into
  `scripts/test-lanes.sh` as `FULL_LANE_BASELINE_SECONDS`. New real-Dolt tests are allowed only if
  they reuse the session templates and their cost is justified in the ticket report; prefer extending
  an existing flow over adding another init.
- **`scripts/test-lanes.sh` is the enforcement point.** The repository has no CI, so this script —
  run before a merge and before a release — is what turns the budgets into a failing exit code. It
  runs both lanes with `--durations`, prints their wall times and fails when the full lane exceeds
  the baseline by more than 20 % (override the baseline for a different machine with
  `FULL_LANE_BASELINE_SECONDS`).

The **server-harness numbers are historical**: the 758 s → 154.83 s result and the “≤180 s” goal
belong to the 0.2.0 server design that P14 removed. They are context for how the suite used to be
measured, not the 0.5.0 target; do not quote them as the current budget.

## 3. Fixtures and layout

```
tests/conftest.py
  dolt_binary / dolt_env / dolt_run / dolt_conf   isolated real-Dolt environment
  isolated_env(root, home)                        DOLT_ROOT_PATH, HOME, XDG_*, GIT_*
  template_ledgers                                session LedgerTemplates (per worker)
  embedded_ledger_factory / ledger_factory        copy a session template (or real init)
  make_ledger / dolt_repo                         bare Dolt databases for adopt/raw tests
tests/data/presets/                               preset fixtures used by the templates
tests/data/ledgers/                               checked-in historical databases + builders
```

- Use `ledger_factory([...])` for "a normal ledger of this shape"; it returns an opened `Ledger`.
- Use `dolt_repo` / `make_ledger` when the test is about a bare database or `tm adopt`.
- Pass `-l/--ledger <ledger.path>` (or `monkeypatch.chdir` into the ledger) in CLI tests; there is no
  `--workspace` and no `TETRAMARK_WORKSPACE`, and a test must never create a `workspace.yaml`.
- Isolated Dolt config: a test that shells out directly uses `dolt_run`/`dolt_env`, never the
  developer's global config. The lab directory `~/Documents/Lab/Dolt` is only ever **read** in the
  optional read-only snapshot test (copy it under `tmp_path` first if a test needs to write).

## 4. Markers and selection

- `fast` — Dolt-free; the only mark the fast lane runs. The module owns it with
  `pytestmark = pytest.mark.fast` (or per-test decorators) and the module must not import/execute
  Dolt work at collection time.
- `embedded` — the test deliberately exercises the embedded driver and needs the real `dolt` binary.
  In 0.5.0 that is every real-database test; `-m embedded` selects the integration set, while the
  unmarked remainder is likewise embedded whenever it touches a ledger.
- Default addopts: `-ra -n auto --dist loadfile`.

Common commands:

```sh
uv run pytest -q -m fast                # fast lane (seconds)
uv run pytest                           # full lane (-n auto from addopts)
uv run pytest -n0 tests/test_status.py  # one file, no xdist
uv run pytest -q -m embedded            # real-Dolt integration set
uv run pytest -k "backup or lock"       # focused by keyword
```

In a restricted sandbox without `uv`, the repository's `.dsh/tm-l8g/t.sh <worktree> <pytest args>` and
`tm.sh` wrappers run the same commands against a worktree (`PYTHONPATH=<worktree>/src`). They are a
measurement-environment helper, not a product requirement.

## 5. Rules for adding tests

1. **Keep the fast lane fast.** New pure-Python tests are marked fast; a test that needs Dolt is not.
2. **Reuse the session templates.** A test that just needs a ledger uses `ledger_factory`; only a test
   about init/adopt/preset generation pays for a real init (`real_init=True`).
3. **Justify real-Dolt cost.** One end-to-end flow per behaviour is enough; extend an existing flow
   instead of adding a second ledger. Put the cost in the ticket report when adding a new module.
4. **Docs are executable.** `tests/test_docs_commands.py` runs the flows documented in the README,
   the command reference, the migration recipe and the schema skill; when a doc example changes, that
   test changes with it. Documentation and its test stay in one change.
5. **Never weaken a test to make a lane green.** Adapt paths/flags (e.g. `-l PATH` instead of
   `--workspace`) and fixture names; keep the assertion.
6. **Never mock Dolt** and never hand-fake a database directory; copy a template, a fixture or run a
   real init.
7. **No server, no ports.** Do not start/stop `dolt sql-server` in tests; do not add a pid/port
   fixture. The write lock is tested with `flock` directly.
8. **No workspace markers.** A test must not create `workspace.yaml`; the standalone-ledger guard test
   greps for it. Resolution tests use stub directories and belong to the fast lane.
9. **Isolate the environment.** Use the fixtures; never rely on the developer's Dolt/Git config or on
   `~/Documents/Lab`.
10. **Clean up.** Every opened `Ledger` is closed (the factory does it); no `dolt` child processes or
    lock files survive a green run (lock files are left in place inside `tmp_path` on purpose).

## 6. Release acceptance

Before a release (ticket .13/.14) run and record:

- `uv run pytest -q -m fast` — green, seconds;
- `uv run pytest` — green, with test/skip counts and wall time (the 0.5.0 baseline lives in the .11
  report);
- the heavy migration/lab subset — green: `tests/test_migrate.py` and the read-only 0.4.2 snapshot
  test (skipped cleanly when the snapshot directory is absent);
- `scripts/test-lanes.sh` — fast and full lanes inside budget (exit 0).
