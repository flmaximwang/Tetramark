"""The driver contract, run against both real backends (never a mock).

The embedded parameters run in a fresh ``dolt init`` repository per test.  The
server parameters share one workspace whose ``dolt sql-server`` runs on a random
free port; each test uses its own table name.
"""

from __future__ import annotations

import subprocess
import uuid
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from tetramark.driver import Driver, EmbeddedDriver, ServerDriver
from tetramark.driver.workspace import Workspace
from tetramark.errors import LedgerError, UnreachableError

from conftest import free_port, isolated_env

# -- fixtures -------------------------------------------------------------


@pytest.fixture(scope="session")
def server_workspace(tmp_path_factory: pytest.TempPathFactory, dolt_binary: str) -> Iterator[Workspace]:
    """A workspace with one ledger and a running server on a random free port."""
    base = tmp_path_factory.mktemp("server-workspace")
    env = isolated_env(base / "dolt-root", base / "home")
    for key, value in (
        ("user.name", "Tetramark Tests"),
        ("user.email", "tests@tetramark.invalid"),
    ):
        subprocess.run(
            [dolt_binary, "config", "--global", "--add", key, value],
            env=env,
            capture_output=True,
            check=True,
        )
    ledger = base / "data" / "led1"
    ledger.mkdir(parents=True)
    subprocess.run(
        [dolt_binary, "init"], cwd=ledger, env=env, capture_output=True, check=True
    )
    workspace = Workspace.create(base / "data", port=free_port(), default_ledger="led1", env=env)
    workspace.start(daemon=True)
    try:
        yield workspace
    finally:
        workspace.stop()


@pytest.fixture(scope="session")
def server_db(server_workspace: Workspace) -> Iterator[ServerDriver]:
    driver = server_workspace.driver_for("led1")
    assert isinstance(driver, ServerDriver)
    try:
        yield driver
    finally:
        driver.close()


@pytest.fixture
def embedded_driver(dolt_repo: Path, dolt_env: dict[str, str]) -> EmbeddedDriver:
    return EmbeddedDriver(dolt_repo, env=dolt_env)


@pytest.fixture(params=["embedded", pytest.param("server", marks=pytest.mark.server)])
def driver(request: pytest.FixtureRequest) -> Driver:
    """Each contract test runs once per backend."""
    if request.param == "embedded":
        return request.getfixturevalue("embedded_driver")
    return request.getfixturevalue("server_db")


def table_name() -> str:
    return f"contract_{uuid.uuid4().hex[:10]}"


# -- contract -------------------------------------------------------------


def test_database_name_points_at_the_ledger(driver: Driver) -> None:
    assert driver.database in {"repo", "led1"}


def test_round_trips_tricky_values(driver: Driver) -> None:
    table = table_name()
    driver.execute(
        f"CREATE TABLE {table} ("
        "id BIGINT PRIMARY KEY, label VARCHAR(255), note TEXT, "
        "qty DECIMAL(18,6), active TINYINT(1), seen_on DATE)"
    )
    tricky = (
        "quote's \"double\" `tick` \\backslash\\ 台账 管 🧪\n"
        "second line %s and 100%% -- not a comment; DROP TABLE boom"
    )
    driver.execute(
        f"INSERT INTO {table} (id, label, note, qty, active, seen_on) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (1, tricky, "note with 'quotes'", Decimal("1.250000"), True, date(2026, 10, 23)),
    )
    driver.execute(
        f"INSERT INTO {table} (id, label, note, qty, active, seen_on) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (2, None, None, None, False, None),
    )

    rows = driver.query(f"SELECT * FROM {table} ORDER BY id")
    assert len(rows) == 2
    first, second = rows
    assert first["id"] == 1
    assert first["label"] == tricky
    assert first["note"] == "note with 'quotes'"
    assert str(first["qty"]) in {"1.250000", "1.25"}
    assert first["active"] in (1, True)
    assert str(first["seen_on"]).startswith("2026-10-23")

    assert second["label"] is None
    assert second["note"] is None
    assert second["qty"] is None
    assert second["active"] in (0, False)


def test_query_returns_empty_list_for_no_rows(driver: Driver) -> None:
    table = table_name()
    driver.execute(f"CREATE TABLE {table} (id BIGINT PRIMARY KEY)")
    assert driver.query(f"SELECT * FROM {table}") == []


