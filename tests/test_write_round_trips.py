"""Write-side process budget: one transaction flush + one Dolt commit.

Tetramark-0i8.7.  A successful ``WriteSession`` used to pay two ``dolt sql``
processes: one for ``START TRANSACTION; <buffered DML>; COMMIT`` and one for
``DOLT_ADD`` + ``DOLT_COMMIT``.  These tests count real
``EmbeddedDriver._invoke`` calls -- each one is an actual Dolt subprocess -- and
pin the combined path to one process, while checking that everything the user
observes stays the same: the commit message, author and hash, the lock release,
``no_commit``/``dry_run``, DDL ordering and the rollback/error behaviour.

Nothing is mocked and no server is used: every ledger is a real copy of the
session template and every statement runs against Dolt.  The public
``Driver.transaction()``/``commit()`` pair is exercised directly at the bottom:
only ``WriteSession`` was allowed to move to the combined process.
"""

from __future__ import annotations

import pytest

from tetramark.core.ledger import Ledger
from tetramark.core.writelock import lock_path, write_lock_holder
from tetramark.driver import EmbeddedDriver
from tetramark.errors import LedgerError

from conftest import FACTORY_AUTHOR, PRESETS

#: These tests pin budgets by counting the embedded driver's ``dolt sql``
#: processes; the opt-in server backend has no such per-statement process, so
#: ``tests/conftest.py`` skips them when ``TETRAMARK_DRIVER=server``.
pytestmark = pytest.mark.embedded_driver


class Invocations:
    """Every ``dolt sql`` process the embedded driver starts, in order."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[tuple[str, str]] = []  # (SQL script, result format)
        original = EmbeddedDriver._invoke

        def recorded(
            driver: EmbeddedDriver, sql: str, result_format: str
        ) -> str:
            self.calls.append((sql, result_format))
            return original(driver, sql, result_format)

        monkeypatch.setattr(EmbeddedDriver, "_invoke", recorded)

    def reset(self) -> None:
        self.calls.clear()

    @property
    def count(self) -> int:
        return len(self.calls)

    def sql(self) -> list[str]:
        return [sql for sql, _result_format in self.calls]


def commit_count(ledger: Ledger) -> int:
    return int(ledger.driver.query("SELECT COUNT(*) AS n FROM dolt_log")[0]["n"])


def latest_commit(ledger: Ledger) -> dict[str, object]:
    return ledger.driver.query(
        "SELECT message, author_email FROM dolt_log "
        "ORDER BY date DESC, commit_order DESC LIMIT 1"
    )[0]


def test_one_dml_write_session_is_one_process(
    ledger_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The flush and the Dolt commit share one ``dolt sql`` script."""
    ledger: Ledger = ledger_factory([PRESETS / "base"])
    ledger.driver.execute("CREATE TABLE write_probe (id BIGINT)")
    before = commit_count(ledger)
    calls = Invocations(monkeypatch)

    with ledger.write("write probe", verb="add", object="box") as session:
        session.execute("INSERT INTO write_probe (id) VALUES (%s)", (1,))

    assert calls.count == 1, calls.sql()
    script, result_format = calls.calls[0]
    assert result_format == "json"
    # One process: transaction, buffered DML, SQL commit, then the Dolt commit.
    assert script.index("START TRANSACTION") < script.index("INSERT INTO write_probe")
    assert script.index("INSERT INTO write_probe") < script.index("COMMIT;")
    assert script.index("COMMIT;") < script.index("DOLT_ADD('-A')")
    assert script.index("DOLT_ADD('-A')") < script.index("DOLT_COMMIT(")
    assert session.commit_hash

    calls.reset()
    assert commit_count(ledger) == before + 1
    latest = latest_commit(ledger)
    assert latest["message"] == "add(box): write probe"
    assert latest["author_email"] == "factory@tetramark.invalid"
    assert ledger.driver.query("SELECT id FROM write_probe") == [{"id": 1}]
    # The reported hash is the real Dolt commit, not a made-up value.
    assert ledger.driver.query(
        "SELECT message FROM dolt_log WHERE commit_hash = %s", (session.commit_hash,)
    ) == [{"message": "add(box): write probe"}]


