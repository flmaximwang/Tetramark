# 18 · Server-backed test harness: migrate schema plan/apply
Depends on: 15 · Spec: `docs/test-server-harness.md` §6

Goal: move `tests/test_schema_diff.py` (31 tests, 757 s worker time under `--dist loadfile`,
2,994 dolt spawns, the single critical-path file) onto the server harness.

## File (only this one)

- `tests/test_schema_diff.py`

## How

1. Module-level fixture replacement:

```python
@pytest.fixture
def ledger_factory(server_ledger_factory):
    return server_ledger_factory
```

2. Keep the whole test logic intact: `plan()`, `apply()`, `reload_schema()`, `dirty()`,
   `commit_count()`, `PresetWriter`, every assertion. These all operate on the `Ledger`/`Driver`
   interfaces and must keep passing on `ServerDriver`.
3. CLI tests (`test_cli_schema_plan_text_and_json`, `test_cli_schema_apply_*`) — pass
   `--workspace <data_dir>` explicitly; they must still exercise the real CLI.
4. If a specific test cannot work through the server (e.g. it inspects embedded-only error text or
   file layout), keep **that test** on `embedded_ledger_factory` by requesting it directly and list
   it in the report. Do not weaken assertions, do not skip.
5. Report any `schema/diff.py` bug the server backend exposes (do not patch `src/` in this ticket;
   escalate to the Lead with a minimal reproduction).

## Constraints

- Only `tests/test_schema_diff.py` may change. No `src/`, no `conftest.py`, no other tests.
- No `skip`/`xfail`, no deleted tests, no loosened assertions.
- Do not commit; leave the tree dirty.
- Sandbox: prepend `<ps-shim>` to `PATH` for pytest.

## Verify

```bash
PATH="<ps-shim>:$PATH" .venv/bin/python -m pytest tests/test_schema_diff.py -q -n0 --durations=0
# run twice more; also `-n 4` if the machine allows
```

Report: wall time before (757 s worker / ~12 min when this file is alone on a worker) vs after,
remaining embedded tests, list of `plan()`/`apply()` behaviours verified on the server backend, and
whether splitting this file further is still needed (expected: no, once it drops below ~60 s).

## Result

`tests/test_schema_diff.py` uses `server_ledger_factory`; 31 passed, **19.9 s** (was 757 s worker
time), 3× `-n0` + `-n 4` green. No embedded exceptions needed; plan/apply/reload/PresetWriter
behave identically on `ServerDriver`. No further file splitting needed.
