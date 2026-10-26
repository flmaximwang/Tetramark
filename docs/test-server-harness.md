# Test Harness v2 — Server-Backed Fixtures (route C)

Status: **implemented (tickets 15–19)** · Owner: Lead · Depends on: current test suite (`dolt 2.3.5`) ·
Related measurement report: `docs/test-runtime-optimization.md` (route C, §2.4).

## 1. Problem

The suite spends its wall time spawning `dolt` processes: one `dolt sql` per statement
(60–100 ms idle, 0.2–0.4 s under 10-worker load). A full run is **758 s** and records about
**13,000 dolt spawns**; 199 of 605 tests touch dolt and consume >99 % of the time.

A long-lived `dolt sql-server` changes the unit cost: one query is **0.40 ms** (measured,
100 queries), a connection is 56 ms, and server start is 464 ms. Embedded error paths are
still exercised by dedicated tests. The goal of this spec is to move the *bulk* of the
dolt-heavy tests onto a per-worker server while keeping the suite semantics (isolation,
assertions, embedded coverage) intact.

## 2. Goals / non-goals

Goals

1. Full suite wall time **≤ 180 s** (stretch **≤ 120 s**) on the M1 Max developer machine with
   `-n auto`, from 758 s; 605 tests still pass (or the documented count after intentional merges).
2. Isolation unchanged: every test behaves as if it had a private ledger; no cross-test state.
3. No product behaviour change: `tm` CLI and embedded driver semantics stay identical;
   only the test harness and small, additive library helpers change.
4. Embedded coverage retained: driver contract, workspace/server lifecycle, docs quickstart,
   init-semantics and raw-CLI tests still run against the embedded driver / real CLI.
5. No leaked `dolt sql-server` processes; no flaky failures across 3 consecutive full runs.

Non-goals

- Removing `EmbeddedDriver` or changing its public contract.
- Reworking the CLI UX or output formats.
- CI configuration, Windows support, or dolt version bumps.
- Product performance work from `docs/test-runtime-optimization.md` P0-1/P0-3/P1-* (they are
  complementary; this spec only changes the test harness + the small helper it needs).

## 3. Measured inputs

| Fact | Value | Source |
|---|---|---|
| Server query / connect / start | 0.40 ms / 56 ms / 464 ms | `perf-notes/prod-hotpath.md` §8 |
| `init_ledger(base/full)` embedded | 39 / 65 dolt spawns, 4.4–7.4 s | harness/arch audits |
| Embedded read query | 1–2 spawns; 60–100 ms idle | `embedded.py:81-207` |
| Suite | 605 tests / 758 s / ~13,000 spawns | `/tmp/baseline_full.log` |
| Pure-Python 4 files | 341 tests, ~1.6 s | `perf-notes/pytest-arch.md` §3.1 |

## 4. Architecture

```
pytest worker (xdist gw0..gwN)
└── session fixture `server_workspace`            (one per worker)
    ├── data_dir = tmp_path_factory.mktemp("server-ws-gw0")
    ├── env = isolated_env(...)                   (DOLT_ROOT_PATH, HOME, XDG_*, GIT_*)
    ├── bootstrap: one bare `dolt init` ledger    (Workspace.start needs ≥1 ledger)
    ├── `dolt sql-server` on a free port          (Workspace.create + start(daemon=True))
    ├── drop stale `t_<worker>_*` databases       (left over by a crashed run)
    └── per-test `server_ledger_factory` (function scope)
        ├── CREATE DATABASE `t_<gw>_<n>`          (admin pymysql connection)
        ├── write `<data_dir>/<db>/ledger.yaml`   (same YAML as init_ledger)
        ├── ServerDriver: DDL + ledger_meta + dolt_tests + DOLT_COMMIT
        ├── Ledger(..., driver=ServerDriver)      (caller uses it like any Ledger)
        └── teardown: close driver + DROP DATABASE
```

Key decisions

- **One server per xdist worker**, not per test: 464 ms start + 56 ms connect is amortised over
  the worker's whole slice. Workers are isolated by their own `data_dir` and port.
- **Database per test**, not directory copy: `CREATE DATABASE` is ~1 round trip and `DROP DATABASE`
  gives clean isolation without copying 170 KB+ of noms files.
- **Bootstrap ledger** exists only so `Workspace.start()` (which refuses an empty data dir) can
  start the server; tests never see it.
- **Embedded templates are not needed** in v1: 34–67 DDL statements over the server cost
  ~15–20 ms each, i.e. ~0.5–1.0 s per fresh ledger, versus 4.4–7.4 s embedded. A later ticket may
  add `DOLT_CLONE`/template databases if the remaining cost matters.
- **Error-path tests stay embedded**: pymysql surfaces different messages and class names than the
  embedded CLI output, so tests that assert raw Dolt error text stay on the embedded driver
  (see §6).

## 5. API changes (additive only)

### 5.1 `tetramark.core.ledger.init_ledger_on_server(...)`

```python
def init_ledger_on_server(
    workspace: Workspace | str | Path,
    name: str,
    presets: Sequence[str | Path] | str | Path | None = None,
    *,
    id_prefix: str | None = None,
    author: str | None = None,
    statuses: Sequence[str] | None = None,
) -> Ledger: ...
```

