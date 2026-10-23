"""Smoke tests for the scaffold: CLI entry point, exit codes, dolt fixture."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from doltledger import __version__
from doltledger.cli.app import app
from doltledger.errors import (
    EXIT_ERROR,
    EXIT_OK,
    EXIT_UNREACHABLE,
    EXIT_USAGE,
    LedgerError,
    UnreachableError,
    UsageError,
)

runner = CliRunner()


def test_version_flag_prints_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == EXIT_OK
    assert result.output.strip() == f"ledger {__version__}"


def test_help_lists_global_options() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == EXIT_OK
    assert "--version" in result.output
    assert "--workspace" in result.output
    assert "--ledger" in result.output


def test_installed_console_script_prints_version() -> None:
    script = Path(sys.executable).parent / "ledger"
    if not script.exists():
        pytest.skip("the 'ledger' console script is not installed in this environment")
    result = subprocess.run([str(script), "--version"], capture_output=True, text=True)
    assert result.returncode == EXIT_OK
    assert result.stdout.strip() == f"ledger {__version__}"


def test_exit_codes() -> None:
    assert (EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_UNREACHABLE) == (0, 1, 2, 3)
    assert LedgerError.exit_code == 1
    assert UsageError.exit_code == 2
    assert UnreachableError.exit_code == 3
    assert issubclass(UsageError, LedgerError)
    assert issubclass(UnreachableError, LedgerError)


def test_usage_error_exits_2() -> None:
    result = runner.invoke(app, ["--no-such-option"])
    assert result.exit_code == EXIT_USAGE


def test_dolt_repo_is_initialised(dolt_repo: Path, dolt_run) -> None:
    assert (dolt_repo / ".dolt").is_dir()

    result = dolt_run("sql", "-r", "json", "-q", "SELECT 1 AS one", cwd=dolt_repo)
    assert result.returncode == 0
    assert json.loads(result.stdout)["rows"] == [{"one": 1}]

    log = dolt_run("log", "--oneline", cwd=dolt_repo)
    assert "Initialize data repository" in log.stdout


def test_dolt_repo_uses_isolated_global_config(
    dolt_repo: Path, dolt_env: dict[str, str], dolt_run, tmp_path: Path
) -> None:
    assert dolt_env["DOLT_ROOT_PATH"] == str(tmp_path / "dolt-root")
    assert dolt_env["HOME"] == str(tmp_path / "home")

    config = dolt_run("config", "--global", "--list", cwd=dolt_repo)
    assert "user.name" in config.stdout
    assert "DoltLedger Tests" in config.stdout

    # The isolated root holds the config; the real one was never consulted.
    global_config = tmp_path / "dolt-root" / ".dolt" / "config_global.json"
    assert global_config.is_file()


def test_dolt_repo_does_not_write_to_home(dolt_repo: Path, dolt_env: dict[str, str]) -> None:
    home = Path(dolt_env["HOME"])
    assert home.is_dir()
    assert list(home.rglob("*")) == []
