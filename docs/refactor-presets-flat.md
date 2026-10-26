# Refactor · flatten the bundled presets

Status: proposed  ·  Scope: `src/tetramark/presets/`, loader, docs, tests

## 1. Why

The bundled presets are shipped as one directory per preset, each holding exactly one file named
`preset.yaml`:

```
src/tetramark/presets/
  __init__.py
  base/preset.yaml
  library/preset.yaml
  molbio/preset.yaml
```

The extra directory level carries no information: nothing else lives in those directories (no
`kinds/`, no `rules/`), the directory name and the `preset:` key always agree, and the path is only
ever resolved by preset name. It makes the package layout read as if presets were per-preset
bundles, which they are not.

## 2. Goal

One YAML file per bundled preset, directly under the package:

```
src/tetramark/presets/
  __init__.py
  base.yaml
  library.yaml
  molbio.yaml
```

File contents are byte-identical to today's `preset.yaml` files apart from the removed directory
level — this is a layout refactor, not a schema change. `include: [base]` keeps working unchanged,
and no effective schema fingerprint changes.

## 3. Non-goals / decisions

1. **Directory-form presets stay supported.** The loader already accepts a preset as a directory
   (with optional `kinds/*.yaml` and `rules/*.yaml`) or as a single YAML file. Only the *bundled*
   presets are flattened; `load_preset(<path-to-dir>)` and `tm init --preset <dir>` must keep
   working for ledger authors. Existing `tests/data/presets/**` fixtures stay as they are — they are
   the regression coverage for the directory form.
2. **`__init__.py` stays.** `importlib.resources.files("tetramark.presets")` needs the package; the
   YAML files are package data next to it.
3. **No `pyproject.toml` change.** Hatchling ships every file under `src/tetramark`, so top-level
   package YAML is packaged exactly like the nested YAML was.
4. **Error displays change.** A schema error from a bundled preset now reports
   `molbio.yaml: kinds.plasmid.fields.construct.to` instead of `molbio/preset.yaml: …`. No test
   asserts the bundled display path; fixture paths in `tests/data/` are unaffected.
5. **Historical tickets are frozen.** `docs/tickets/01…19` describe past waves; do not rewrite them.
   Only living docs (`docs/spec.md`, `README.md`, `skills/**`) get updated.
6. **No behaviour change for `tm presets list|show`, `tm init`, `ledger.yaml` `presets:` entries.**

## 4. Required code changes

`src/tetramark/schema/loader.py`

- `list_bundled_presets()` (today: directories containing `preset.yaml`) must list the *stems of the
  top-level `*.yaml`/`*.yml` files* in the bundled package directory, sorted. Keep the function
  signature and the `list[str]` contract; update its docstring.
- Module docstring: `tetramark/presets/<name>/` → `tetramark/presets/<name>.yaml`; the example error
  path `molbio/preset.yaml: …` → `molbio.yaml: …`.
- `_bundled_source()`: `display = name if is_file else name` is redundant — collapse to `display =
  name` while touching the function.
- Nothing else: `_load_unit` already handles `is_file`, `_candidate_names` already tries
  `<name>.yaml`, and bundled `include:` resolution already falls back to the bundled root.

`src/tetramark/presets/__init__.py`

- Docstring: presets are flat `<name>.yaml` files in this package.

## 5. Tickets

| # | Ticket | Depends on | Write scope |
|---|---|---|---|
| R1 | Flatten preset files + `list_bundled_presets()` + loader/`__init__` docstrings | — | `src/tetramark/presets/`, `src/tetramark/schema/loader.py` |
| R2 | Update `docs/spec.md` (preset layout, repo-layout section) | — | `docs/spec.md` |
| R3 | Update skills docs (`SKILL.md`, `references/presets.md`, `commands.md`) | — | `skills/` |
| R4 | Tests: flat bundled presets, directory form still works | R1 | `tests/test_presets.py`, `tests/test_schema_loader.py` |
| R5 | Final verification (full affected suite + CLI smoke) | R1–R4 | — (read-only) |

R1, R2 and R3 are independent and may run in parallel (disjoint write scopes). R4 starts after R1
lands because it asserts the new layout. R5 is the Lead's gate.

Each ticket: no `git commit`, no `git checkout/stash/reset`, no touching files outside its scope, and
the working tree is left dirty for review.

## 6. Acceptance criteria

- `find src/tetramark/presets -name preset.yaml` is empty; the directory holds only `__init__.py`,
  `base.yaml`, `library.yaml`, `molbio.yaml` (plus `__pycache__/`, which is not committed).
- `list_bundled_presets() == ["base", "library", "molbio"]`.
- `tm presets list` prints the three names; `tm presets show molbio` prints `preset: molbio` and the
  include-resolved document.
- `tm init <name> --preset molbio` succeeds and `ledger_meta.preset_versions` is
  `{"base": 1, "molbio": 1}`.
- `load_schema(LedgerConfig(presets=["base"]))` fingerprint and kind set are unchanged from before
  the refactor (layout-only change).
- A directory-form preset (`tests/data/presets/full`) still loads via `load_preset(dir)`, and a
  self-contained flat YAML path still loads. (Unchanged loader behaviour: for a single-file source,
  `include:` resolves against that file's parent with a bundled-name fallback, so
  `load_preset(dir / "preset.yaml")` still needs `search_paths=[parent]` — the bundled flat files
  are self-contained because they fall back to the bundled root.)
- `pytest` is green for `tests/test_presets.py`, `tests/test_schema_loader.py`,
  `tests/test_ledger_init.py`, `tests/test_docs_commands.py` (non-server tests), `tests/test_ddl.py`.
- Docs contain no remaining reference to `presets/<name>/preset.yaml` (except frozen historical
  tickets and `tests/`).

## 7. Verification commands

`uv run` cannot be used in this environment (it fails to initialise its cache under `~/.cache/uv`).
Use the repo venv, and shim `ps` (the sandbox denies process listing, which the session-scoped
server-reaper fixture calls before any test runs):

```bash
mkdir -p /tmp/tmshim && printf '#!/bin/sh\nexit 0\n' > /tmp/tmshim/ps && chmod +x /tmp/tmshim/ps
PATH=/tmp/tmshim:$PATH .venv/bin/python -m pytest tests/test_schema_loader.py -q -n0
PATH=/tmp/tmshim:$PATH .venv/bin/python -m pytest tests/test_presets.py -q -n0
PATH=/tmp/tmshim:$PATH .venv/bin/tm presets list
PATH=/tmp/tmshim:$PATH .venv/bin/tm presets show molbio | head -5
```

## 8. Risks

- `list_bundled_presets()` silently returning `[]` (e.g. filtering on `is_dir`) would only be caught
  by `tests/test_presets.py::test_all_three_bundled_presets_are_discoverable` and the CLI tests — R4
  must keep both assertions and add one that the bundled name resolves to a *file* source.
- An orphan `__pycache__/` or `.DS_Store` left behind makes the tree look unflattened; R1 removes
  both from `src/tetramark/presets/`.