- Shares `_prepare_init` with `init_ledger` (same YAML, schema, statements, author handling).
- Requires a running server (`workspace.running_pid()` + port reachable), else `UnreachableError`.
- Creates the database through an **admin** PyMySQL connection (no database selected),
  writes `ledger.yaml`, applies statements + `_write_meta` + `install_tests` inside
  `ServerDriver.transaction()`, commits `init(ledger): <name>`, and returns the opened `Ledger`.
- On any failure: `DROP DATABASE` + remove the directory; never leave a half-initialised ledger.
- `init_ledger` (embedded) remains unchanged and becomes the reference implementation;
  both paths must produce the same schema/meta commit. A shared regression test asserts this.

### 5.2 `tetramark.driver.workspace` helpers (optional, if useful)

- Small read-only helpers to expose `host`, `port`, `data_dir` for the admin connection are already
  available via `Workspace.config`; do not add mutation APIs.

### 5.3 Test fixtures (`tests/conftest.py`)

- `server_workspace` (session): as §4; tears down with `workspace.stop()` and asserts the server
  process is gone.
- `server_ledger_factory` (function): factory with the **same signature and return type** as
  `ledger_factory(presets=None, *, name=None, id_prefix="77", author=FACTORY_AUTHOR, statuses=None)`.
  Calls are recorded and every created database is dropped in teardown. `name` may be passed for
  tests that assert on it; default unique `t_<gw>_<n>`.
- `embedded_ledger_factory` (function): the current implementation of `ledger_factory`, kept under
  a clearly named fixture for tests that must stay embedded.
- Migration stays a one-line module fixture, exactly like the existing pattern:

```python
@pytest.fixture
def ledger_factory(server_ledger_factory):
    return server_ledger_factory
```

- A helper `_admin_connection(workspace)` (tests-only) may wrap `pymysql.connect`; it must not
  leak connections.

## 6. Migration matrix

| File | Tests | Target | Notes |
|---|---:|---|---|
| `test_objects.py` | 26 | **server** | direct ledger API; already on template copy |
| `test_csv_io.py` | 15 | **server** | direct API + some CLI (`--workspace` needed) |
| `test_cli_objects.py` | 6 | **server** | CliRunner; pass `--workspace` explicitly |
| `test_read_side.py` | 13 | **server** | direct API + CLI history/find |
| `test_validation.py` | 18 | **server** | raw SQL via `ledger.driver` works on server too |
| `test_schema_diff.py` | 31 | **server** | `plan`/`apply` via server; ledger.path edits still work |
| `test_ledger_init.py` | 31 | mixed | init/open **semantics** tests stay embedded; write/open smoke cases may move |
| `test_docs_commands.py` | 5 | mixed | quickstart/server flow stays real-server; other flows may start a server for their workspace |
| `test_remote_backup.py` | 13 | mostly embedded | `dolt push/clone` needs real directories; migrate only pure API cases if safe |
| `test_presets.py` | 5 | embedded | asserts every bundled preset can init via `init_ledger` |
| `test_ddl.py` | 16 | embedded | uses raw `dolt` CLI + dolt-specific error text |
| `test_driver_contract.py` | 27 | embedded + server | already parametrised; keep both |
| `test_workspace.py`, `test_cli_workspace.py` | 35 | embedded | server lifecycle itself |
| `test_smoke.py` | 8 | embedded | process-level CLI smoke |

Rules

1. Never delete, rename or weaken a test to make migration easier. No `skip`/`xfail` without Lead
   approval; no assertion loosening beyond adapting backend-specific message matching.
2. A migrated file must still pass **all** its original assertions; if a test asserts embedded-only
   behaviour (raw CLI output, file layout after `dolt init`, `pgrep`-based lifecycle), it stays
   embedded and is listed in the PR description.
3. If a CLI call inside a test needs a workspace, pass `--workspace <session ws>` or set
   `TETRAMARK_WORKSPACE`; do not rely on `monkeypatch.chdir` into a per-test dir.
4. Tests that mutate `ledger.yaml`/`presets` on disk may run on the server path: the files live in
   `data_dir/<db>/`, exactly as before.
5. Anything that runs `dolt sql` embedded against a directory that the worker's server has open is
   forbidden (Dolt does not support that); such tests stay embedded.

## 7. Isolation, cleanup, failure modes

- Per-worker data dir and port; unique database names; `DROP DATABASE` in teardown.
- Session start: drop `t_<gw>_*` leftovers (best effort; one `SHOW DATABASES` + drops).
- Session end: stop server, wait for exit (existing `Workspace.stop()` semantics). **Known fix
  required** (found during T15): when `start()`/`stop()` run in the same process the exited server
  stays a zombie and `stop()` waits the full `READY_TIMEOUT` (12 s per worker); `_terminate` must
  call `os.waitpid(pid, os.WNOHANG)` to reap the child. Regression test: `stop()` < 5 s.
- If the server dies: all subsequent tests in that worker fail with `UnreachableError`; the
  session fixture must attempt one restart per worker (bounded, no infinite loops).
