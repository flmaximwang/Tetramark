"""Workspace config, ledger listing and embedded driver selection."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tetramark.driver import EmbeddedDriver
from tetramark.driver.workspace import (
    DEFAULT_PORT,
    Workspace,
    WorkspaceConfig,
)
from tetramark.errors import LedgerError, UnreachableError, UsageError


def write_workspace(path: Path, **overrides: object) -> Workspace:
    payload: dict[str, object] = {"host": "127.0.0.1", "port": DEFAULT_PORT}
    payload.update(overrides)
    path.mkdir(parents=True, exist_ok=True)
    (path / "workspace.yaml").write_text(yaml.safe_dump(payload), encoding="utf-8")
    return Workspace.load(path)


@pytest.fixture
def workspace(tmp_path: Path, dolt_env: dict[str, str]) -> Workspace:
    return Workspace.create(tmp_path / "data", env=dolt_env)


# -- config ---------------------------------------------------------------


def test_create_writes_workspace_yaml(tmp_path: Path) -> None:
    workspace = Workspace.create(tmp_path / "data", port=13310, default_ledger="led1")

    assert workspace.data_dir == (tmp_path / "data").resolve()
    assert workspace.config_path.is_file()
    payload = yaml.safe_load(workspace.config_path.read_text(encoding="utf-8"))
    assert payload == {
        "host": "127.0.0.1",
        "port": 13310,
        "default_ledger": "led1",
    }


def test_create_defaults(workspace: Workspace) -> None:
    assert workspace.config.host == "127.0.0.1"
    assert workspace.config.port == DEFAULT_PORT == 13308
    assert workspace.config.default_ledger is None
    assert workspace.config.author is None


def test_create_twice_refuses(tmp_path: Path) -> None:
    Workspace.create(tmp_path / "data")
    with pytest.raises(LedgerError, match="already exists"):
        Workspace.create(tmp_path / "data")


def test_load_accepts_directory_or_file(workspace: Workspace) -> None:
    from_dir = Workspace.load(workspace.data_dir)
    from_file = Workspace.load(workspace.config_path)
    assert from_dir.config == from_file.config == workspace.config


def test_load_missing_is_a_usage_error(tmp_path: Path) -> None:
    with pytest.raises(UsageError, match="workspace.yaml"):
        Workspace.load(tmp_path / "nowhere")


def test_load_rejects_invalid_yaml(tmp_path: Path) -> None:
    (tmp_path / "workspace.yaml").write_text("- not a mapping\n", encoding="utf-8")
    with pytest.raises(UsageError, match="expected a mapping"):
        Workspace.load(tmp_path)


def test_load_rejects_invalid_values(tmp_path: Path) -> None:
    (tmp_path / "workspace.yaml").write_text("port: not-a-number\n", encoding="utf-8")
    with pytest.raises(UsageError, match="invalid"):
        Workspace.load(tmp_path)


def test_unknown_keys_are_ignored(tmp_path: Path) -> None:
    workspace = write_workspace(tmp_path, future_key="kept")
    assert workspace.config.port == DEFAULT_PORT


def test_discover_walks_up(tmp_path: Path) -> None:
    workspace = write_workspace(tmp_path / "data")
    nested = workspace.data_dir / "a" / "b"
    nested.mkdir(parents=True)
    found = Workspace.discover(nested)
    assert found is not None
    assert found.data_dir == workspace.data_dir


def test_discover_uses_environment_fallback(tmp_path: Path) -> None:
    workspace = write_workspace(tmp_path / "data")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    found = Workspace.discover(elsewhere, env={"TETRAMARK_WORKSPACE": str(workspace.data_dir)})
    assert found is not None
    assert found.data_dir == workspace.data_dir


def test_discover_returns_none_without_workspace(tmp_path: Path) -> None:
    start = tmp_path / "nothing"
    start.mkdir()
    assert Workspace.discover(start, env={}) is None


# -- ledgers --------------------------------------------------------------


def test_list_ledgers_finds_direct_dolt_directories(
    workspace: Workspace, make_ledger
) -> None:
    make_ledger(workspace.data_dir / "led1")
    make_ledger(workspace.data_dir / "led2")
    (workspace.data_dir / "not-a-ledger").mkdir()
    (workspace.data_dir / ".hidden").mkdir()
    (workspace.data_dir / "led1" / "nested").mkdir()

    assert workspace.list_ledgers() == ["led1", "led2"]


def test_list_ledgers_empty_for_missing_data_dir(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "gone")
    assert workspace.list_ledgers() == []


# -- start / stop ---------------------------------------------------------


def test_driver_for_uses_embedded_when_no_server(
    workspace: Workspace, make_ledger
) -> None:
    make_ledger(workspace.data_dir / "led1")
    driver = workspace.driver_for("led1")
    assert isinstance(driver, EmbeddedDriver)
    assert driver.query("SELECT 1 AS one") == [{"one": 1}]


def test_driver_for_requires_a_ledger(workspace: Workspace) -> None:
    with pytest.raises(UsageError, match="no ledger selected"):
        workspace.driver_for()


def test_driver_for_missing_ledger(workspace: Workspace) -> None:
    workspace.config.default_ledger = "nope"
    with pytest.raises(UnreachableError, match="not found"):
        workspace.driver_for()



# This module intentionally keeps the embedded-driver path covered.
pytestmark = pytest.mark.embedded
