"""Server-backed test harness: ``init_ledger_on_server`` and the fixtures.

These tests exercise the infrastructure from ticket 15: a per-worker
``dolt sql-server`` whose databases are created and dropped per test.  The
embedded path stays the reference implementation, so the equivalence test
compares schema, ``ledger_meta`` and commit history for both backends.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tetramark.core.ledger import Ledger, init_ledger_on_server
from tetramark.driver.workspace import Workspace
from tetramark.errors import UnreachableError

from conftest import (
    FACTORY_AUTHOR,
    PRESETS,
    _configure_dolt_identity,
    database_names,
    free_port,
    isolated_env,
    worker_id,
)

META_KEYS = ("spec_version", "fingerprint", "preset_versions", "ledger_yaml")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def table_names(ledger: Ledger) -> list[str]:
    """Every table and view in the ledger, in a stable order."""
    rows = ledger.driver.query(
        "SELECT table_name AS name FROM information_schema.tables "
        "WHERE table_schema = DATABASE() ORDER BY table_name"
    )
    return [str(row["name"]) for row in rows]


def commit_count(ledger: Ledger) -> int:
    return int(ledger.driver.query("SELECT COUNT(*) AS n FROM dolt_log")[0]["n"])


def meta_values(ledger: Ledger) -> dict[str, str]:
    return {key: ledger.meta[key] for key in META_KEYS}


# ---------------------------------------------------------------------------
# Equivalence with the embedded path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("preset", "name"),
    [
        (str(PRESETS / "base"), "equivalent-base"),
        ("molbio", "equivalent-molbio"),
    ],
)
def test_server_init_matches_embedded_init(
    preset: str,
    name: str,
    embedded_ledger_factory,
    server_ledger_factory,
) -> None:
    embedded = embedded_ledger_factory([preset], name=name)
    server = server_ledger_factory([preset], name=name)

    assert server.schema.fingerprint() == embedded.schema.fingerprint()
    assert table_names(server) == table_names(embedded)
    assert server.meta == embedded.meta
    assert meta_values(server) == meta_values(embedded)

    # One commit from the initial database plus exactly one init commit.
    assert commit_count(server) == commit_count(embedded) == 2
    assert server.driver.query("SELECT * FROM dolt_status") == []
    assert embedded.driver.query("SELECT * FROM dolt_status") == []


# ---------------------------------------------------------------------------
# Failure cleanup
# ---------------------------------------------------------------------------


def test_server_init_requires_running_server(tmp_path: Path) -> None:
    workspace = Workspace.create(tmp_path / "no-server", port=free_port())
    with pytest.raises(UnreachableError, match="no Dolt server is running"):
        init_ledger_on_server(
            workspace,
            f"t_{worker_id()}_offline",
            [PRESETS / "base"],
            author=FACTORY_AUTHOR,
        )
    assert not workspace.ledger_path(f"t_{worker_id()}_offline").exists()


def test_server_init_failure_cleans_up(
    monkeypatch: pytest.MonkeyPatch, server_workspace: Workspace
) -> None:
    name = f"t_{worker_id()}_failure"

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr("tetramark.core.ledger.install_tests", boom)
    with pytest.raises(RuntimeError, match="boom"):
        init_ledger_on_server(
            server_workspace,
            name,
            [PRESETS / "base"],
            id_prefix="77",
            author=FACTORY_AUTHOR,
        )

    assert name not in database_names(server_workspace)
    assert not (server_workspace.data_dir / name).exists()


# ---------------------------------------------------------------------------
# Per-test isolation and teardown
# ---------------------------------------------------------------------------


@pytest.fixture
def residue_probe(server_workspace: Workspace):
    """Fail the test if any ``t_<worker>_*`` database survives teardown.

    Requested before ``server_ledger_factory`` by :func:`isolated_factory`, so
    pytest finalizes it *after* the factory has closed and dropped its
    databases.
    """
    yield
    prefix = f"t_{worker_id()}_"
    leftovers = [name for name in database_names(server_workspace) if name.startswith(prefix)]
    assert leftovers == [], f"leftover test databases: {leftovers}"


@pytest.fixture
def isolated_factory(residue_probe, server_ledger_factory):
    return server_ledger_factory


def test_server_factory_isolates_tests(
    isolated_factory, server_workspace: Workspace
) -> None:
    one = isolated_factory()
    two = isolated_factory()

    prefix = f"t_{worker_id()}_"
    assert one.name.startswith(prefix)
    assert two.name.startswith(prefix)
    assert one.name != two.name
    databases = database_names(server_workspace)
    assert one.name in databases
    assert two.name in databases

    one.driver.execute(
        "INSERT INTO `ledger_meta` (`key`, `value`) VALUES ('isolation', 'one')"
    )
    assert one.driver.query(
        "SELECT `value` FROM `ledger_meta` WHERE `key` = %s", ("isolation",)
    ) == [{"value": "one"}]
    assert two.driver.query(
        "SELECT `value` FROM `ledger_meta` WHERE `key` = %s", ("isolation",)
    ) == []


# ---------------------------------------------------------------------------
# Factory arguments reach the new ledger
# ---------------------------------------------------------------------------


def test_server_factory_honours_arguments(server_ledger_factory) -> None:
    ledger = server_ledger_factory(
        name="custom-db",
        id_prefix="AB",
        author="Someone Else <someone@tetramark.invalid>",
        statuses=["fresh", "quarantined"],
    )

    assert ledger.name == "custom-db"
    assert ledger.database == "custom-db"
    assert ledger.author == "Someone Else <someone@tetramark.invalid>"
    assert ledger.schema.statuses == ["fresh", "quarantined"]
    assert ledger.next_id("stock").startswith("AB")

    create = ledger.driver.query("SHOW CREATE TABLE aliquot")[0]["Create Table"]
    assert "'fresh'" in create
    assert "'quarantined'" in create
    assert "'consumed'" not in create

    config = (ledger.path / "ledger.yaml").read_text(encoding="utf-8")
    assert "name: custom-db" in config
    assert "prefix: AB" in config
    assert "quarantined" in config


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------


@pytest.fixture
def throwaway_server(tmp_path: Path, dolt_binary: str):
    """A second, manually managed workspace; its teardown checks the stop path."""
    base = tmp_path / "throwaway"
    env = isolated_env(base / "dolt-root", base / "home")
    _configure_dolt_identity(dolt_binary, env)

    data_dir = base / "data"
    bootstrap = data_dir / "bootstrap"
    bootstrap.mkdir(parents=True)
    subprocess.run(
        [dolt_binary, "init"],
        cwd=bootstrap,
        env=env,
        capture_output=True,
        check=True,
    )
    workspace = Workspace.create(data_dir, port=free_port(), env=env)
    workspace.start(daemon=True)
    try:
        yield workspace
    finally:
        assert workspace.stop() is True
        assert workspace.running_pid() is None
        assert not workspace.port_in_use()


def test_server_workspace_stops_cleanly(throwaway_server: Workspace) -> None:
    assert throwaway_server.running_pid() is not None
    assert throwaway_server.port_in_use()
    assert throwaway_server.list_ledgers() == ["bootstrap"]


# ---------------------------------------------------------------------------
# Spawn count
# ---------------------------------------------------------------------------


def test_server_ledger_init_spawns_no_dolt_process(
    monkeypatch: pytest.MonkeyPatch, server_ledger_factory
) -> None:
    """A server-backed init must not shell out to ``dolt`` at all."""
    calls: list[tuple[object, ...]] = []
    real_run = subprocess.run

    def counting_run(*args: object, **kwargs: object):
        calls.append(args)
        return real_run(*args, **kwargs)

    monkeypatch.setattr("tetramark.core.ledger.subprocess.run", counting_run)
    ledger = server_ledger_factory()

    dolt_calls = [args for args in calls if str(args[0][0]).endswith("/dolt")]
    assert dolt_calls == []
    assert calls == []
    assert ledger.name.startswith(f"t_{worker_id()}_")


def test_workspace_stop_reaps_server_child(
    dolt_binary: str, tmp_path: Path
) -> None:
    """``Workspace.stop()`` in the process that started the server must not wait
    for a zombie: it is < 5 s instead of the ``READY_TIMEOUT`` (12 s)."""
    import time

    from tetramark.driver.workspace import Workspace

    env = isolated_env(tmp_path / "root", tmp_path / "home")
    _configure_dolt_identity(dolt_binary, env)
    data_dir = tmp_path / "data"
    bootstrap = data_dir / "bootstrap"
    bootstrap.mkdir(parents=True)
    subprocess.run(
        [dolt_binary, "init"], cwd=bootstrap, env=env, capture_output=True, check=True
    )
    workspace = Workspace.create(data_dir, port=free_port(), env=env)
    workspace.start(daemon=True)
    started = time.monotonic()
    assert workspace.stop() is True
    elapsed = time.monotonic() - started
    assert elapsed < 5.0, f"stop() took {elapsed:.1f}s (zombie not reaped)"
    assert workspace.running_pid() is None
