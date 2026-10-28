# 20 · Presets live in the repo, and each ledger keeps its own copy
Depends on: — · Spec: §4 (presets), §5 · Issue: git-bug `2f6a0d5`

Goal: presets stop being package data. They live in `presets/` at the repo root. `tm init` copies each
selected preset, plus everything it includes, into the ledger directory, so a ledger is self-contained
and does not depend on where the Tetramark checkout lives.

## Decisions (settled, do not re-open)

- Move `src/tetramark/presets/{base,library,molbio}.yaml` to `presets/` (repo root). Remove the
  `tetramark.presets` package and the importlib-resources lookup (`_bundled_root`, `_bundled_source`,
  `list_bundled_presets` in `schema/loader.py`). The wheel no longer ships presets.
- `tm init --preset NAME|PATH`:
  - `PATH` (contains `/` or ends in `.yaml`): that file.
  - `NAME`: `<presets_dir>/<NAME>.yaml`, where `presets_dir` is a new key in the workspace config
    (`workspace.yaml`, resolved relative to that file). If no `presets_dir` is configured, report an error
    that names the key.
  - The preset **and its `include:` closure** (e.g. `molbio` → `base`) are copied to
    `<ledger>/presets/<name>.yaml`. `ledger.yaml` keeps listing bare names (`presets: [molbio]`).
- When a ledger is opened, presets resolve **only** from `<ledger>/presets/`. If the copy is missing, fail
  with a message saying which file is expected and that `migration/` (ticket 21) or a manual copy fixes
  it. Do not fall back to `presets_dir`: a silent fallback would let the ledger drift from its recorded
  `preset_versions`.
- Includes inside a copied preset resolve relative to that copy's directory.
- `ledger_meta.preset_versions` is still written exactly as today.

## Files

- `presets/*.yaml` (moved, content unchanged)
- `src/tetramark/schema/loader.py`, `src/tetramark/core/ledger.py` (init copies, open resolves)
- `src/tetramark/driver/workspace.py`, `src/tetramark/cli/config.py`, `src/tetramark/cli/ledger.py`
  (`presets_dir`, the `--preset` help text, any `tm preset list`-style output)
- `pyproject.toml` (only if package-data config needs touching)
- `tests/conftest.py` + the tests that use bundled names (`test_presets.py`, `test_schema_loader.py`,
  `test_ledger_init.py`, …): point fixtures at the repo `presets/` through `presets_dir`.
- `docs/spec.md` §4, `README.md`, `skills/tetramark/SKILL.md`, `docs/migration.md` §0 (init command)

## Constraints

- No preset content changes here (ticket 22 does renames). Keep `version: 1` on every preset.
- zsqlab10 (`/Users/maxim/Documents/Lab/Dolt/zsqlab_10_gammaPFD-Fiber_2026.06.25`) has no local
  copy yet. That's expected: ticket 24 handles it. Do not touch that ledger here.
- Tests run against a real `dolt`, with no mocks. Do not commit; leave the tree dirty.

## Verify

- `tm init --preset molbio` with `presets_dir` set produces `<ledger>/presets/{molbio,base}.yaml`,
  byte-identical to the repo files.
- `tm init --preset ./some/custom.yaml` works without `presets_dir`.
- Opening a ledger whose `presets/` copy was deleted fails with the documented message.
- Changing the repo's `presets/molbio.yaml` after init does **not** change an existing ledger's schema.
- `uv build` → the wheel contains no `*.yaml` presets.
- Full suite green.
