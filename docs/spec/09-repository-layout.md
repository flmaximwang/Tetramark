# 9. Repository layout

```
pyproject.toml            # uv; deps: typer, pyyaml, pydantic>=2, pymysql, rich
presets/                  # parts (base, nucleic_acids, bacteria, proteins, cell_culture, immuno,
                          # viral_vectors, biosamples, drosophila, c_elegans, zebrafish, mouse,
                          # library) and bundles (molbio, cellbio, angenet) + README.md
                          # (part/bundle table, include graph, recipes); tm init copies them
src/tetramark/
  driver/                 # base.py, server.py, embedded.py, workspace.py (server start/stop)
  schema/                 # model.py (pydantic), loader.py (presets/include/mixins), ddl.py, diff.py, tests_gen.py
  core/                   # ids.py, ledger.py (open/init), objects.py (entity/stock/aliquot/box ops), events.py
  ops/                    # find.py, render.py (grid), io_csv.py, remote.py
  cli/                    # app.py (Typer root) + one module per command group
skills/tetramark/SKILL.md (+ references/)
skills/tetramark-schema/SKILL.md (+ references/)
docs/spec/                # this specification, one file per chapter
docs/guides/              # how-to guides (migration, test harness)
docs/records/             # dated delivery records (real migrations, wave deliveries)
docs/issues/              # open design for planned work (not yet implemented)
migration/                # hand-run upgrade steps (scripts + their READMEs)
tests/                    # pytest; fixtures create real dolt repos in tmp_path
```

Testing: pytest against real `dolt` (no mocks of Dolt). Fixture `ledger_factory(presets=[...])` makes a
temp workspace + ledger via EmbeddedDriver; a session-scoped optional fixture runs a sql-server on a
random free port for ServerDriver tests. `uv run pytest` must pass on every merge and before a release;
see [the test-harness guide](../guides/test-harness.md).
