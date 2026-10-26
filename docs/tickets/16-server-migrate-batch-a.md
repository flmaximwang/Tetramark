# 16 · Server-backed test harness: migrate batch A
Depends on: 15 · Spec: `docs/test-server-harness.md` §6

Goal: move four dolt-heavy test files from the embedded `ledger_factory` to
`server_ledger_factory` without changing a single assertion.

## Files (only these)

- `tests/test_objects.py` (26 tests)
- `tests/test_csv_io.py` (15)
- `tests/test_cli_objects.py` (6)
- `tests/test_read_side.py` (13)

## How

1. At the top of each file add the same one-line module fixture already used by
   `test_objects.py` today, but pointing at the new factory:

```python
@pytest.fixture
def ledger_factory(server_ledger_factory):
    return server_ledger_factory
```

2. Fix only **harness plumbing**, never assertions:
   - CLI invocations (`typer.testing.CliRunner` / `runner.invoke`) that rely on cwd discovery must
     be given `--workspace <ledger.workspace.data_dir>` (or `monkeypatch.setenv(
     "TETRAMARK_WORKSPACE", str(...))`). Prefer the explicit flag.
   - Tests that open a second ledger (`open_ledger(...)`, `ledger_factory` twice) work unchanged.
   - Anything calling `dolt_run(...)`/`subprocess` against the ledger directory or using
     `Workspace.start/stop` must stay embedded: leave that test on `embedded_ledger_factory`
     (request it by name directly instead of the module alias) and note it in the report.
3. If an assertion fails because of a backend-specific message, do **not** loosen it silently:
   - if the same product error is raised (Python-level), adapt the match to a `(?i)` pattern that
     still asserts the meaning;
   - otherwise revert that test to `embedded_ledger_factory` and list it.

## Constraints

- No `skip`/`xfail`, no deleting tests, no changes under `src/` (T15 already added what is needed).
- Do not touch `tests/conftest.py`; if a fixture gap blocks you, report it instead of editing.
- Do not touch other test files.
- Do not commit; leave the tree dirty.
- Sandbox: prepend `<ps-shim>` (a dir containing an executable `ps` shim) to `PATH` for pytest.

## Verify

```bash
PATH="<ps-shim>:$PATH" .venv/bin/python -m pytest tests/test_objects.py tests/test_csv_io.py \
  tests/test_cli_objects.py tests/test_read_side.py -q -n0
# plus twice more for the migrated files (flakiness check)
```

Report: per-file wall time with `--durations=0` before/after if you can measure before, number of
tests still on embedded factory and why, and any assertion that needed adapting.

## Result

Migrated `test_objects.py`, `test_csv_io.py`, `test_cli_objects.py`, `test_read_side.py` to
`server_ledger_factory` (60 passed, `-n auto`); 3× `-n0` green each. Two `test_csv_io` cases stayed
embedded because `ServerDriver` returns `datetime` objects that the test snapshot helper fed to
`json.dumps`; documented for a follow-up. No assertions weakened, no skips.
