"""Write-side process budget: init, migration and schema apply (Tetramark-0i8.1).

One logical write operation must not spawn one ``dolt sql`` process per DDL
statement.  These tests count real ``EmbeddedDriver._invoke`` calls -- every one
is an actual Dolt subprocess -- so a regression back to per-statement DDL fails
here: init (31 generated statements) has to fit in at most four calls, a
migration step and a schema apply in at most six.

Nothing is mocked and no server is used: the ledgers are the session's real
template copies (or one real ``init_ledger`` for the init budget) and every
statement runs against Dolt.  The ``Driver.execute_many`` contract (one process,
order preserved, failing statement named without replaying the batch) is
covered at the bottom.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

import tetramark.migrate as migrate_module
from tetramark.core.ledger import Ledger, init_ledger
from tetramark.driver import EmbeddedDriver
from tetramark.errors import LedgerError
from tetramark.migrate import MigrationStep
from tetramark.migrate import run as run_migration
from tetramark.schema.ddl import view_statements
from tetramark.schema.diff import apply, plan
from tetramark.schema.model import SPEC_VERSION
from tetramark.schema.tests_gen import generate_tests

from conftest import FACTORY_AUTHOR, PRESETS


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

    def one_script_with(self, statements: int) -> list[str]:
        """The SQL scripts that carried exactly ``statements`` batch markers."""
        marker = "TM_STMT_"
        return [sql for sql in self.sql() if sql.count(marker) == statements]


# ---------------------------------------------------------------------------
# Budget: init
# ---------------------------------------------------------------------------


def test_init_base_preset_uses_at_most_four_sql_calls(
    tmp_path: Path, dolt_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh base ledger: DDL batch, meta/tests transaction, one commit."""
    calls = Invocations(monkeypatch)
    ledger = init_ledger(
        tmp_path / "measured",
        "measured",
        [PRESETS / "base"],
        author=FACTORY_AUTHOR,
        env=dolt_env,
    )
    try:
        # 31 generated statements, but three processes: the DDL script, the
        # ledger_meta + dolt_tests transaction, and the Dolt commit.  The
        # ``dolt init`` of the directory is a separate program, not counted.
        write_calls = calls.sql()
        assert calls.count <= 4, write_calls
        assert calls.count == 3, write_calls
        # The schema script really is one process: every generated statement is
        # behind its marker in a single invocation.
        markers = [sql.count("TM_STMT_") for sql in write_calls]
        assert max(markers) >= 25, markers

        # Same database content as before the batching: one extra commit (the
        # ``dolt init`` one plus init's), the full meta and the generated tests.
        assert ledger.driver.query("SELECT COUNT(*) AS n FROM dolt_log") == [
            {"n": 2}
        ]
        assert ledger.driver.query(
            "SELECT message FROM dolt_log ORDER BY date DESC, commit_order DESC "
            "LIMIT 1"
        ) == [{"message": "init(ledger): measured"}]
        stored = {
            str(row["key"]): str(row["value"])
            for row in ledger.driver.query("SELECT `key`, `value` FROM `ledger_meta`")
        }
        assert stored == ledger.meta
        assert ledger.meta["spec_version"] == str(SPEC_VERSION)
        assert ledger.meta["created_with"] == ledger.meta["tetramark_version"]
        assert int(
            ledger.driver.query("SELECT COUNT(*) AS n FROM `dolt_tests`")[0]["n"]
        ) == len(generate_tests(ledger.schema))
    finally:
        ledger.close()


# ---------------------------------------------------------------------------
# Budget: a migration step
# ---------------------------------------------------------------------------


