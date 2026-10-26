# 17 · Server-backed test harness: migrate batch B (validation, ledger_init, docs)
Depends on: 15 · Spec: `docs/test-server-harness.md` §6

Goal: migrate the semantically sensitive files, keeping every original assertion.

## Files (only these)

- `tests/test_validation.py` (18 tests) — **all** migrate: it uses product APIs + raw
  `ledger.driver.execute` SQL; both work on `ServerDriver`.
- `tests/test_ledger_init.py` (31) — **partial**:
  - keep embedded (init/open semantics, fingerprint diagnostics, failure cleanup, CLI init):
    the tests that create ledgers specifically to assert `init_ledger`/`open` behaviour;
  - migrate the write/open smoke cases that only need a working ledger (they use
    `ledger_factory` but assert writes/commits/read paths).
  - Implement by requesting `server_ledger_factory` directly in the converted tests (name the
    fixture explicitly) and keep `ledger_factory` for the rest; do not add a module-wide alias.
- `tests/test_docs_commands.py` (5) — best effort, **stay green or revert the file**:
  - the README quickstart test keeps its real server + CLI flow unchanged;
  - for the other flows, start a real `dolt sql-server` for the test workspace (after
    `tm init`, via `tm server start -d` or `Workspace.start`), so every documented command still
    actually executes, now through `ServerDriver`; keep all assertions and command coverage;
  - if any documented assertion becomes impossible, revert this file entirely and document why
    (the Lead accepts leaving `test_docs_commands.py` embedded).

## Rules

- Never weaken or delete an assertion; no skips/xfails.
- No changes under `src/`; no edits to `tests/conftest.py` or other test files.
- CLI calls in converted tests: pass `--workspace` explicitly (or set `TETRAMARK_WORKSPACE`).
- Do not commit; leave the tree dirty.
- Sandbox: prepend `<ps-shim>` to `PATH` for pytest.

## Verify

```bash
PATH="<ps-shim>:$PATH" .venv/bin/python -m pytest tests/test_validation.py tests/test_ledger_init.py \
  tests/test_docs_commands.py -q -n0
# run the file(s) 3 times if any server test is involved
```

Report: embedded-vs-server split for `test_ledger_init` (node ids), whether docs migrated, per-file
wall time, and any assertion that needed a backend-neutral message pattern.

## Result

`test_validation.py` fully server-backed (18.2 s vs 500 s worker before); `test_ledger_init.py` split:
write/open cases on the server (88.9 s total file), init/open-semantics cases stay embedded;
`test_docs_commands.py` flows now start a real `dolt sql-server` and restart it after `tm init`
creates a new ledger (52.9 s vs 565 s). 56 passed, 3× `-n0` + `-n auto` green, no skips.
