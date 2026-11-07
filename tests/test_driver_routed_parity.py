"""Real-Dolt parity for embedded reads while a user-run server is alive.

``dolt sql`` auto-routes through a live ``dolt sql-server`` on the ledger
directory, and Dolt then renders integer columns as JSON strings (``"1"``
instead of ``1``).  ``EmbeddedDriver`` restores Python ``int`` from the
same-process ``SHOW COLUMNS`` ``Type`` (Tetramark-2gz.1); these tests prove the
parity against the real Dolt binary, not a mock:

* a test-owned ``dolt sql-server`` is started on a free loopback port against
  the test's own database directory and torn down again (no leftover process).
  Since 0.8.44.1 the embedded driver keeps one writable live session, so the
  test closes that session before the server starts and then opens a fresh
  driver whose session routes through the server, mirroring the real case (a
  user server is already alive when ``tm`` runs);
* routing is *asserted*, never assumed: a raw ``dolt sql -r json`` INT column
  must come back as a string, otherwise the test fails (a skip would hide a
  broken fixture);
* the same reads run before the server exists and after it is gone, and every
  Python type + value must match (integers, unsigned bigints above ``2**63``,
  numeric-looking TEXT, DECIMAL, NULL filling, aggregates, empty results,
  ``query_many``, ``fill_columns=False``, pending-transaction previews and a
  branch session);
* the documented residual (non-``SELECT`` / probe-rejected statements) is
  pinned: those values are returned exactly as Dolt reports them and are never
  coerced.

The one-process read invariant is checked here too: the type probe is part of
the same ``dolt sql`` run, never a second process.
"""

from __future__ import annotations

import json
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

import pytest

from conftest import FACTORY_AUTHOR, isolated_env
from support.read_asserts import assert_single_read_process
from tetramark.core.ledger import init_ledger, open as open_ledger
from tetramark.driver import EmbeddedDriver
from tetramark.driver.server import ServerDriver, shutdown_all_servers

pytestmark = pytest.mark.embedded_driver

