"""The opt-in ``dolt sql-server`` driver, against a real Dolt server.

No mocks: every test here starts a real ``dolt sql-server`` in a real
``dolt init`` repository and talks to it over PyMySQL.  The driver is the
opt-in backend selected by ``TETRAMARK_DRIVER=server``; the default embedded
driver is unchanged and keeps its own contract tests.

This module runs in both lanes.  It pins the callback parity that matters for
the full suite:

* values come back with the embedded driver's JSON shapes (``DECIMAL`` and
  ``DATE``/``DATETIME`` as text, ``NULL`` columns present);
* ``query_many``/``execute_many`` keep one result per statement and their order;
* transactions roll back, DDL keeps Dolt's implicit-commit behaviour, and a
  command still makes exactly one commit with the same message and author;
* a ledger opened with ``TETRAMARK_DRIVER=server`` really uses this driver;
* the server cache is bounded, kills evicted processes, is reused by the next
  ledger open, and leaves no ``dolt sql-server`` behind after
  :func:`~tetramark.driver.server.shutdown_all_servers`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pymysql = pytest.importorskip("pymysql")

from conftest import FACTORY_AUTHOR, PRESETS
from tetramark.core.ledger import Ledger, init_ledger, open as open_ledger
from tetramark.driver import (
    DRIVER_ENV,
    Driver,
    EmbeddedDriver,
    driver_for,
    driver_mode,
)
from tetramark.driver import server as server_module
from tetramark.driver.server import (
    ServerDriver,
    cached_server_count,
    server_start_count,
    shutdown_all_servers,
)
from tetramark.errors import LedgerError, UsageError

# -- fixtures -------------------------------------------------------------


@pytest.fixture
def server_env(dolt_env: dict[str, str]) -> dict[str, str]:
    """The isolated Dolt environment, with server reuse disabled.

    ``TETRAMARK_SERVER_CACHE=0`` makes each driver own its server, so a test
    can assert on the exact process it started instead of the shared cache.
    """
    env = dict(dolt_env)
    env["TETRAMARK_SERVER_CACHE"] = "0"
    return env


@pytest.fixture
def server_driver(dolt_repo: Path, server_env: dict[str, str]) -> Iterator[ServerDriver]:
    driver = ServerDriver(dolt_repo, env=server_env)
    try:
        yield driver
    finally:
        driver.close()


def table_name() -> str:
    import uuid

    return f"server_{uuid.uuid4().hex[:10]}"


def owned_servers_for(path: Path) -> list[server_module._Server]:
    """Servers this process started for ``path`` that are still running."""
    resolved = Path(path).expanduser().resolve()
    with server_module._LOCK:
        return [
            server
            for server in server_module._OWNED
            if server.path == resolved and server.alive()
        ]


# -- backend selection ----------------------------------------------------


def test_driver_mode_defaults_to_embedded() -> None:
    assert driver_mode({}) == "embedded"
    assert driver_mode({DRIVER_ENV: ""}) == "embedded"
    assert driver_mode({DRIVER_ENV: " embedded "}) == "embedded"
    assert driver_mode({DRIVER_ENV: "SERVER"}) == "server"


def test_driver_mode_rejects_an_unknown_backend() -> None:
    with pytest.raises(UsageError, match=DRIVER_ENV):
        driver_mode({DRIVER_ENV: "mysql"})


def test_driver_for_selects_the_server_backend(
    dolt_repo: Path, server_env: dict[str, str]
) -> None:
    embedded = driver_for(dolt_repo, env={**server_env, DRIVER_ENV: "embedded"})
    assert isinstance(embedded, EmbeddedDriver)
    selected = driver_for(dolt_repo, env={**server_env, DRIVER_ENV: "server"})
    assert isinstance(selected, ServerDriver)
    try:
        assert selected.query("SELECT 1 AS one") == [{"one": 1}]
    finally:
        selected.close()


def test_open_with_server_env_uses_the_server_driver(
    ledger_factory: Any, server_env: dict[str, str]
) -> None:
    """The mode is selected from the effective ``env=`` mapping, not globally."""
    ledger = ledger_factory(name="server-open")
    ledger.close()  # the mode is per open, not per path
    env = {**server_env, DRIVER_ENV: "server"}
    opened = open_ledger(ledger.path, env=env)
    try:
        assert isinstance(opened.driver, ServerDriver)
        assert opened.driver.query(
            "SELECT `key`, `value` FROM `ledger_meta` WHERE `key` = 'spec_version'"
        ) == [{"key": "spec_version", "value": opened.meta["spec_version"]}]
    finally:
        opened.close()
    assert not owned_servers_for(ledger.path)


def test_init_with_server_env_uses_the_server_driver(
    tmp_path: Path, server_env: dict[str, str]
) -> None:
    env = {**server_env, DRIVER_ENV: "server"}
    ledger = init_ledger(
        tmp_path / "server-init",
        "server-init",
        [str(PRESETS / "base")],
        id_prefix="77",
        author=FACTORY_AUTHOR,
        env=env,
    )
    try:
        assert isinstance(ledger, Ledger)
        assert isinstance(ledger.driver, ServerDriver)
        # Init made one Dolt commit through the server driver.
        assert ledger.driver.query(
            "SELECT COUNT(*) AS n FROM dolt_log WHERE message = 'init(ledger): server-init'"
        ) == [{"n": 1}]
    finally:
        ledger.close()
    assert not owned_servers_for(tmp_path / "server-init")


# -- value parity with the embedded ``-r json`` output --------------------


def test_round_trips_tricky_values(server_driver: ServerDriver) -> None:
    table = table_name()
    server_driver.execute(
        f"CREATE TABLE {table} ("
        "id BIGINT PRIMARY KEY, label VARCHAR(255), note MEDIUMTEXT, "
        "qty DECIMAL(18,6), active TINYINT(1), seen_on DATE, seen_at DATETIME)"
    )
    tricky = (
        "quote's \"double\" `tick` \\backslash\\ 台账 管 🧪\n"
        "second line %s and 100%% -- not a comment; DROP TABLE boom"
    )
    server_driver.execute(
        f"INSERT INTO {table} (id, label, note, qty, active, seen_on, seen_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (
            1,
            tricky,
            "note with 'quotes'",
            "1.250000",
            True,
            "2026-10-23",
            "2026-10-23 12:34:56",
        ),
    )
    server_driver.execute(
        f"INSERT INTO {table} (id, label, note, qty, active, seen_on, seen_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (2, None, None, None, False, None, None),
    )

    rows = server_driver.query(f"SELECT * FROM {table} ORDER BY id")
    assert len(rows) == 2
    first, second = rows
    assert first["id"] == 1
    assert first["label"] == tricky
    assert first["note"] == "note with 'quotes'"
    # The embedded driver's JSON output reports these as text; the server
    # driver normalises PyMySQL's Decimal/date objects to the same shapes so
    # every CLI/JSON output stays byte-identical across backends.
    assert first["qty"] == "1.250000"
    assert first["active"] == 1
    assert first["seen_on"] == "2026-10-23"
    assert first["seen_at"] == "2026-10-23 12:34:56"
    assert second["label"] is None
    assert second["note"] is None
    assert second["qty"] is None
    assert second["active"] == 0
    assert set(second) == set(first)


def test_query_returns_every_column_without_a_csv_run(
    server_driver: ServerDriver,
) -> None:
    assert server_driver.query("SELECT NULL AS only_null") == [{"only_null": None}]
    assert server_driver.query("SELECT 1 AS n WHERE 1 = 0") == []


def test_query_many_keeps_one_result_per_statement(
    server_driver: ServerDriver,
) -> None:
    table = table_name()
    server_driver.execute(f"CREATE TABLE {table} (id BIGINT PRIMARY KEY, label VARCHAR(255))")
    server_driver.execute(f"INSERT INTO {table} VALUES (%s, %s)", (1, "one"))
    server_driver.execute(f"INSERT INTO {table} VALUES (%s, %s)", (2, None))

    results = server_driver.query_many(
        [
            (f"SELECT id, label FROM {table} ORDER BY id", None),
            ("SELECT 1 AS n WHERE 1 = 0", None),
            (f"SELECT COUNT(*) AS n FROM {table}", None),
            ("SELECT NULL AS only_null", None),
        ]
    )
    assert results == [
        [{"id": 1, "label": "one"}, {"id": 2, "label": None}],
        [],
        [{"n": 2}],
        [{"only_null": None}],
    ]


def test_bad_sql_raises_a_ledger_error(server_driver: ServerDriver) -> None:
    with pytest.raises(LedgerError, match="(?i)not found"):
        server_driver.query("SELECT * FROM definitely_missing_table")
    with pytest.raises(LedgerError, match="(?i)not found"):
        server_driver.query_many(
            [("SELECT 1 AS ok", None), ("SELECT * FROM definitely_missing_table", None)]
        )


# -- transactions and commits ---------------------------------------------


def test_transaction_commits_and_sees_its_own_writes(
    server_driver: ServerDriver,
) -> None:
    table = table_name()
    server_driver.execute(f"CREATE TABLE {table} (v BIGINT)")
    with server_driver.transaction():
        server_driver.execute(f"INSERT INTO {table} VALUES (%s)", (1,))
        server_driver.execute(f"INSERT INTO {table} VALUES (%s)", (2,))
        assert server_driver.query(f"SELECT COUNT(*) AS n FROM {table}") == [{"n": 2}]
    assert server_driver.query(f"SELECT COUNT(*) AS n FROM {table}") == [{"n": 2}]


def test_transaction_rolls_back_on_exception(server_driver: ServerDriver) -> None:
    table = table_name()
    server_driver.execute(f"CREATE TABLE {table} (v BIGINT)")
    with pytest.raises(RuntimeError):
        with server_driver.transaction():
            server_driver.execute(f"INSERT INTO {table} VALUES (%s)", (1,))
            raise RuntimeError("boom")
    assert server_driver.query(f"SELECT COUNT(*) AS n FROM {table}") == [{"n": 0}]


def test_ddl_inside_a_transaction_is_effective(server_driver: ServerDriver) -> None:
    table = table_name()
    with server_driver.transaction():
        server_driver.execute(f"CREATE TABLE {table} (v BIGINT)")
        assert server_driver.query(f"SELECT COUNT(*) AS n FROM {table}") == [{"n": 0}]
        server_driver.execute(f"INSERT INTO {table} VALUES (%s)", (1,))
    assert server_driver.query(f"SELECT COUNT(*) AS n FROM {table}") == [{"n": 1}]


def test_ddl_keeps_the_embedded_implicit_commit_semantics(
    server_driver: ServerDriver,
) -> None:
    """DML before a DDL statement is committed by Dolt; later DML still rolls back.

    That is exactly what the embedded write session does (its deferred DML is
    flushed before the DDL batch), so a failing command leaves the same state.
    """
    before = table_name()
    after = table_name()
    server_driver.execute(f"CREATE TABLE {before} (v BIGINT)")
    with pytest.raises(RuntimeError):
        with server_driver.transaction():
            server_driver.execute(f"INSERT INTO {before} VALUES (%s)", (1,))
            server_driver.execute(f"CREATE TABLE {after} (v BIGINT)")
            server_driver.execute(f"INSERT INTO {after} VALUES (%s)", (2,))
            raise RuntimeError("boom")
    assert server_driver.query(f"SELECT COUNT(*) AS n FROM {before}") == [{"n": 1}]
    assert server_driver.query(f"SELECT COUNT(*) AS n FROM {after}") == [{"n": 0}]


def test_nested_transaction_is_refused(server_driver: ServerDriver) -> None:
    with server_driver.transaction():
        with pytest.raises(LedgerError, match="nested"):
            with server_driver.transaction():
                pass


def test_commit_creates_one_dolt_commit_with_message_and_author(
    server_driver: ServerDriver,
) -> None:
    table = table_name()
    server_driver.execute(f"CREATE TABLE {table} (v BIGINT)")
    server_driver.execute(f"INSERT INTO {table} VALUES (%s)", (1,))
    message = f"driver contract {table}"
    author = "Driver Tests <driver@tests.invalid>"

    commit_hash = server_driver.commit(message, author)
    assert commit_hash is not None and len(commit_hash) > 10
    assert server_driver.query(
        "SELECT message, email FROM dolt_log WHERE commit_hash = %s", (commit_hash,)
    ) == [{"message": message, "email": "driver@tests.invalid"}]


def test_commit_without_changes_returns_none(server_driver: ServerDriver) -> None:
    table = table_name()
    server_driver.execute(f"CREATE TABLE {table} (v BIGINT)")
    assert server_driver.commit(f"first {table}") is not None
    assert server_driver.commit(f"second {table}") is None


def test_commit_refuses_non_ascii_message(server_driver: ServerDriver) -> None:
    with pytest.raises(LedgerError, match="ASCII"):
        server_driver.commit("加了一支管")


def test_commit_inside_a_transaction_is_refused(server_driver: ServerDriver) -> None:
    with server_driver.transaction():
        with pytest.raises(LedgerError, match="inside an open transaction"):
            server_driver.commit("too early")


def test_execute_many_keeps_order(server_driver: ServerDriver) -> None:
    first = table_name()
    second = table_name()
    server_driver.execute_many(
        [
            f"CREATE TABLE {first} (v BIGINT)",
            f"INSERT INTO {first} VALUES (1)",
            f"CREATE TABLE {second} (v BIGINT)",
            f"INSERT INTO {second} VALUES (2)",
        ]
    )
    assert server_driver.query(f"SELECT v FROM {first}") == [{"v": 1}]
    assert server_driver.query(f"SELECT v FROM {second}") == [{"v": 2}]


# -- branches -------------------------------------------------------------


def test_branch_session_reads_and_writes_its_own_branch(
    server_driver: ServerDriver, server_env: dict[str, str]
) -> None:
    table = table_name()
    server_driver.execute(f"CREATE TABLE {table} (v BIGINT)")
    server_driver.execute(f"INSERT INTO {table} VALUES (%s)", (1,))
    assert server_driver.commit(f"branch fixture {table}") is not None
    server_driver.execute("CALL DOLT_BRANCH('feat_x')")

    branch = ServerDriver(server_driver.path, branch="feat_x", env=server_env)
    try:
        assert branch.query("SELECT active_branch() AS b") == [{"b": "feat_x"}]
        assert branch.query(f"SELECT COUNT(*) AS n FROM {table}") == [{"n": 1}]
        branch.execute(f"INSERT INTO {table} VALUES (%s)", (2,))
        assert branch.commit(f"write on feat_x {table}") is not None
    finally:
        branch.close()

    # The checked-out branch is untouched: no row, no commit.
    assert server_driver.query(f"SELECT COUNT(*) AS n FROM {table}") == [{"n": 1}]
    assert server_driver.query("SELECT active_branch() AS b") == [{"b": "main"}]
    branch_again = ServerDriver(server_driver.path, branch="feat_x", env=server_env)
    try:
        assert branch_again.query(f"SELECT COUNT(*) AS n FROM {table}") == [{"n": 2}]
    finally:
        branch_again.close()


def test_missing_branch_is_a_usage_error(
    dolt_repo: Path, server_env: dict[str, str]
) -> None:
    with pytest.raises(UsageError, match="does not exist"):
        ServerDriver(dolt_repo, branch="absent_branch", env=server_env)
    assert not owned_servers_for(dolt_repo)


def test_branch_is_validated_against_the_checked_out_database(
    server_driver: ServerDriver, server_env: dict[str, str]
) -> None:
    with pytest.raises(UsageError, match="does not exist"):
        ServerDriver(server_driver.path, branch="nope", env=server_env)
    assert server_driver.query("SELECT 1 AS ok") == [{"ok": 1}]


# -- process budget and lifecycle -----------------------------------------


def test_fifty_statements_use_one_server(
    dolt_repo: Path, server_env: dict[str, str]
) -> None:
    cache_env = {**server_env, "TETRAMARK_SERVER_CACHE": "1"}
    starts = server_start_count()
    driver = ServerDriver(dolt_repo, env=cache_env)
    try:
        table = table_name()
        driver.execute(f"CREATE TABLE {table} (id BIGINT PRIMARY KEY)")
        for index in range(50):
            driver.execute(f"INSERT INTO {table} VALUES (%s)", (index,))
        assert driver.query(f"SELECT COUNT(*) AS n FROM {table}") == [{"n": 50}]
        assert server_start_count() == starts + 1
    finally:
        driver.close()

    # A command that reopens the ledger reuses the cached server: no new start.
    reopened = ServerDriver(dolt_repo, env=cache_env)
    try:
        assert reopened.query(f"SELECT COUNT(*) AS n FROM {table}") == [{"n": 50}]
        assert server_start_count() == starts + 1
    finally:
        reopened.close()
        shutdown_all_servers()
    assert not owned_servers_for(dolt_repo)


def test_close_kills_an_uncached_server(
    dolt_repo: Path, server_env: dict[str, str]
) -> None:
    driver = ServerDriver(dolt_repo, env=server_env)  # cache=0: owned, uncached
    driver.query("SELECT 1")
    process = driver._server.process  # noqa: SLF001 - this test owns the driver
    assert process is not None and process.poll() is None
    driver.close()
    assert process.poll() is not None
    assert not owned_servers_for(dolt_repo)


def test_cache_eviction_kills_the_least_recently_used_server(
    tmp_path: Path, make_ledger: Any, dolt_env: dict[str, str]
) -> None:
    env = {**dolt_env, "TETRAMARK_SERVER_CACHE": "1"}
    servers: list[Any] = []
    drivers: list[ServerDriver] = []
    try:
        for index in range(3):
            repo = make_ledger(tmp_path / f"cache-{index}")
            driver = ServerDriver(repo, env=env)
            driver.query("SELECT 1 AS one")
            servers.append(driver._server.process)  # noqa: SLF001
            drivers.append(driver)
            # Commands close their ledger: the server stays cached and idle.
            driver.close()
        # Each new ledger evicted the previous idle server.
        assert servers[0].poll() is not None
        assert servers[1].poll() is not None
        # The most recent one is cached: it stays alive for the next open...
        assert servers[2].poll() is None
        assert cached_server_count() == 1
    finally:
        for driver in drivers:
            driver.close()
        shutdown_all_servers()
    # ... and the session-finish/exiting cleanup kills it.
    assert servers[2].poll() is not None
    assert cached_server_count() == 0


def test_replacing_the_directory_at_a_path_restarts_the_server(
    tmp_path: Path, make_ledger: Any, dolt_env: dict[str, str]
) -> None:
    """A stale cache entry must not serve a directory that was replaced."""
    env = {**dolt_env, "TETRAMARK_SERVER_CACHE": "1"}
    path = tmp_path / "replaced"
    make_ledger(path)
    first = ServerDriver(path, env=env)
    try:
        first.query("SELECT 1 AS one")
        old_process = first._server.process  # noqa: SLF001
    finally:
        first.close()
    assert old_process.poll() is None  # cached for reuse

    shutil.rmtree(path)
    make_ledger(path)  # same path, new directory inode, empty database
    starts = server_start_count()
    second = ServerDriver(path, env=env)
    try:
        assert second.query("SELECT 1 AS one") == [{"one": 1}]
        # The old server is gone and a new one was started for the new inode.
        assert server_start_count() == starts + 1
        assert old_process.poll() is not None
    finally:
        second.close()
        shutdown_all_servers()


def test_discard_stops_the_cached_server(
    dolt_repo: Path, dolt_env: dict[str, str]
) -> None:
    env = {**dolt_env, "TETRAMARK_SERVER_CACHE": "1"}
    driver = ServerDriver(dolt_repo, env=env)
    driver.query("SELECT 1 AS one")
    process = driver._server.process  # noqa: SLF001
    driver.discard()
    assert process.poll() is not None
    assert not owned_servers_for(dolt_repo)


def test_a_subprocess_can_borrow_the_running_server(
    dolt_repo: Path, server_env: dict[str, str]
) -> None:
    """CLI/migration subprocesses reuse the parent process's server.

    A database directory allows only one ``dolt sql-server``; without borrowing
    through Dolt's ``.dolt/sql-server.info`` a subprocess would fail to start a
    second one.  The subprocess writes through the borrowed server and the
    parent sees the rows.
    """
    driver = ServerDriver(dolt_repo, env=server_env)
    try:
        driver.execute("CREATE TABLE borrowed (v BIGINT)")
        driver.execute("INSERT INTO borrowed VALUES (1)")
        assert driver.commit("parent row") is not None

        code = (
            "import sys\n"
            "from tetramark.driver.server import ServerDriver\n"
            "driver = ServerDriver(sys.argv[1], env=None)\n"
            "try:\n"
            "    print(driver.query('SELECT COUNT(*) AS n FROM borrowed'))\n"
            "    driver.execute('INSERT INTO borrowed VALUES (2)')\n"
            "    print(driver.commit('child row'))\n"
            "finally:\n"
            "    driver.close()\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code, str(dolt_repo)],
            env=server_env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "[{'n': 1}]" in result.stdout
        # The parent's server is still alive and sees the child's commit.
        assert driver.query("SELECT COUNT(*) AS n FROM borrowed") == [{"n": 2}]
        assert driver.query(
            "SELECT 1 AS found FROM dolt_log WHERE message = 'child row'"
        ) == [{"found": 1}]
    finally:
        driver.close()
    assert not owned_servers_for(dolt_repo)


def test_shutdown_is_idempotent(dolt_repo: Path, server_env: dict[str, str]) -> None:
    driver = ServerDriver(dolt_repo, env=server_env)
    driver.query("SELECT 1")
    driver.close()
    shutdown_all_servers()
    shutdown_all_servers()
    assert cached_server_count() == 0
