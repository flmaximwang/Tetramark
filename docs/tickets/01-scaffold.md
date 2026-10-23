# 01 · Scaffold & test harness
Depends on: —  · Spec: §9

Create the uv project skeleton.
- `pyproject.toml` (name `doltledger`, Python >=3.11, deps typer, pyyaml, pydantic>=2, pymysql, rich; dev: pytest),
  console script `ledger = doltledger.cli.app:main`, src layout, presets as package data.
- Empty packages per §9 layout (`driver/ schema/ core/ ops/ cli/ presets/`) with `__init__.py`.
- `cli/app.py`: Typer root with `--version`, global options placeholders (`--workspace`, `--ledger`), exit-code
  constants (0/1/2/3) in `doltledger/errors.py` with exception classes `LedgerError`(1), `UsageError`(2), `UnreachableError`(3).
- `tests/conftest.py`: fixture `dolt_repo(tmp_path)` → runs `dolt init` (set `dolt config --global` name/email
  via env `DOLT_ROOT_PATH=tmp` so it never touches the user's global config); skip suite cleanly if `dolt` missing.
- `tests/test_smoke.py`: `ledger --version` works; `dolt_repo` fixture yields an initialised repo.

Done when: `uv sync && uv run pytest` passes; `uv run ledger --help` prints.
