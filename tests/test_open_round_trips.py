"""Read-side process budget: ledger open, adopt preconditions and status.

Every CLI command pays the ledger open, and ``tm adopt``/``tm status`` used to
issue one ``dolt sql`` process per read.  These tests count real
``EmbeddedDriver._invoke`` calls -- each one is an actual Dolt subprocess, no
mock and no server -- and pin the budget (Tetramark-0i8.4):

* ``open()``: exactly one process (``ledger_meta`` read; no eager branch query);
* ``open(branch=...)``: at most two (the branch-existence row rides the same
  batch, the second process is only the failure fallback);
* the adopt precondition block: at most two (one batched ``query_many``);
* ``collect_status`` after open: at most one extra process.

The ``fill_columns`` opt-out is asserted explicitly: the default ``query``/
``query_many`` still returns every column key, including an all-``NULL`` one,
while ``fill_columns=False`` skips the CSV header run and does not restore it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tetramark.cli.status import collect_status
from tetramark.core.ledger import (
    META_CREATED_WITH,
    META_SPEC_VERSION,
    adopt_ledger,
    open as open_ledger,
)
from tetramark.driver import EmbeddedDriver
from tetramark.errors import LedgerError, UsageError
from tetramark.schema.model import SPEC_VERSION

from conftest import FACTORY_AUTHOR, PRESETS

#: These tests pin budgets by counting the embedded driver's ``dolt sql``
#: processes; the opt-in server backend has no such per-statement process, so
#: the ``embedded_driver`` marker makes conftest switch them to that
#: backend even though the rest of the session defaults to the server.
pytestmark = pytest.mark.embedded_driver


class Invocations:
    """Every ``dolt sql`` process the embedded driver starts, in order."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[tuple[str, str]] = []  # (SQL script, result format)
        original = EmbeddedDriver._invoke

        def recorded(driver: EmbeddedDriver, sql: str, result_format: str) -> str:
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


# ---------------------------------------------------------------------------
# open(): one process
# ---------------------------------------------------------------------------


