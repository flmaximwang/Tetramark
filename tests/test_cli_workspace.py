"""``tm workspace init`` through the CLI."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from tetramark.cli.app import app
from tetramark.driver.workspace import Workspace
from tetramark.errors import LedgerError

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolated_process(monkeypatch: pytest.MonkeyPatch, dolt_env: dict[str, str], tmp_path: Path) -> None:
    """Point the CLI at the isolated Dolt/Git config."""
    for key in (
        "DOLT_ROOT_PATH",
        "HOME",
        "XDG_CONFIG_HOME",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_SYSTEM",
    ):
        monkeypatch.setenv(key, dolt_env[key])
    monkeypatch.delenv("TETRAMARK_WORKSPACE", raising=False)
    monkeypatch.chdir(tmp_path)


def test_workspace_init_creates_config(tmp_path: Path) -> None:
    data = tmp_path / "data"
    result = runner.invoke(app, ["workspace", "init", str(data), "--port", "13310"])

    assert result.exit_code == 0, result.output
    assert "Created workspace" in result.output
    payload = yaml.safe_load((data / "workspace.yaml").read_text(encoding="utf-8"))
    assert payload == {"host": "127.0.0.1", "port": 13310}


def test_workspace_init_can_set_default_ledger_and_author(tmp_path: Path) -> None:
    data = tmp_path / "data"
    result = runner.invoke(
        app,
        [
            "workspace",
            "init",
            str(data),
            "--default-ledger",
            "lab101",
            "--author",
            "Maxim <maxim@example.invalid>",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = yaml.safe_load((data / "workspace.yaml").read_text(encoding="utf-8"))
    assert payload["default_ledger"] == "lab101"
    assert payload["author"] == "Maxim <maxim@example.invalid>"


def test_workspace_init_stores_presets_dir_and_config_manages_it(
    tmp_path: Path,
) -> None:
    data = tmp_path / "data"
    result = runner.invoke(
        app,
        ["workspace", "init", str(data), "--presets-dir", "presets"],
    )
    assert result.exit_code == 0, result.output
    payload = yaml.safe_load((data / "workspace.yaml").read_text(encoding="utf-8"))
    assert payload["presets_dir"] == "presets"

    # Relative values resolve against workspace.yaml, not the caller's cwd.
    workspace = Workspace.load(data)
    assert workspace.presets_dir_path == (data / "presets").resolve()

    got = runner.invoke(
        app, ["--workspace", str(data), "config", "get", "presets_dir"]
    )
    assert got.exit_code == 0, got.output
    assert got.output.strip() == str((data / "presets").resolve())

    changed = runner.invoke(
        app,
        ["--workspace", str(data), "config", "set", "presets_dir", "/tmp/presets"],
    )
    assert changed.exit_code == 0, changed.output
    payload = yaml.safe_load((data / "workspace.yaml").read_text(encoding="utf-8"))
    assert payload["presets_dir"] == "/tmp/presets"


def test_workspace_init_defaults_to_the_current_directory() -> None:
    result = runner.invoke(app, ["workspace", "init"])
    assert result.exit_code == 0, result.output
    assert (Path.cwd() / "workspace.yaml").is_file()


def test_workspace_init_twice_fails(tmp_path: Path) -> None:
    data = tmp_path / "data"
    assert runner.invoke(app, ["workspace", "init", str(data)]).exit_code == 0
    result = runner.invoke(app, ["workspace", "init", str(data)])
    assert result.exit_code == 1
    assert isinstance(result.exception, LedgerError)


# This module intentionally keeps the embedded-driver path covered.
pytestmark = pytest.mark.embedded