- The existing `_reap_test_servers` watchdog stays; it must not kill the session server while the
  worker is alive.
- Parallel runs (`-n auto`) keep one server per worker; `-n0` gets one server total.
- `pytest -x`/killed runs leave a `dolt sql-server`: the existing reaper covers tmp roots.

## 8. Acceptance criteria (per ticket and final)

| # | Check | Threshold |
|---|---|---|
| A1 | Full suite, `-n auto --dist load` (or loadfile if still default) | 605 passed, 0 failed |
| A2 | Wall time, same machine as baseline | ≤ 240 s after T16–T18; **≤ 180 s** after T19; stretch ≤ 120 s |
| A3 | Three consecutive full runs | green, no flake |
| A4 | Embedded path | `test_driver_contract.py` both params, `test_workspace.py`, `test_docs_commands.py`, `test_ledger_init.py` init cases pass |
| A5 | Process hygiene | no `dolt sql-server` after run; `pgrep -f pytest-of` empty |
| A6 | Equivalence | shared regression test: embedded `init_ledger` and `init_ledger_on_server` produce identical schema/fingerprint/`ledger_meta`/commit count |
| A7 | Developer env | `uv run pytest` (with the harness's ps shim only in this sandbox) works; no new required env vars |

Measurements to record in the final report: wall time, user/sys CPU, dolt spawn count, per-file
table before/after, remaining embedded tests.

## 9. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Error-message differences (pymysql vs CLI) | keep driver-error tests embedded; adapt message regex only where the underlying error is identical |
| `Workspace.start` needs ≥1 ledger | bootstrap bare ledger; tests never depend on it |
| Server crash mid-run | one bounded restart per worker; fail fast otherwise |
| `DROP DATABASE` slows teardown | measure; if >5 ms/test, batch drops at session end with `t_<gw>_*` sweep |
| Shared `default_ledger` in `workspace.yaml` | session workspace has no default ledger; tests pass `--ledger`/`--workspace` |
| CLI workspace discovery | pass `--workspace` explicitly or set `TETRAMARK_WORKSPACE` |
| `close()` semantics | `ServerDriver.close()` exists; factory must close drivers, and teardown must not hang on server shutdown while drivers are open |
| Sandbox lacks `ps` | tests harness must only rely on `Workspace.stop()`; the temporary `ps` shim is a measurement-environment workaround, not a product requirement |
| Windows/portability | out of scope; use `free_port()` already in conftest |

## 9b. Measured result (implemented)

Tickets 15–19 landed on the `optimize-tests` worktree. Same machine, same dolt 2.3.5:

| Run | Config | Result | Wall | CPU (user+sys) |
|---|---|---|---:|---:|
| baseline (before) | `-n auto --dist loadfile` | 605 passed | **758.27 s** | 1369 s |
| after, default | `-n auto --dist loadfile` | **614 passed** | **154.83 s (-79.6 %)** | 260 s |
| after, cheap change | `-n auto --dist load` | 614 passed | **141.50 s (-81.3 %)** | 271 s |

- Worker time dropped from 4,323 s to ~894 s; the new total includes 9 new harness tests (605→614).
- Embedded residual (still on `embedded_ledger_factory`, ~2 min of worker time): `test_ddl` (50 s),
  `test_presets` (106 s), `test_remote_backup` (144 s), plus the embedded halves of
  `test_ledger_init`/`test_driver_contract`/`test_workspace`/`test_cli_workspace`/`test_smoke`.
- `test_schema_diff.py` dropped from 757 s worker time to ~49 s; `test_validation` 500→41 s;
  `test_objects` 491→89 s; `test_docs_commands` 565→95 s (real server per flow).
- Two tests in batch A stayed embedded because `ServerDriver` returns `datetime` objects where the
  test helper serialises rows with `json.dumps`; a follow-up can normalise that in `ServerDriver`.
- Optional next wins: template databases (`DOLT_CLONE`) to remove the per-test DDL (~0.5–1 s each),
  migrating the remaining `test_remote_backup`/`test_presets` cases, and `-n 8` on 8 P-cores.

## 10. Rollout / ticket wave

| Ticket | Scope | Depends | Parallel-safe |
|---|---|---|---|
| T15 | `init_ledger_on_server` + `server_workspace`/`server_ledger_factory` fixtures + equivalence test | — | no (touches `tests/conftest.py`, `src/tetramark/core/ledger.py`) |
| T16 | migrate `test_objects`, `test_csv_io`, `test_cli_objects`, `test_read_side` | T15 | yes (own files) |
| T17 | migrate `test_validation`, mixed `test_ledger_init` cases, `test_docs_commands` server flow | T15 | yes |
| T18 | migrate `test_schema_diff`; split heavy test files if ticket says so | T15 | yes |
| T19 | acceptance: full-suite runs ×3, spawn count, markers (`embedded`), docs/README, final report; optional `-n` tuning | T15–T18 | no (Lead) |

Files are disjoint across T16–T18 except `tests/conftest.py` (T15 only) and final docs (T19).
Merging: cherry-pick/apply each ticket's diff on the integration branch, then run T19.
