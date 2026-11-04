# 9. Repository layout

```
pyproject.toml            # uv/hatchling; deps: typer, pyyaml, pydantic>=2, rich
presets/                  # parts (base, nucleic_acids, bacteria, proteins, cell_culture, immuno,
                          # viral_vectors, biosamples, drosophila, c_elegans, zebrafish, mouse,
                          # library) and bundles (molbio, cellbio, angenet) + README.md
                          # (part/bundle table, include graph, recipes); tm init copies them.
                          # The wheel ships this directory as tetramark/presets/ (package data).
src/tetramark/
  driver/                 # base.py (Driver ABC), embedded.py, resolution.py (LedgerRef/discovery)
  schema/                 # model.py (pydantic), loader.py (presets/include/mixins/local dirs),
                          # ddl.py, diff.py, tests_gen.py
  core/                   # ids.py, ledger.py (open/init/adopt), objects.py, events.py,
                          # writelock.py (per-ledger flock, P11)
  ops/                    # find.py, render.py (grid), io_csv.py, remote.py, portability.py, ...
  cli/                    # app.py (Typer root) + one module per command group
scripts/
  test-lanes.sh           # fast lane + full lane with --durations and the wall-time budget
  release_check.py        # do pyproject.toml, __init__.__version__ and uv.lock agree? --json for CI
.github/workflows/
  release.yml             # a version bump landing on main -> annotated tag + GitHub release
skills/tetramark/SKILL.md (+ references/)
skills/tetramark-schema/SKILL.md (+ references/)
docs/spec/                # this specification, one file per chapter
docs/guides/              # how-to guides (migration, upgrade, releasing, test harness)
docs/releases/            # release notes, v<version>.md: title + body of the GitHub release
docs/records/             # dated delivery records (real migrations, wave deliveries)
docs/issues/              # open design for planned work (not yet implemented)
migration/                # hand-run upgrade steps: a migrate.py + README, or a README-only manual step
tests/                    # pytest; fixtures create real dolt repos in tmp_path
```

Testing: pytest against the real `dolt` binary (no mocks of Dolt), embedded driver only. A ledger
fixture builds each standard shape once per session and hands out directory copies; tests marked
`fast` are Dolt-free and form the seconds-long inner loop. `uv run pytest` must pass on every merge
and before a release; see [the test-harness guide](../guides/test-harness.md), and run the measured
lanes with [`scripts/test-lanes.sh`](../../scripts/test-lanes.sh).

Releasing: the version in `pyproject.toml` is the trigger, not a tag. A merge that changes it makes
[`release.yml`](../../.github/workflows/release.yml) tag the merge commit and publish the release; see
[the releasing guide](../guides/releasing.md). Manual tags are not part of the process.
