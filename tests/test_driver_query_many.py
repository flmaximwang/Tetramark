"""Contract tests for ``Driver.query_many`` (the batched read API).

``query_many`` must return one result list per statement, in order, with a
zero-row statement represented by an empty list (not dropped), and with the
columns Dolt omits for ``NULL`` values restored.  The embedded backend is
expected to pay at most two ``dolt sql`` processes for a whole batch (one
``-r json`` for the rows, one ``-r csv`` for the column lists), so these tests
also count ``_invoke`` calls: a regression to one process per statement must
fail here.  Real Dolt, never a mock, like the rest of the suite.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from tetramark.driver import Driver, EmbeddedDriver
from tetramark.errors import LedgerError


@pytest.fixture
def embedded_driver(dolt_repo: Path, dolt_env: dict[str, str]) -> EmbeddedDriver:
    return EmbeddedDriver(dolt_repo, env=dolt_env)


@pytest.fixture
def driver(embedded_driver: EmbeddedDriver) -> Driver:
    """The embedded driver; the contract is backend-agnostic."""
    return embedded_driver


def table_name() -> str:
    return f"batch_{uuid.uuid4().hex[:10]}"


def make_table(driver: Driver) -> str:
    """A small table with one fully populated row and one with a NULL value."""
    table = table_name()
    driver.execute(
        f"CREATE TABLE {table} ("
        "id BIGINT PRIMARY KEY, name VARCHAR(30), note TEXT)"
    )
    driver.execute(
        f"INSERT INTO {table} (id, name, note) VALUES (%s, %s, %s)",
        (1, "alpha", "first"),
    )
    driver.execute(
        f"INSERT INTO {table} (id, name, note) VALUES (%s, %s, %s)",
        (2, "beta", None),
    )
    return table


# ---------------------------------------------------------------------------
# alignment
# ---------------------------------------------------------------------------


def test_query_many_returns_one_result_per_statement_in_order(driver: Driver) -> None:
    table = make_table(driver)

    results = driver.query_many(
        [
            (f"SELECT id FROM {table} ORDER BY id", None),
            (f"SELECT name FROM {table} WHERE id = %s", (2,)),
            (f"SELECT COUNT(*) AS n FROM {table}", None),
        ]
    )

    assert results == [
        [{"id": 1}, {"id": 2}],
        [{"name": "beta"}],
        [{"n": 2}],
    ]


def test_query_many_keeps_zero_row_statements_aligned(driver: Driver) -> None:
    table = make_table(driver)

    results = driver.query_many(
        [
            # Empty results at the start, middle and end of the batch: `{}` is
            # a real (empty) result set and must not be dropped or shift the
            # statements that follow it.
            (f"SELECT id FROM {table} WHERE id > 100", None),
            (f"SELECT id FROM {table} WHERE id = 2", None),
            (f"SELECT id FROM {table} WHERE id > 100", None),
            (f"SELECT COUNT(*) AS n FROM {table}", None),
            (f"SELECT id FROM {table} ORDER BY id DESC", None),
            (f"SELECT id FROM {table} WHERE id > 100", None),
        ]
    )

    assert [len(rows) for rows in results] == [0, 1, 0, 1, 2, 0]
    assert results[1] == [{"id": 2}]
    assert results[3] == [{"n": 2}]
    assert results[4] == [{"id": 2}, {"id": 1}]


def test_query_many_handles_a_batch_that_is_entirely_empty(driver: Driver) -> None:
    table = make_table(driver)

    results = driver.query_many(
        [
            (f"SELECT id FROM {table} WHERE id > 100", None),
            (f"SELECT name FROM {table} WHERE id > 100", None),
        ]
    )

    assert results == [[], []]


# ---------------------------------------------------------------------------
# NULL-restoration
# ---------------------------------------------------------------------------


def test_query_many_restores_columns_dolt_omits_for_null(driver: Driver) -> None:
    table = make_table(driver)

    results = driver.query_many(
        [
            # Deliberately not alphabetical: JSON sorts keys, so a parser that
            # zips header order with dict values would corrupt these rows.
            (f"SELECT note, name, id FROM {table} ORDER BY id", None),
            ("SELECT NULL AS only_null, 'present' AS present", None),
            ("SELECT NULL AS a, NULL AS b", None),
        ]
    )

    assert results[0] == [
        {"id": 1, "name": "alpha", "note": "first"},
        {"id": 2, "name": "beta", "note": None},
    ]
    assert results[1] == [{"only_null": None, "present": "present"}]
    assert results[2] == [{"a": None, "b": None}]


# ---------------------------------------------------------------------------
# values
# ---------------------------------------------------------------------------


def test_query_many_round_trips_tricky_values(driver: Driver) -> None:
    table = table_name()
    driver.execute(
        f"CREATE TABLE {table} (id BIGINT PRIMARY KEY, note TEXT)"
    )
    tricky = (
        "quote's \"double\" `tick` \\backslash\\ 台账 管 🧪\n"
        "second line %s and 100%% -- not a comment; DROP TABLE boom"
    )
    for row_id, note in ((1, tricky), (2, ""), (3, None)):
        driver.execute(
            f"INSERT INTO {table} (id, note) VALUES (%s, %s)", (row_id, note)
        )

    results = driver.query_many(
        [
            (f"SELECT id, note FROM {table} ORDER BY id", None),
            (f"SELECT COUNT(*) AS n FROM {table} WHERE note IS NULL", None),
        ]
    )

    assert [row["note"] for row in results[0]] == [tricky, "", None]
    assert results[1] == [{"n": 1}]


# ---------------------------------------------------------------------------
# branch sessions
# ---------------------------------------------------------------------------


def test_query_many_reads_the_branch_session(
    embedded_driver: EmbeddedDriver, dolt_env: dict[str, str]
) -> None:
    base = embedded_driver
    table = table_name()
    base.execute(f"CREATE TABLE {table} (v BIGINT)")
    base.execute(f"INSERT INTO {table} VALUES (%s)", (1,))
    assert base.commit(f"query_many branch fixture {table}") is not None

    branch_name = f"feat_{uuid.uuid4().hex[:8]}"
    base.execute("CALL DOLT_BRANCH(%s)", (branch_name,))
    branch = EmbeddedDriver(base.path, branch=branch_name, env=dolt_env)
    try:
        branch.execute(f"INSERT INTO {table} VALUES (%s)", (2,))
        assert branch.commit(f"query_many branch row {table}") is not None

        results = branch.query_many(
            [
                (f"SELECT v FROM {table} ORDER BY v", None),
                (f"SELECT v FROM {table} WHERE v > 100", None),
                (f"SELECT COUNT(*) AS n FROM {table}", None),
            ]
        )

        assert results[0] == [{"v": 1}, {"v": 2}]
        assert results[1] == []
        assert results[2] == [{"n": 2}]
        # The batch's USE selected the revision database for its own sessions
        # only: the checked-out branch still sees one row.
        assert base.query(f"SELECT v FROM {table} ORDER BY v") == [{"v": 1}]
    finally:
        close = getattr(branch, "close", None)
        if callable(close):
            close()


# ---------------------------------------------------------------------------
# transactions
# ---------------------------------------------------------------------------


def test_query_many_previews_pending_transaction_writes(driver: Driver) -> None:
    table = table_name()
    driver.execute(f"CREATE TABLE {table} (v BIGINT)")
    with driver.transaction():
        driver.execute(f"INSERT INTO {table} VALUES (%s)", (1,))
        results = driver.query_many(
            [
                (f"SELECT COUNT(*) AS n FROM {table}", None),
                (f"SELECT v FROM {table} WHERE v > 100", None),
            ]
        )
        assert results == [[{"n": 1}], []]
    assert driver.query(f"SELECT COUNT(*) AS n FROM {table}") == [{"n": 1}]


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------


def test_query_many_propagates_a_statement_error(driver: Driver) -> None:
    table = make_table(driver)

    with pytest.raises(LedgerError, match="(?i)not found"):
        driver.query_many(
            [
                (f"SELECT id FROM {table} ORDER BY id", None),
                ("SELECT * FROM definitely_missing_table", None),
                ("SELECT 1 AS a", None),
            ]
        )


# ---------------------------------------------------------------------------
# cost: one process for the batch, not one per statement
# ---------------------------------------------------------------------------


def test_query_many_costs_at_most_two_dolt_processes(
    driver: Driver, monkeypatch: pytest.MonkeyPatch
) -> None:
    table = make_table(driver)
    calls: list[str] = []
    original = EmbeddedDriver._invoke

    def counted(
        self: EmbeddedDriver, sql: str, result_format: str
    ) -> str:
        calls.append(result_format)
        return original(self, sql, result_format)

    monkeypatch.setattr(EmbeddedDriver, "_invoke", counted)

    results = driver.query_many(
        [
            (f"SELECT id, name FROM {table} ORDER BY id", None),
            (f"SELECT id FROM {table} WHERE id > 100", None),
            (f"SELECT COUNT(*) AS n FROM {table}", None),
            (f"SELECT name FROM {table} WHERE name = 'missing'", None),
            (f"SELECT note FROM {table} WHERE id = 1", None),
        ]
    )

    assert results[0] == [
        {"id": 1, "name": "alpha"},
        {"id": 2, "name": "beta"},
    ]
    assert results[1] == []
    assert results[2] == [{"n": 2}]
    assert results[3] == []
    assert results[4] == [{"note": "first"}]
    assert calls == ["json", "csv"], calls

    # An all-empty batch needs no CSV run, but must stay aligned and cheap.
    calls.clear()
    assert driver.query_many(
        [
            (f"SELECT id FROM {table} WHERE id > 100", None),
            (f"SELECT id FROM {table} WHERE id > 100", None),
        ]
    ) == [[], []]
    assert len(calls) <= 2, calls
