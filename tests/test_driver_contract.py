"""The driver contract, run against the real embedded backend (never a mock).

Each test runs in a fresh ``dolt init`` repository (or a session copy of one)
and uses its own table name.  The contract is the :class:`Driver` ABC, so a
future server driver can be held to the same tests.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from tetramark.driver import Driver, EmbeddedDriver
from tetramark.errors import LedgerError, UnreachableError, UsageError

#: These tests pin budgets by counting the embedded driver's ``dolt sql``
#: processes; the opt-in server backend has no such per-statement process, so
#: the ``embedded_driver`` marker makes conftest switch them to that
#: backend even though the rest of the session defaults to the server.
pytestmark = pytest.mark.embedded_driver

# -- fixtures -------------------------------------------------------------


@pytest.fixture
def embedded_driver(dolt_repo: Path, dolt_env: dict[str, str]) -> EmbeddedDriver:
    return EmbeddedDriver(dolt_repo, env=dolt_env)


@pytest.fixture
def driver(embedded_driver: EmbeddedDriver) -> Driver:
    """The embedded driver; the contract is backend-agnostic."""
    return embedded_driver


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


# -- batched reads --------------------------------------------------------


def test_query_many_keeps_one_result_per_statement(driver: Driver) -> None:
    table = table_name()
    driver.execute(
        f"CREATE TABLE {table} (id BIGINT PRIMARY KEY, label VARCHAR(255))"
    )
    driver.execute(f"INSERT INTO {table} VALUES (%s, %s)", (1, "one"))
    driver.execute(f"INSERT INTO {table} VALUES (%s, %s)", (2, None))

    results = driver.query_many(
        [
            (f"SELECT id, label FROM {table} ORDER BY id", None),
            ("SELECT 1 AS n WHERE 1 = 0", None),
            (f"SELECT COUNT(*) AS n FROM {table}", None),
            ("SELECT NULL AS only_null", None),
        ]
    )
    assert len(results) == 4
    # The NULL-only column is restored even though JSON omitted it, and the
    # zero-row statement still owns an (empty) entry at its position.
    assert results[0] == [{"id": 1, "label": "one"}, {"id": 2, "label": None}]
    assert results[1] == []
    assert results[2] == [{"n": 2}]
    assert results[3] == [{"only_null": None}]


def test_query_many_propagates_errors(driver: Driver) -> None:
    with pytest.raises(LedgerError, match="(?i)not found"):
        driver.query_many(
            [
                ("SELECT 1 AS ok", None),
                ("SELECT * FROM definitely_missing_table", None),
            ]
        )


def test_embedded_query_many_runs_json_and_csv_once(
    dolt_repo: Path, dolt_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = EmbeddedDriver(dolt_repo, env=dolt_env)
    calls: list[str] = []
    original = EmbeddedDriver._invoke

    def counting(
        self: EmbeddedDriver, sql: str, result_format: str
    ) -> str:
        calls.append(result_format)
        return original(self, sql, result_format)

    monkeypatch.setattr(EmbeddedDriver, "_invoke", counting)
    results = driver.query_many(
        [
            ("SELECT 1 AS a, NULL AS b", None),
            ("SELECT 2 AS c WHERE 1 = 0", None),
            ("SELECT 3 AS d", None),
        ]
    )
    assert results == [[{"a": 1, "b": None}], [], [{"d": 3}]]
    # One JSON batch for the rows, one CSV batch for the column headers.
    assert calls == ["json", "csv"]


# -- backend specifics ----------------------------------------------------


def test_embedded_rejects_a_directory_that_is_not_a_repository(tmp_path: Path) -> None:
    (tmp_path / "not-a-repo").mkdir()
    with pytest.raises(UnreachableError, match="not a Dolt database"):
        EmbeddedDriver(tmp_path / "not-a-repo")


def test_embedded_reports_dolt_errors(dolt_repo: Path, dolt_env: dict[str, str]) -> None:
    driver = EmbeddedDriver(dolt_repo, env=dolt_env)
    with pytest.raises(LedgerError, match="table not found"):
        driver.query("SELECT * FROM nope")


# -- branch selection (revision database) ---------------------------------


@dataclass
class BranchPair:
    """A checked-out session plus a session on a new branch of the same repo."""

    base: Driver
    branch: Driver
    name: str
    table: str


@pytest.fixture
def branch_pair(
    embedded_driver: EmbeddedDriver, dolt_env: dict[str, str]
) -> Iterator[BranchPair]:
    """A branch created from HEAD, with one committed row on the checked-out branch."""
    table = table_name()
    base: Driver = embedded_driver
    base.execute(f"CREATE TABLE {table} (v BIGINT)")
    base.execute(f"INSERT INTO {table} VALUES (%s)", (1,))
    assert base.commit(f"branch fixture {table}") is not None

    name = f"feat_{uuid.uuid4().hex[:8]}"
    base.execute("CALL DOLT_BRANCH(%s)", (name,))
    branch: Driver = EmbeddedDriver(embedded_driver.path, branch=name, env=dolt_env)
    try:
        yield BranchPair(base=base, branch=branch, name=name, table=table)
    finally:
        close = getattr(branch, "close", None)
        if callable(close):
            close()


def test_branch_writes_leave_the_checked_out_branch_unchanged(
    branch_pair: BranchPair,
) -> None:
    pair = branch_pair
    table = pair.table
    assert pair.branch.query(f"SELECT COUNT(*) AS n FROM {table}") == [{"n": 1}]

    pair.branch.execute(f"INSERT INTO {table} VALUES (%s)", (2,))
    message = f"write on {pair.name}"
    assert pair.branch.commit(message) is not None

    # The checked-out branch neither sees the row nor the commit.
    assert pair.base.query(f"SELECT COUNT(*) AS n FROM {table}") == [{"n": 1}]
    assert pair.base.query(
        "SELECT 1 AS `found` FROM dolt_log WHERE `message` = %s", (message,)
    ) == []
    # ... and the commit landed on the branch tip, visible as of the branch.
    assert pair.branch.query(f"SELECT COUNT(*) AS n FROM {table}") == [{"n": 2}]
    assert pair.base.query(
        f"SELECT COUNT(*) AS n FROM {table} AS OF %s", (pair.name,)
    ) == [{"n": 2}]
    # No session ever moved the repository's checkout: the base session still
    # reports main, while the branch session's own checkout is the branch.
    assert pair.base.query("SELECT active_branch() AS `b`") == [{"b": "main"}]
    assert pair.branch.query("SELECT active_branch() AS `b`") == [{"b": pair.name}]


def test_two_sessions_read_in_parallel_without_crossing(
    branch_pair: BranchPair,
) -> None:
    pair = branch_pair
    table = pair.table
    pair.branch.execute(f"INSERT INTO {table} VALUES (%s)", (2,))
    assert pair.branch.commit("branch row") is not None
    pair.base.execute(f"INSERT INTO {table} VALUES (%s)", (3,))
    assert pair.base.commit("main row") is not None

    # Interleaved reads: each session sees exactly its own branch.
    assert pair.base.query(f"SELECT v FROM {table} ORDER BY v") == [
        {"v": 1},
        {"v": 3},
    ]
    assert pair.branch.query(f"SELECT v FROM {table} ORDER BY v") == [
        {"v": 1},
        {"v": 2},
    ]
    assert pair.base.query(f"SELECT COUNT(*) AS n FROM {table}") == [{"n": 2}]
    assert pair.branch.query(f"SELECT COUNT(*) AS n FROM {table}") == [{"n": 2}]
    assert pair.base.query("SELECT active_branch() AS `b`") == [{"b": "main"}]


def test_query_many_reads_the_session_branch(branch_pair: BranchPair) -> None:
    pair = branch_pair
    pair.branch.execute(f"INSERT INTO {pair.table} VALUES (%s)", (2,))
    assert pair.branch.commit("branch batch row") is not None

    results = pair.branch.query_many(
        [
            (f"SELECT COUNT(*) AS n FROM {pair.table}", None),
            ("SELECT active_branch() AS `b`", None),
        ]
    )
    assert results == [[{"n": 2}], [{"b": pair.name}]]
    # The checked-out branch is still untouched by the batch read.
    assert pair.base.query(f"SELECT COUNT(*) AS n FROM {pair.table}") == [{"n": 1}]


def test_missing_branch_is_a_usage_error(
    embedded_driver: EmbeddedDriver,
) -> None:
    missing = f"absent_{uuid.uuid4().hex[:8]}"
    with pytest.raises(UsageError, match="does not exist"):
        EmbeddedDriver(embedded_driver.path, branch=missing)
    # A missing branch is never created implicitly.
    assert embedded_driver.query(
        "SELECT 1 AS `found` FROM dolt_branches WHERE `name` = %s", (missing,)
    ) == []