def test_transaction_commits_and_sees_its_own_writes(driver: Driver) -> None:
    table = table_name()
    driver.execute(f"CREATE TABLE {table} (v BIGINT)")
    with driver.transaction():
        driver.execute(f"INSERT INTO {table} VALUES (%s)", (1,))
        driver.execute(f"INSERT INTO {table} VALUES (%s)", (2,))
        inside = driver.query(f"SELECT COUNT(*) AS n FROM {table}")
        assert inside == [{"n": 2}]
    assert driver.query(f"SELECT COUNT(*) AS n FROM {table}") == [{"n": 2}]


def test_transaction_rolls_back_on_exception(driver: Driver) -> None:
    table = table_name()
    driver.execute(f"CREATE TABLE {table} (v BIGINT)")
    with pytest.raises(RuntimeError):
        with driver.transaction():
            driver.execute(f"INSERT INTO {table} VALUES (%s)", (1,))
            raise RuntimeError("boom")
    assert driver.query(f"SELECT COUNT(*) AS n FROM {table}") == [{"n": 0}]


def test_ddl_inside_transaction_is_effective(driver: Driver) -> None:
    table = table_name()
    with driver.transaction():
        driver.execute(f"CREATE TABLE {table} (v BIGINT)")
        assert driver.query(f"SELECT COUNT(*) AS n FROM {table}") == [{"n": 0}]
        driver.execute(f"INSERT INTO {table} VALUES (%s)", (1,))
    assert driver.query(f"SELECT COUNT(*) AS n FROM {table}") == [{"n": 1}]


def test_nested_transaction_is_refused(driver: Driver) -> None:
    with driver.transaction():
        with pytest.raises(LedgerError, match="nested"):
            with driver.transaction():
                pass


def test_commit_creates_a_dolt_commit(driver: Driver) -> None:
    table = table_name()
    driver.execute(f"CREATE TABLE {table} (v BIGINT)")
    driver.execute(f"INSERT INTO {table} VALUES (%s)", (1,))
    message = f"driver contract {table}"
    author = "Driver Tests <driver@tests.invalid>"

    commit_hash = driver.commit(message, author)
    assert commit_hash is not None and len(commit_hash) > 10

    rows = driver.query(
        "SELECT message, email FROM dolt_log WHERE commit_hash = %s", (commit_hash,)
    )
    assert rows == [{"message": message, "email": "driver@tests.invalid"}]


def test_commit_without_changes_returns_none(driver: Driver) -> None:
    table = table_name()
    driver.execute(f"CREATE TABLE {table} (v BIGINT)")
    assert driver.commit(f"first {table}") is not None
    assert driver.commit(f"second {table}") is None


def test_commit_refuses_non_ascii_message(driver: Driver) -> None:
    with pytest.raises(LedgerError, match="ASCII"):
        driver.commit("加了一支管")


def test_commit_inside_transaction_is_refused(driver: Driver) -> None:
    with driver.transaction():
        with pytest.raises(LedgerError, match="inside an open transaction"):
            driver.commit("too early")


def test_bad_sql_raises_ledger_error(driver: Driver) -> None:
    with pytest.raises(LedgerError, match="(?i)not found"):
        driver.query("SELECT * FROM definitely_missing_table")


# -- backend specifics ----------------------------------------------------


def test_embedded_rejects_a_directory_that_is_not_a_repository(tmp_path: Path) -> None:
    (tmp_path / "not-a-repo").mkdir()
    with pytest.raises(UnreachableError, match="not a Dolt database"):
        EmbeddedDriver(tmp_path / "not-a-repo")


def test_embedded_reports_dolt_errors(dolt_repo: Path, dolt_env: dict[str, str]) -> None:
    driver = EmbeddedDriver(dolt_repo, env=dolt_env)
    with pytest.raises(LedgerError, match="table not found"):
        driver.query("SELECT * FROM nope")


def test_server_driver_reports_unreachable_port(port: int) -> None:
    with pytest.raises(UnreachableError):
        ServerDriver("127.0.0.1", port, "led1", connect_timeout=1)


@pytest.mark.server
def test_server_driver_reports_missing_database(server_workspace: Workspace) -> None:
    with pytest.raises(LedgerError, match="(?i)database not found|unknown database"):
        ServerDriver(
            server_workspace.config.host,
            server_workspace.config.port,
            "no_such_ledger",
            connect_timeout=2,
        )