def test_open_reads_ledger_meta_in_one_sql_call(
    ledger_factory, dolt_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A successful open is one ``dolt sql`` process, not two to four."""
    ledger = ledger_factory(name="open-one")
    stored = ledger.driver.query("SELECT `key`, `value` FROM `ledger_meta`")
    calls = Invocations(monkeypatch)

    opened = open_ledger(ledger.path, env=dolt_env)
    try:
        assert calls.count == 1, calls.sql()
        # The one process is the JSON meta read: no CSV header run, no eager
        # branch-existence query.
        assert calls.calls[0][1] == "json"
        assert "ledger_meta" in calls.sql()[0]
        assert "dolt_branches" not in calls.sql()[0]
        # The cache is filled from that same read (no read-back round trip).
        assert opened.meta == {str(row["key"]): str(row["value"]) for row in stored}
        assert opened.meta[META_SPEC_VERSION] == str(SPEC_VERSION)
    finally:
        opened.close()


def test_open_on_a_branch_uses_at_most_two_sql_calls_and_reads_that_branch(
    ledger_factory, dolt_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The branch-existence row rides the meta batch; meta comes from the branch."""
    ledger = ledger_factory(name="open-branch")
    ledger.driver.execute("CALL DOLT_BRANCH(%s)", ("feature",))
    branch_driver = EmbeddedDriver(ledger.path, branch="feature", env=dolt_env)
    branch_driver.execute(
        "UPDATE `ledger_meta` SET `value` = %s WHERE `key` = %s",
        ("branch-marker", META_CREATED_WITH),
    )
    assert branch_driver.commit("branch meta marker", author=FACTORY_AUTHOR)

    calls = Invocations(monkeypatch)
    opened = open_ledger(ledger.path, branch="feature", env=dolt_env)
    try:
        assert calls.count <= 2, calls.sql()
        assert opened.branch == "feature"
        # The meta rows really come from the selected revision database, not
        # from the checked-out branch.
        assert opened.meta[META_CREATED_WITH] == "branch-marker"
        ledger.read_meta()
        assert ledger.meta[META_CREATED_WITH] != "branch-marker"
        assert opened.check_fingerprint(warn=False) is True
    finally:
        opened.close()


def test_open_with_a_missing_branch_keeps_the_usage_error_and_two_calls(
    ledger_factory, dolt_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing branch is still a UsageError with the same text, in <= 2 calls."""
    ledger = ledger_factory(name="open-missing-branch")
    calls = Invocations(monkeypatch)

    with pytest.raises(UsageError, match="does not exist"):
        open_ledger(ledger.path, branch="absent", env=dolt_env)
    assert calls.count <= 2, calls.sql()
    # Nothing was created and the checked-out branch was not touched.
    assert ledger.driver.query(
        "SELECT 1 AS `found` FROM dolt_branches WHERE `name` = %s", ("absent",)
    ) == []


# ---------------------------------------------------------------------------
# adopt: one batched precondition block
# ---------------------------------------------------------------------------


def _head_hash(dolt_run, repo: Path) -> str:
    result = dolt_run(
        "sql", "-r", "csv", "-q", "SELECT DOLT_HASHOF('HEAD')", cwd=repo
    )
    return result.stdout.splitlines()[1].strip()


def test_adopt_preconditions_use_at_most_two_sql_calls(
    dolt_repo: Path, dolt_env: dict[str, str], dolt_run, monkeypatch: pytest.MonkeyPatch
) -> None:
    """dirty/schema/dolt_tests + active_branch + HEAD are one query_many."""
    calls = Invocations(monkeypatch)
    plan = adopt_ledger(
        dolt_repo,
        "repo",
        [PRESETS / "base"],
        author=FACTORY_AUTHOR,
        dry_run=True,
        env=dolt_env,
    )
    assert calls.count <= 2, calls.sql()
    # One JSON batch: no CSV header run for the NOT NULL columns.
    assert calls.count == 1, calls.sql()
    assert calls.calls[0][1] == "json"
    # Every precondition read is in that one script, in order.
    script = calls.sql()[0]
    for fragment in (
        "dolt_status",
        "information_schema.tables",
        "COUNT(*) AS `n` FROM `dolt_tests`",
        "active_branch()",
        "DOLT_HASHOF('HEAD')",
    ):
        assert fragment in script, (fragment, script)
    # The same HEAD/active branch the old separate reads returned.
    assert plan.adopted_from_commit == _head_hash(dolt_run, dolt_repo)
    assert plan.branch == "main"


def test_adopt_on_a_branch_stays_within_two_sql_calls(
    dolt_repo: Path, dolt_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = Invocations(monkeypatch)
    plan = adopt_ledger(
        dolt_repo,
        "repo",
        [PRESETS / "base"],
        author=FACTORY_AUTHOR,
        branch="main",
        dry_run=True,
        env=dolt_env,
    )
    assert calls.count <= 2, calls.sql()
    assert plan.branch == "main"


def test_adopt_refuses_a_dirty_working_set_from_the_same_batch(
    dolt_repo: Path, dolt_env: dict[str, str], dolt_run, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal order and message are unchanged by the batching."""
    dolt_run("sql", "-q", "CREATE TABLE scratch (id BIGINT)", cwd=dolt_repo)
    calls = Invocations(monkeypatch)

    with pytest.raises(LedgerError, match="working set is not clean"):
        adopt_ledger(
            dolt_repo,
            "repo",
            [PRESETS / "base"],
            author=FACTORY_AUTHOR,
            dry_run=True,
            env=dolt_env,
        )
    assert calls.count == 1, calls.sql()


def test_adopt_reports_a_missing_branch_with_at_most_two_sql_calls(
    dolt_repo: Path, dolt_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = Invocations(monkeypatch)
    with pytest.raises(UsageError, match="does not exist"):
        adopt_ledger(
            dolt_repo,
            "repo",
            [PRESETS / "base"],
            author=FACTORY_AUTHOR,
            branch="absent",
            dry_run=True,
            env=dolt_env,
        )
    assert calls.count <= 2, calls.sql()


# ---------------------------------------------------------------------------
# status: no extra read per field
# ---------------------------------------------------------------------------


def test_status_after_open_adds_at_most_one_sql_call(
    ledger_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = ledger_factory(name="status-budget")
    calls = Invocations(monkeypatch)

    payload = collect_status(ledger)

    assert calls.count <= 1, calls.sql()
    assert payload["branch"] == "main"
    assert payload["spec_version"] == SPEC_VERSION
    # The meta fields come from the open-time cache, not a new query.
    assert all("ledger_meta" not in sql for sql in calls.sql())


# ---------------------------------------------------------------------------
# fill_columns: the explicit opt-out (never silent)
# ---------------------------------------------------------------------------


def test_default_query_keeps_an_all_null_column_while_the_opt_out_drops_it(
    dolt_repo: Path, dolt_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Driver.query contract stays: all columns present unless opted out."""
    driver = EmbeddedDriver(dolt_repo, env=dolt_env)
    calls = Invocations(monkeypatch)

    rows = driver.query("SELECT NULL AS `only_null`, 1 AS `one`")
    assert rows == [{"only_null": None, "one": 1}]
    assert "only_null" in rows[0]
    # Default: the JSON row plus the CSV header run that restores NULL columns.
    assert calls.count == 2, calls.sql()

    calls.reset()
    rows = driver.query("SELECT NULL AS `only_null`, 1 AS `one`", fill_columns=False)
    # Opted out: the NULL column is simply absent, and one process was saved.
    assert rows == [{"one": 1}]
    assert "only_null" not in rows[0]
    assert calls.count == 1, calls.sql()


def test_query_many_fill_columns_opt_out_is_per_batch(
    dolt_repo: Path, dolt_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """One JSON run for the batch; the default keeps a NULL-only entry aligned."""
    driver = EmbeddedDriver(dolt_repo, env=dolt_env)
    statements = [
        ("SELECT NULL AS `only_null`", None),
        ("SELECT 1 AS `one`", None),
    ]
    calls = Invocations(monkeypatch)

    results = driver.query_many(statements)
    assert results == [[{"only_null": None}], [{"one": 1}]]
    assert calls.count == 2, calls.sql()  # JSON rows + CSV headers

    calls.reset()
    results = driver.query_many(statements, fill_columns=False)
    # Opted out: the all-NULL row survives as an empty dict (the batch stays
    # aligned) but its column key is not restored, and one process was saved.
    assert len(results) == 2
    assert results == [[{}], [{"one": 1}]]
    assert calls.count == 1, calls.sql()
