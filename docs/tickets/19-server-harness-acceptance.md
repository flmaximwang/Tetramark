# 19 · Server-backed test harness: acceptance, markers, docs
Depends on: 15, 16, 17, 18 · Spec: `docs/test-server-harness.md` §6–§8

Goal: prove the migration on the integrated branch, document it, and record the measured result.

## Scope

1. **Markers / registry** (`pyproject.toml`, tests as needed)
   - Register `embedded` marker: "test must run against the embedded driver (no server)".
   - Mark the tests/files that intentionally stay embedded (driver contract embedded param,
     workspace/server lifecycle, docs quickstart, init-semantics, raw `dolt` CLI, smoke).
   - Keep existing `server` marker semantics; `-m "embedded"` must run the embedded coverage set and
     `-m "server"` the server-lifecycle set.
2. **Full-suite acceptance** (on the integrated branch, sandbox shim in `PATH`):
   - three consecutive `pytest -q -n auto --dist load` runs → all green;
   - record wall time, `user/sys`, and no leaked `dolt sql-server` after each run;
   - target **≤180 s** (baseline 758 s); if >180 s, report the residual hotspots per file and
     propose follow-ups (do not weaken tests);
   - record the number of embedded tests remaining and their share of the wall time.
3. **Spawn accounting**
   - Add a tiny test-only plugin (e.g. `tests/_spawn_count.py`, enabled by env var
     `TM_COUNT_SPAWNS=1`) that wraps `subprocess.run`/`Popen` and counts/Records child `dolt`
     invocations per nodeid; run the full suite once with it and report total spawns before/after
     (baseline: ~13,000). Do **not** use a PATH wrapper (it breaks `Workspace.start` pid semantics).
4. **Docs**
   - `README.md`: add a "Testing" paragraph: default `uv run pytest`, the two fixture families
     (`server_ledger_factory` vs `embedded_ledger_factory`), marker layers, and that the embedded
     driver is still covered.
   - `docs/test-runtime-optimization.md`: fill route C with the measured before/after and the
     remaining embedded hotspots; mark it implemented.
   - `docs/tickets/README.md`: mark this wave done.
5. Optional `-n` check: compare `-n auto` vs `-n 8` on the integrated suite (one run each) and put
   the recommendation in the final report; do not change `addopts` without data.

## Constraints

- Do not change test assertions or remove tests; only markers/docs/plugin.
- Do not commit; leave the tree dirty.
- Sandbox: prepend `<ps-shim>` to `PATH` for pytest.

## Acceptance

See spec §8 A1–A7. Report: three run summaries, before/after table (wall, CPU, spawns, per-file
top-10), embedded-residual list, marker commands, and any remaining flaky/slow test.

## Result

- Full suite: **614 passed**, `--dist loadfile` **154.83 s**, `--dist load` **141.50 s**
  (baseline 758.27 s / 605 passed) — both under the 180 s target.
- CPU: 1,369 s → 260 s; worker time 4,323 s → ~894 s; no leaked `dolt sql-server`.
- `embedded` marker registered; `-m embedded` runs 89 tests, `-m server` 20; README has a Testing
  section and the spec/report record the measured result. Two consecutive full runs green; optional
  follow-ups (template DB clone, migrate the two datetime cases, `-n 8`) are listed in the spec §9b.