def test_no_commit_write_session_flushes_in_one_process(
    ledger_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``no_commit`` keeps today's one-process flush and makes no commit."""
    ledger: Ledger = ledger_factory([PRESETS / "base"])
    ledger.driver.execute("CREATE TABLE write_probe (id BIGINT)")
    before = commit_count(ledger)
    calls = Invocations(monkeypatch)

    with ledger.write(
        "uncommitted probe", verb="add", object="box", no_commit=True
    ) as session:
        session.execute("INSERT INTO write_probe (id) VALUES (%s)", (2,))

    assert calls.count == 1, calls.sql()
    assert "START TRANSACTION" in calls.sql()[0]
    assert "INSERT INTO write_probe" in calls.sql()[0]
    assert "DOLT_COMMIT" not in calls.sql()[0]
    assert session.commit_hash is None

    calls.reset()
    assert commit_count(ledger) == before
    # Executed, just not committed in Dolt's history.
    assert ledger.driver.query("SELECT id FROM write_probe") == [{"id": 2}]


def test_dry_run_write_session_starts_no_process(
    ledger_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dry run only collects SQL: no Dolt process, no lock, no commit."""
    ledger: Ledger = ledger_factory([PRESETS / "base"])
    before = commit_count(ledger)
    calls = Invocations(monkeypatch)

    with ledger.write(
        "dry probe", verb="add", object="box", dry_run=True
    ) as session:
        session.execute("INSERT INTO never_created (id) VALUES (%s)", (3,))

    assert calls.count == 0, calls.sql()
    assert "INSERT INTO never_created" in session.sql
    assert session.commit_hash is None
    assert not lock_path(ledger.path).exists()

    calls.reset()
    assert commit_count(ledger) == before


def test_ddl_then_dml_write_session_is_two_processes(
    ledger_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A DDL batch plus one DML/commit process: the schema is still first."""
    ledger: Ledger = ledger_factory([PRESETS / "base"])
    before = commit_count(ledger)
    calls = Invocations(monkeypatch)

    with ledger.write("ddl probe", verb="add", object="box") as session:
        # DDL is deferred until the INSERT flushes the batch before it, so the
        # CREATE reaches Dolt first; the INSERT and the Dolt commit share the
        # second (and last) process even though they were issued separately.
        session.execute("CREATE TABLE ddl_probe (id BIGINT)")
        session.execute("INSERT INTO ddl_probe (id) VALUES (%s)", (7,))

    assert calls.count == 2, calls.sql()
    ddl_script, dml_script = calls.sql()
    # DDL: its own batch, statements marked, no transaction and no commit.
    assert "TM_STMT_" in ddl_script
    assert "CREATE TABLE ddl_probe" in ddl_script
    assert "START TRANSACTION" not in ddl_script
    assert "DOLT_COMMIT" not in ddl_script
    # DML: transaction + insert + SQL commit + the Dolt commit, all in one.
    assert dml_script.index("START TRANSACTION") < dml_script.index(
        "INSERT INTO ddl_probe"
    )
    assert dml_script.index("COMMIT;") < dml_script.index("DOLT_COMMIT(")
    assert session.commit_hash

    calls.reset()
    assert commit_count(ledger) == before + 1
    assert ledger.driver.query("SELECT id FROM ddl_probe") == [{"id": 7}]


def test_write_session_body_failure_executes_and_commits_nothing(
    ledger_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A body exception discards the buffered DML before it is ever sent."""
    ledger: Ledger = ledger_factory([PRESETS / "base"])
    ledger.driver.execute("CREATE TABLE failure_probe (id BIGINT)")
    before = commit_count(ledger)
    calls = Invocations(monkeypatch)

    with pytest.raises(RuntimeError, match="boom"):
        with ledger.write("failing probe", verb="add", object="box") as session:
            session.execute("INSERT INTO failure_probe (id) VALUES (%s)", (1,))
            assert calls.count == 0  # not even the transaction flush ran
            raise RuntimeError("boom")

    assert calls.count == 0, calls.sql()
    calls.reset()
    assert commit_count(ledger) == before
    assert ledger.driver.query("SELECT COUNT(*) AS n FROM failure_probe") == [{"n": 0}]
    assert write_lock_holder(ledger) is None


def test_failing_statement_is_a_ledger_error_and_commits_nothing(
    ledger_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Dolt error in the combined script still surfaces as a LedgerError.

    The DML is buffered, so the error appears when the session closes; nothing
    is left committed, the write lock is released and the ledger stays usable.
    """
    ledger: Ledger = ledger_factory([PRESETS / "base"])
    before = commit_count(ledger)
    calls = Invocations(monkeypatch)

    with pytest.raises(LedgerError, match="(?i)not found"):
        with ledger.write("broken probe", verb="add", object="box") as session:
            session.execute("INSERT INTO missing_probe (id) VALUES (%s)", (1,))

    assert session.commit_hash is None
    # The one process carried the flush and the commit attempt; it failed.
    assert calls.count == 1, calls.sql()
    calls.reset()
    assert commit_count(ledger) == before
    assert write_lock_holder(ledger) is None

    # The driver's state is clean again: a later write session works.
    ledger.driver.execute("CREATE TABLE after_failure (id BIGINT)")
    with ledger.write("after the failure", verb="add", object="box") as session:
        session.execute("INSERT INTO after_failure (id) VALUES (%s)", (2,))
    assert session.commit_hash
    assert ledger.driver.query("SELECT id FROM after_failure") == [{"id": 2}]


def test_nothing_to_commit_returns_no_hash(
    ledger_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A net-zero transaction makes no Dolt commit, like the two-process path."""
    ledger: Ledger = ledger_factory([PRESETS / "base"])
    ledger.driver.execute("CREATE TABLE noop_probe (id BIGINT)")
    # Commit the table first: only a clean working set makes the session's
    # INSERT+DELETE pair net to "nothing to commit".
    assert ledger.driver.commit("seed noop probe") is not None
    before = commit_count(ledger)
    calls = Invocations(monkeypatch)

    with ledger.write("net zero probe", verb="update", object="ledger") as session:
        session.execute("INSERT INTO noop_probe (id) VALUES (%s)", (4,))
        session.execute("DELETE FROM noop_probe WHERE id = %s", (4,))

    assert calls.count == 1, calls.sql()
    assert "DOLT_COMMIT(" in calls.sql()[0]
    assert session.commit_hash is None

    calls.reset()
    assert commit_count(ledger) == before
    assert ledger.driver.query("SELECT COUNT(*) AS n FROM noop_probe") == [{"n": 0}]


def test_entities_add_is_one_read_plus_one_write_process(
    ledger_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real write command: the ID read, then one DML/commit process."""
    ledger: Ledger = ledger_factory([PRESETS / "base"])
    before = commit_count(ledger)
    calls = Invocations(monkeypatch)

    created = ledger.entities.add("item", name="probe")

    assert calls.count == 2, calls.sql()
    # First the next-ID read, then the whole write: no separate flush process.
    assert "SELECT" in calls.sql()[0].upper()
    assert "START TRANSACTION" in calls.sql()[1]
    assert "DOLT_COMMIT(" in calls.sql()[1] and "DOLT_ADD" in calls.sql()[1]
    assert created["name"] == "probe"

    calls.reset()
    assert commit_count(ledger) == before + 1
    latest = latest_commit(ledger)
    assert latest["message"] == f"add(entity): item {created['id']}"
    assert latest["author_email"] == "factory@tetramark.invalid"


def test_direct_driver_transaction_and_commit_still_work(
    ledger_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The public pair is unchanged for callers that do not use WriteSession."""
    ledger: Ledger = ledger_factory([PRESETS / "base"])
    driver = ledger.driver
    driver.execute("CREATE TABLE direct_probe (id BIGINT)")
    before = commit_count(ledger)
    calls = Invocations(monkeypatch)

    with driver.transaction():
        driver.execute("INSERT INTO direct_probe (id) VALUES (%s)", (1,))
        driver.execute("INSERT INTO direct_probe (id) VALUES (%s)", (2,))
        # Still a preview inside the deferred transaction (one process: the
        # batch is replayed in a rolled-back transaction; fill_columns=False
        # skips the CSV header run).
        assert driver.query(
            "SELECT COUNT(*) AS n FROM direct_probe", fill_columns=False
        ) == [{"n": 2}]
    assert calls.count == 2, calls.sql()  # the query preview + the transaction flush
    assert "START TRANSACTION" in calls.sql()[1]

    calls.reset()
    hash_ = driver.commit("direct write", author=FACTORY_AUTHOR)
    assert hash_ is not None
    assert calls.count == 1, calls.sql()  # a direct commit is still one process
    assert commit_count(ledger) == before + 1
    assert driver.query("SELECT id FROM direct_probe ORDER BY id") == [
        {"id": 1},
        {"id": 2},
    ]