#: A value above the signed 64-bit range: the routed JSON string must become an
#: exact Python int, never a float.
UNSIGNED_BIG = 18446744073709551615


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _raw_json(dolt: str, path: Path, env: dict[str, str], sql: str) -> list[dict]:
    """The last result set of one raw ``dolt sql -r json`` run."""
    result = subprocess.run(
        [dolt, "sql", "-r", "json", "-q", sql],
        cwd=path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    payload = json.loads(lines[-1])
    return payload.get("rows", [])


@contextmanager
def routed_server(
    path: Path, env: dict[str, str], dolt: str, guard_sql: str, guard_column: str
) -> Iterator[None]:
    """Run a test-owned ``dolt sql-server`` and prove auto-routing is active.

    The server is started through the product's :class:`ServerDriver` (the only
    server lifecycle code) and stopped through :meth:`ServerDriver.discard`, so
    the test leaves no process behind and does not reimplement port/pid
    management.  Its ``<path>/.dolt/sql-server.info`` is what makes the Dolt CLI
    route ``dolt sql`` through it.  Readiness is proven by the guard query
    returning a *string* for an INTEGER column; while the port is not yet
    accepting, Dolt silently falls back to the local engine (an int), so the
    guard is polled rather than the port alone.
    """
    server = ServerDriver(path, env=env, dolt_binary=dolt)
    try:
        server.query("SELECT 1")  # starts and caches the test-owned server
        info = path / ".dolt" / "sql-server.info"
        deadline = time.monotonic() + 30.0
        routed = False
        while time.monotonic() < deadline:
            if info.is_file() and info.stat().st_size:
                rows = _raw_json(dolt, path, env, guard_sql)
                if rows and isinstance(rows[0].get(guard_column), str):
                    routed = True
                    break
            time.sleep(0.05)
        if not routed:
            pytest.fail(
                "could not establish Dolt CLI auto-routing through the "
                f"test-owned server for {path}; the routed parity proof "
                "cannot run"
            )
        yield
    finally:
        server.discard()


def assert_identical(left: Any, right: Any, where: str = "") -> None:
    """Same shape, same values *and* same Python types, recursively."""
    assert type(left) is type(right), f"{where}: {left!r} != {right!r}"
    if isinstance(left, list):
        assert len(left) == len(right), f"{where}: {left!r} != {right!r}"
        for index, (one, two) in enumerate(zip(left, right)):
            assert_identical(one, two, f"{where}[{index}]")
    elif isinstance(left, dict):
        assert set(left) == set(right), f"{where}: {left!r} != {right!r}"
        for key in left:
            assert_identical(left[key], right[key], f"{where}.{key}")
    else:
        assert left == right, f"{where}: {left!r} != {right!r}"


def make_parity_table(driver: EmbeddedDriver) -> None:
    driver.execute(
        "CREATE TABLE parity ("
        "id INT NOT NULL, "
        "big BIGINT, "
        "big_u BIGINT UNSIGNED, "
        "tiny TINYINT, "
        "neg INT, "
        "text_num VARCHAR(20), "
        "dec_v DECIMAL(10,2), "
        "nullable INT"
        ")"
    )
    driver.execute(
        "INSERT INTO parity (id, big, big_u, tiny, neg, text_num, dec_v) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (1, 9007199254740993, UNSIGNED_BIG, 7, -42, "123", "1.50"),
    )
    driver.execute(
        "INSERT INTO parity (id, big, big_u, tiny, neg, text_num, dec_v, nullable) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (2, -9007199254740993, 3, -7, -1, "0042", "2.25", 5),
    )


# ---------------------------------------------------------------------------
# query / query_many parity
# ---------------------------------------------------------------------------


def test_query_integer_parity_with_and_without_a_live_server(
    dolt_repo: Path, dolt_env: dict[str, str], dolt_binary: str
) -> None:
    driver = EmbeddedDriver(dolt_repo, env=dolt_env)
    make_parity_table(driver)

    reads: Sequence[tuple[str, None]] = [
        ("SELECT * FROM parity ORDER BY id", None),
        (
            "SELECT COUNT(*) AS n, MIN(id) AS lo, MAX(id) AS hi FROM parity",
            None,
        ),
        (
            "SELECT id + 1 AS next_id, CAST(text_num AS SIGNED) AS cast_num "
            "FROM parity WHERE id = 1",
            None,
        ),
        ("SELECT id FROM parity WHERE id > 100", None),
        ("SELECT text_num, dec_v, nullable FROM parity ORDER BY id", None),
    ]
    try:
        local = {sql: driver.query(sql) for sql, _ in reads}
    finally:
        # Since 0.8.44.1 the embedded driver holds one writable session for the
        # ledger, so it must be closed before a dolt sql-server may start on the
        # same directory (the user-run server is the thing a real read finds
        # already alive).
        driver.close()

    # The no-server baseline pins the types every routed read must reproduce.
    first = local["SELECT * FROM parity ORDER BY id"][0]
    assert type(first["id"]) is int and first["id"] == 1
    assert type(first["big_u"]) is int and first["big_u"] == UNSIGNED_BIG
    assert type(first["text_num"]) is str and first["text_num"] == "123"
    assert type(first["dec_v"]) is str and first["dec_v"] == "1.50"
    assert type(first["nullable"]) is type(None)
    assert local["SELECT COUNT(*) AS n, MIN(id) AS lo, MAX(id) AS hi FROM parity"] == [
        {"n": 2, "lo": 1, "hi": 2}
    ]
    assert local["SELECT id FROM parity WHERE id > 100"] == []

    with routed_server(
        dolt_repo, dolt_env, dolt_binary, "SELECT id FROM parity WHERE id = 1", "id"
    ):
        routed = EmbeddedDriver(dolt_repo, env=dolt_env)
        try:
            for sql, _ in reads:
                assert_identical(local[sql], routed.query(sql), sql)
            routed_row = routed.query("SELECT * FROM parity WHERE id = 1")[0]
            assert type(routed_row["big_u"]) is int
            assert routed_row["big_u"] == UNSIGNED_BIG
            assert type(routed_row["text_num"]) is str
            assert type(routed_row["dec_v"]) is str
        finally:
            routed.close()

    # After the server is gone the same reads are back on the local engine.
    after = EmbeddedDriver(dolt_repo, env=dolt_env)
    try:
        for sql, _ in reads:
            assert_identical(local[sql], after.query(sql), f"no server: {sql}")
    finally:
        after.close()


def test_query_many_and_fill_columns_false_parity(
    dolt_repo: Path, dolt_env: dict[str, str], dolt_binary: str
) -> None:
    driver = EmbeddedDriver(dolt_repo, env=dolt_env)
    make_parity_table(driver)

    statements = [
        ("SELECT id, big_u FROM parity ORDER BY id", None),
        ("SELECT id FROM parity WHERE id > 100", None),
        ("SELECT COUNT(*) AS n FROM parity", None),
        (
            "SELECT id + 1 AS next_id, CAST(text_num AS SIGNED) AS cast_num "
            "FROM parity WHERE id = 2",
            None,
        ),
        ("SELECT nullable FROM parity ORDER BY id", None),
    ]
    try:
        local = driver.query_many(statements)
        local_unfilled = driver.query_many(statements, fill_columns=False)
        local_single = driver.query(
            "SELECT id, big_u FROM parity ORDER BY id", fill_columns=False
        )
    finally:
        driver.close()

    first = local[0][0]
    assert type(first["id"]) is int and type(first["big_u"]) is int
    assert local[1] == []
    assert local[2] == [{"n": 2}]
    assert local[3] == [{"next_id": 3, "cast_num": 42}]
    # fill_columns=False leaves a NULL column absent, not None.
    assert "nullable" not in local_unfilled[4][0]
    assert [row["id"] for row in local_single] == [1, 2]
    assert all(type(row["id"]) is int for row in local_single)

    with routed_server(
        dolt_repo, dolt_env, dolt_binary, "SELECT id FROM parity WHERE id = 1", "id"
    ):
        routed = EmbeddedDriver(dolt_repo, env=dolt_env)
        try:
            assert_identical(local, routed.query_many(statements), "query_many")
            assert_identical(
                local_unfilled,
                routed.query_many(statements, fill_columns=False),
                "query_many fill_columns=False",
            )
            assert_identical(
                local_single,
                routed.query(
                    "SELECT id, big_u FROM parity ORDER BY id", fill_columns=False
                ),
                "query fill_columns=False",
            )
            # fill_columns=False still restores integer types; it only skips NULLs.
            routed_unfilled = routed.query_many(statements, fill_columns=False)
            assert "nullable" not in routed_unfilled[4][0]
            assert all(type(row["id"]) is int for row in routed_unfilled[0])
        finally:
            routed.close()


# ---------------------------------------------------------------------------
# pending transaction preview (Q2f)
# ---------------------------------------------------------------------------


def test_pending_transaction_preview_parity(
    dolt_repo: Path, dolt_env: dict[str, str], dolt_binary: str
) -> None:
    driver = EmbeddedDriver(dolt_repo, env=dolt_env)
    make_parity_table(driver)

    def preview(driver: EmbeddedDriver, row_id: int) -> list[dict[str, Any]]:
        with driver.transaction() as tx:
            tx.execute(
                "INSERT INTO parity (id, big, tiny, text_num) VALUES (%s, %s, %s, %s)",
                (row_id, 42, 1, "007"),
            )
            rows = tx.query(
                "SELECT id, big, tiny, text_num, COUNT(*) AS n FROM parity "
                "WHERE id = %s GROUP BY id, big, tiny, text_num",
                (row_id,),
            )
            # The buffer stays pending after the preview: a second preview in
            # the same transaction still sees the uncommitted row.
            again = tx.query(
                "SELECT COUNT(*) AS n FROM parity WHERE id = %s", (row_id,)
            )
        assert again == [{"n": 1}]
        return rows

    try:
        local = preview(driver, 98)
    finally:
        driver.close()
    assert local == [{"id": 98, "big": 42, "tiny": 1, "text_num": "007", "n": 1}]
    assert type(local[0]["id"]) is int and type(local[0]["n"]) is int

    with routed_server(
        dolt_repo, dolt_env, dolt_binary, "SELECT id FROM parity WHERE id = 1", "id"
    ):
        routed = EmbeddedDriver(dolt_repo, env=dolt_env)
        try:
            routed_rows = preview(routed, 99)
        finally:
            routed.close()
    assert routed_rows == [{"id": 99, "big": 42, "tiny": 1, "text_num": "007", "n": 1}]
    assert type(routed_rows[0]["id"]) is int and type(routed_rows[0]["n"]) is int
    assert type(routed_rows[0]["text_num"]) is str

    # Both previewed transactions committed on exit, and the probe after the
    # ROLLBACK never leaked a row: exactly the two committed inserts exist.
    after = EmbeddedDriver(dolt_repo, env=dolt_env)
    try:
        assert after.query("SELECT COUNT(*) AS n FROM parity WHERE id IN (98, 99)") == [
            {"n": 2}
        ]
    finally:
        after.close()


# ---------------------------------------------------------------------------
# branch session (Q2g)
# ---------------------------------------------------------------------------


def test_branch_session_parity_with_a_live_server(
    dolt_repo: Path, dolt_env: dict[str, str], dolt_binary: str
) -> None:
    driver = EmbeddedDriver(dolt_repo, env=dolt_env)
    make_parity_table(driver)
    # A Dolt branch starts at a commit, so the fixture data must be committed
    # before the branch can see it; closing the live session first lets the
    # separate `dolt branch` process take the manifest.
    try:
        driver.commit("parity data", "Parity Tests <parity@tetramark.invalid>")
    finally:
        driver.close()
    subprocess.run(
        [dolt_binary, "branch", "parity-branch"],
        cwd=dolt_repo,
        env=dolt_env,
        check=True,
        capture_output=True,
        text=True,
    )
    local_driver = EmbeddedDriver(dolt_repo, branch="parity-branch", env=dolt_env)
    try:
        local = local_driver.query(
            "SELECT id, big_u, text_num FROM parity ORDER BY id"
        )
    finally:
        local_driver.close()
    assert [row["id"] for row in local] == [1, 2]
    assert type(local[0]["big_u"]) is int

    with routed_server(
        dolt_repo, dolt_env, dolt_binary, "SELECT id FROM parity WHERE id = 1", "id"
    ):
        routed_driver = EmbeddedDriver(dolt_repo, branch="parity-branch", env=dolt_env)
        try:
            assert_identical(
                local,
                routed_driver.query(
                    "SELECT id, big_u, text_num FROM parity ORDER BY id"
                ),
                "branch query",
            )
            assert_identical(
                [local],
                routed_driver.query_many(
                    [("SELECT id, big_u, text_num FROM parity ORDER BY id", None)]
                ),
                "branch query_many",
            )
        finally:
            routed_driver.close()


# ---------------------------------------------------------------------------
# the documented residual: non-SELECT / probe-rejected statements (Q2e)
# ---------------------------------------------------------------------------


def test_non_select_and_probe_rejected_reads_are_not_coerced(
    dolt_repo: Path, dolt_env: dict[str, str], dolt_binary: str
) -> None:
    driver = EmbeddedDriver(dolt_repo, env=dolt_env)
    make_parity_table(driver)
    try:
        # SHOW is not probeable: no type metadata, so no coercion in either mode.
        local_show = driver.query("SHOW COLUMNS FROM parity")
        local_dup = driver.query("SELECT id AS dup, big AS dup FROM parity WHERE id = 1")
    finally:
        driver.close()
    assert type(local_dup[0]["dup"]) is int

    with routed_server(
        dolt_repo, dolt_env, dolt_binary, "SELECT id FROM parity WHERE id = 1", "id"
    ):
        routed = EmbeddedDriver(dolt_repo, env=dolt_env)
        try:
            assert_identical(
                local_show, routed.query("SHOW COLUMNS FROM parity"), "SHOW"
            )
            # Duplicate column names make the temp-table probe fail; the agreed
            # residual returns Dolt's raw value, i.e. the routed string, unchanged.
            routed_dup = routed.query(
                "SELECT id AS dup, big AS dup FROM parity WHERE id = 1"
            )
            assert type(routed_dup[0]["dup"]) is str
            # A distinct-alias read of the same columns *is* normalised.
            normal = routed.query("SELECT id AS a, big AS b FROM parity WHERE id = 1")
            assert type(normal[0]["a"]) is int and type(normal[0]["b"]) is int
        finally:
            routed.close()


# ---------------------------------------------------------------------------
# one-process read invariant
# ---------------------------------------------------------------------------


def test_typed_read_is_still_one_dolt_process(
    dolt_repo: Path,
    dolt_env: dict[str, str],
    dolt_binary: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = EmbeddedDriver(dolt_repo, env=dolt_env)
    make_parity_table(driver)
    calls: list[str] = []
    original = EmbeddedDriver._invoke

    def counted(self: EmbeddedDriver, sql: str, result_format: str) -> str:
        calls.append(result_format)
        return original(self, sql, result_format)

    monkeypatch.setattr(EmbeddedDriver, "_invoke", counted)

    try:
        assert driver.query("SELECT id FROM parity WHERE id = 1") == [{"id": 1}]
        assert_single_read_process(calls)
    finally:
        driver.close()

    calls.clear()
    with routed_server(
        dolt_repo, dolt_env, dolt_binary, "SELECT id FROM parity WHERE id = 1", "id"
    ):
        routed = EmbeddedDriver(dolt_repo, env=dolt_env)
        try:
            assert routed.query("SELECT id FROM parity WHERE id = 1") == [{"id": 1}]
            assert_single_read_process(calls)
        finally:
            routed.close()


# ---------------------------------------------------------------------------
# Tetramark-level regression: stocks.ls() counts are int with a cached server
# ---------------------------------------------------------------------------


def test_stock_list_integer_columns_with_a_live_cached_server(
    tmp_path: Path, dolt_binary: str
) -> None:
    """The reported symptom: ``stocks.ls()`` counts flip to strings.

    A ``TETRAMARK_DRIVER=server`` open starts a real server; closing it keeps
    the server cached and alive, and its ``sql-server.info`` makes the embedded
    driver's ``dolt sql`` calls route through it.  ``aliquots_total`` is a
    ``COUNT`` and must be a Python ``int`` before, during and after that state.
    """
    del dolt_binary  # the fixture only skips the module when dolt is missing
    root = tmp_path / "stock-parity"
    env = isolated_env(root / "dolt-root", root / "home")

    ledger = init_ledger(
        root / "ledger",
        "stock-parity",
        ["nucleic_acids"],
        id_prefix="77",
        author=FACTORY_AUTHOR,
        env={**env, "TETRAMARK_DRIVER": "embedded"},
    )
    try:
        entity = ledger.entities.add("plasmid", name="pUC19")
        ledger.stocks.add("plasmid_prep", entity=entity["id"], aliquots=2)
        local = ledger.stocks.ls(all_statuses=True)
    finally:
        ledger.close()
    assert local, "the fixture stock was not listed"
    assert type(local[0]["aliquots_total"]) is int
    assert local[0]["aliquots_total"] == 2
    assert type(local[0]["aliquots_in_stock"]) is int

    try:
        # A server-backed open + close leaves the server cached and alive.
        server_ledger = open_ledger(
            root / "ledger", env={**env, "TETRAMARK_DRIVER": "server"}
        )
        server_ledger.close()
        assert (root / "ledger" / ".dolt" / "sql-server.info").is_file(), (
            "the cached server did not publish sql-server.info, so the embedded "
            "read would not be routed and this regression would prove nothing"
        )

        routed_ledger = open_ledger(
            root / "ledger", env={**env, "TETRAMARK_DRIVER": "embedded"}
        )
        try:
            assert_identical(
                local, routed_ledger.stocks.ls(all_statuses=True), "routed stocks.ls"
            )
            assert type(routed_ledger.stocks.ls(all_statuses=True)[0]["aliquots_total"]) is int
        finally:
            routed_ledger.close()
    finally:
        shutdown_all_servers()

    after = open_ledger(
        root / "ledger", env={**env, "TETRAMARK_DRIVER": "embedded"}
    )
    try:
        rows = after.stocks.ls(all_statuses=True)
        assert_identical(local, rows, "after shutdown_all_servers")
        assert type(rows[0]["aliquots_total"]) is int
    finally:
        after.close()