def test_migration_step_apply_uses_at_most_six_sql_calls(
    ledger_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recreate every generated view (the spec 1->2 shape) in a few processes."""
    ledger: Ledger = ledger_factory([PRESETS / "base"])
    views = view_statements(ledger.schema)
    statements = [f"DROP VIEW IF EXISTS `{statement.name}`" for statement in views] + [
        statement.sql for statement in views
    ]

    def build_plan(inner: Ledger) -> Sequence[str]:
        assert inner is ledger
        return statements

    step = MigrationStep(
        kind="spec",
        from_version=int(ledger.meta["spec_version"]),
        plan=build_plan,
    )

    calls = Invocations(monkeypatch)
    # Count from the write half (``_apply``) on: the plan and the printed report
    # read before it belong to the read tickets (0i8.2/0i8.3), not this budget.
    original_apply = migrate_module._apply

    def counting_apply(*args: Any, **kwargs: Any):
        calls.reset()
        return original_apply(*args, **kwargs)

    monkeypatch.setattr(migrate_module, "_apply", counting_apply)
    # The ledger is already open (with the isolated environment); the runner
    # would reopen it.  Real Dolt either way: only the wiring is shared.
    monkeypatch.setattr(migrate_module, "open_target", lambda *a, **k: ledger)

    result = run_migration(step, ledger_path=ledger.path)
    try:
        # 10+ generated statements (drops and creates) went to Dolt as one
        # script; the tag, the meta transaction, the commit and the final
        # dolt_log verification make up the rest.
        assert len(statements) >= 10
        assert calls.one_script_with(len(statements)), calls.sql()
        assert calls.count <= 6, calls.sql()
        assert result.commit_hash is not None
        assert ledger.meta["spec_version"] == str(step.to_version)
        assert ledger.driver.query(
            "SELECT COUNT(*) AS n FROM `dolt_log` WHERE `message` = %s",
            (f"migrate(ledger): spec {step.from_version} -> {step.to_version}",),
        ) == [{"n": 1}]
    finally:
        ledger.close()


# ---------------------------------------------------------------------------
# Budget: a schema apply
# ---------------------------------------------------------------------------


def test_schema_apply_multi_statement_plan_uses_at_most_six_sql_calls(
    ledger_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A multi-step plan (every generated view recreated) is one DDL batch."""
    ledger: Ledger = ledger_factory([PRESETS / "base"])
    try:
        names = [statement.name for statement in view_statements(ledger.schema)]
        assert len(names) >= 5
        for name in names:
            ledger.driver.execute(f"DROP VIEW `{name}`")
        current = plan(ledger)
        assert len(current.steps) >= 5
        assert not current.destructive

        calls = Invocations(monkeypatch)
        result = apply(ledger, schema_plan=current)

        # plan.steps are all DDL: one batch, then the ledger_meta/dolt_tests
        # transaction, the commit, and the post-apply meta read-back.
        assert calls.one_script_with(len(current.steps)), calls.sql()
        assert calls.count <= 6, calls.sql()
        assert result.applied is True
        assert result.commit_hash is not None
        assert ledger.driver.query(
            "SELECT COUNT(*) AS n FROM `dolt_log` WHERE `message` LIKE %s",
            ("apply(schema):%",),
        ) == [{"n": 1}]
        assert plan(ledger).is_empty
    finally:
        ledger.close()


# ---------------------------------------------------------------------------
# Driver.execute_many contract
# ---------------------------------------------------------------------------


@pytest.fixture
def embedded_driver(dolt_repo: Path, dolt_env: dict[str, str]) -> EmbeddedDriver:
    return EmbeddedDriver(dolt_repo, env=dolt_env)


def test_execute_many_runs_the_whole_batch_in_one_process(
    embedded_driver: EmbeddedDriver, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = embedded_driver
    driver.execute("CREATE TABLE batch_first (id BIGINT)")
    calls = Invocations(monkeypatch)

    driver.execute_many(
        [
            "CREATE TABLE batch_second (id BIGINT)",
            "CREATE TABLE batch_third (id BIGINT)",
            ("INSERT INTO batch_second (id) VALUES (%s)", (7,)),
            "ALTER TABLE batch_third ADD COLUMN label VARCHAR(20)",
        ]
    )

    assert calls.count == 1, calls.sql()
    batch = calls.sql()[0]
    # One script, in call order, with one marker per statement.
    assert batch.count("TM_STMT_") == 4, batch
    assert batch.index("batch_second (id BIGINT)") < batch.index(
        "batch_third (id BIGINT)"
    ) < batch.index("INSERT INTO batch_second") < batch.index(
        "ALTER TABLE batch_third"
    )
    # Every statement really ran, in that order.
    assert driver.query("SELECT id FROM batch_second") == [{"id": 7}]
    assert driver.query(
        "SELECT `column_name` AS name FROM `information_schema`.`columns` "
        "WHERE `table_schema` = DATABASE() AND `table_name` = 'batch_third' "
        "ORDER BY `ordinal_position`"
    ) == [{"name": "id"}, {"name": "label"}]


def test_execute_many_flushes_buffered_dml_first(
    embedded_driver: EmbeddedDriver, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inside a transaction the DML queued before the batch runs first."""
    driver = embedded_driver
    driver.execute("CREATE TABLE order_probe (id BIGINT)")
    calls = Invocations(monkeypatch)

    with driver.transaction():
        driver.execute("INSERT INTO order_probe (id) VALUES (%s)", (1,))
        driver.execute_many(
            [
                "ALTER TABLE order_probe ADD COLUMN label VARCHAR(20)",
                "UPDATE order_probe SET label = 'seen' WHERE id = 1",
            ]
        )
        assert calls.count == 2, calls.sql()
        # The buffered INSERT is one transaction; the DDL batch is its own
        # process, after it, in call order.
        assert "START TRANSACTION" in calls.sql()[0]
        assert "INSERT INTO order_probe" in calls.sql()[0]
        assert "ALTER TABLE order_probe" in calls.sql()[1]
        assert "UPDATE order_probe" in calls.sql()[1]

    assert driver.query("SELECT id, label FROM order_probe") == [
        {"id": 1, "label": "seen"}
    ]


def test_execute_many_names_the_failing_statement_without_replaying(
    embedded_driver: EmbeddedDriver, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = embedded_driver
    calls = Invocations(monkeypatch)

    with pytest.raises(LedgerError) as raised:
        driver.execute_many(
            [
                "CREATE TABLE first_ok (id BIGINT)",
                "CREATE TABLE broken (id BIGINT,)",
                "CREATE TABLE never_ran (id BIGINT)",
            ]
        )

    message = str(raised.value)
    # The failing statement is named (and its SQL given) without re-running the
    # batch: the markers say where Dolt stopped.
    assert "statement 2 of 3 failed: CREATE TABLE broken (id BIGINT,)" in message
    # Dolt's own error text is kept; the internal marker output is not.
    assert "syntax error" in message.lower()
    assert "TM_STMT" not in message
    assert calls.count == 1, calls.sql()

    # No replay: the statement before the failure is in the database once, the
    # one after it never ran.
    tables = {
        str(row["name"])
        for row in driver.query(
            "SELECT `table_name` AS name FROM `information_schema`.`tables` "
            "WHERE `table_schema` = DATABASE()"
        )
    }
    assert "first_ok" in tables
    assert "never_ran" not in tables


def test_execute_many_reports_an_empty_batch_as_a_no_op(
    embedded_driver: EmbeddedDriver, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = Invocations(monkeypatch)
    embedded_driver.execute_many([])
    assert calls.count == 0
