"""Workspace config, ledger listing and server lifecycle (real dolt server)."""

from __future__ import annotations

import socket
from pathlib import Path

import pytest
import yaml

from tetramark.driver import EmbeddedDriver, ServerDriver
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


# -- port -----------------------------------------------------------------


def test_port_in_use_detects_a_listener(port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", port))
        listener.listen()
        workspace = Workspace("/tmp", WorkspaceConfig(port=port))
        assert workspace.port_in_use()


def test_port_in_use_false_when_free(port: int) -> None:
    workspace = Workspace("/tmp", WorkspaceConfig(port=port))
    assert not workspace.port_in_use()


# -- running pid ----------------------------------------------------------


def test_stale_pid_file_is_removed(workspace: Workspace) -> None:
    workspace.pid_file.write_text("999999", encoding="utf-8")
    assert workspace.running_pid() is None
    assert not workspace.pid_file.exists()


def test_pid_file_with_garbage_is_ignored(workspace: Workspace) -> None:
    workspace.pid_file.write_text("not-a-pid", encoding="utf-8")
    assert workspace.running_pid() is None


def test_status_when_not_running(workspace: Workspace) -> None:
    status = workspace.status()
    assert not status.running
    assert status.pid is None
    assert status.host == "127.0.0.1"
    assert status.port == DEFAULT_PORT
    assert status.endpoint() == "mysql://root@127.0.0.1:13308"
    assert status.endpoint("led1").endswith("/led1")


def test_log_tail_reads_the_end(workspace: Workspace) -> None:
    workspace.log_file.write_text("one\ntwo\nthree\n", encoding="utf-8")
    assert workspace.log_tail(2) == "two\nthree"
    workspace.log_file.unlink()
    assert "no log" in workspace.log_tail()


# -- start / stop ---------------------------------------------------------


def test_start_without_ledgers_is_refused(workspace: Workspace) -> None:
    with pytest.raises(LedgerError, match="no ledgers"):
        workspace.start(daemon=True)


def test_start_with_busy_port_is_refused(
    workspace: Workspace, make_ledger, port: int
) -> None:
    make_ledger(workspace.data_dir / "led1")
    workspace.config.port = port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", port))
        listener.listen()
        with pytest.raises(LedgerError, match=f"port {port} is already in use"):
            workspace.start(daemon=True)
    assert not workspace.pid_file.exists()


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


@pytest.mark.server
def test_server_lifecycle(workspace: Workspace, make_ledger, port: int) -> None:
    make_ledger(workspace.data_dir / "led1")
    workspace.config.port = port
    workspace.config.default_ledger = "led1"

    assert not workspace.status().running
    assert isinstance(workspace.driver_for("led1"), EmbeddedDriver)

    pid = workspace.start(daemon=True)
    try:
        assert pid is not None
        assert workspace.pid_file.read_text().strip() == str(pid)
        assert workspace.log_file.is_file()

        status = workspace.status()
        assert status.running
        assert status.pid == pid
        assert status.port_busy
        assert status.ledgers == ["led1"]
        assert status.default_ledger == "led1"

        with pytest.raises(LedgerError, match="already running"):
            workspace.start(daemon=True)

        driver = workspace.driver_for("led1")
        assert isinstance(driver, ServerDriver)
        assert driver.query("SELECT 1 AS one") == [{"one": 1}]
        driver.close()
    finally:
        assert workspace.stop() is True

    assert not workspace.status().running
    assert not workspace.pid_file.exists()
    assert workspace.stop() is False

    # The ledger is usable again through the embedded driver.
    driver = workspace.driver_for("led1")
    assert isinstance(driver, EmbeddedDriver)
    assert driver.query("SELECT 1 AS one") == [{"one": 1}]


@pytest.mark.server
def test_server_start_failure_leaves_no_pid_file(
    workspace: Workspace, make_ledger, monkeypatch: pytest.MonkeyPatch, port: int
) -> None:
    make_ledger(workspace.data_dir / "led1")
    workspace.config.port = port

    # A server command that exits immediately: the readiness probe must fail
    # fast, clean up the pid file and surface the log tail.
    monkeypatch.setattr(Workspace, "_server_argv", lambda self: ["/usr/bin/false"])
    with pytest.raises(LedgerError, match="did not become ready"):
        workspace.start(daemon=True)
    assert not workspace.pid_file.exists()


# This module intentionally keeps the embedded-driver path covered.
pytestmark = pytest.mark.embedded
