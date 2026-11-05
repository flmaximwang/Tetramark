"""Pin list/show reads to one batched driver round trip.

``tm entity list``, ``tm stock list``, ``tm box list``, ``tm box show`` and the
``show``/``list`` operations behind them used to issue one ``dolt sql`` process
per kind (and another one per result set for its CSV column header).  The
batched read API must make each of them cost at most two processes -- one
``-r json`` batch plus one ``-r csv`` pass -- no matter how many kinds the
preset defines, while returning exactly the rows and values the per-statement
queries returned.

The tests count ``EmbeddedDriver._invoke`` calls after the fixture rows are
built (so the ledger's own open/close is not part of the budget), and they
compare the batched rows with the same operation run through per-statement
``query`` calls.  Real Dolt, never a mock, like the rest of the suite.
"""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from conftest import FACTORY_AUTHOR, PRESETS, isolated_env
from support.read_asserts import assert_single_read_process
from tetramark.core.ledger import Ledger, init_ledger, open as open_ledger
from tetramark.driver.embedded import EmbeddedDriver
from tetramark.driver.server import discard_server
from tetramark.errors import LedgerError

#: These tests pin budgets by counting the embedded driver's ``dolt sql``
#: processes; the ``embedded_driver`` marker makes conftest switch this test to
#: that backend even though the rest of the session defaults to the server.
pytestmark = pytest.mark.embedded_driver

#: ``full`` includes ``chain`` includes ``base``: three entity kinds, three
#: stock kinds and nine box kinds.  The old per-kind path paid two dolt
#: processes per kind, so this shape makes the fan-out obvious.
MANY_KINDS = [str(PRESETS / "full")]


@pytest.fixture(scope="module")
def read_ledger(
    tmp_path_factory: pytest.TempPathFactory, dolt_binary: str
) -> Iterator[tuple[Ledger, dict[str, Any]]]:
    """A many-kind ledger with its rows, built once per worker.

    The rows are fixture setup, not the read budget under test: they are built
    through the server backend (cheap) and the ledger is then reopened with the
    embedded driver the tests count.  Sharing it across the module is safe:
    every test here only reads.
    """
    del dolt_binary  # the fixture only skips the module when dolt is missing
    root = tmp_path_factory.mktemp("list-batch")
    env = isolated_env(root / "dolt-root", root / "home")
    ledger = init_ledger(
        root / "ledger",
        "list-batch",
        MANY_KINDS,
        id_prefix="77",
        author=FACTORY_AUTHOR,
        env={**env, "TETRAMARK_DRIVER": "server"},
    )
    built = build_rows(ledger)
    ledger.close()
    # Closing a server-backed ledger leaves the ``dolt sql-server`` cached and
    # alive, and Dolt then routes the embedded ``dolt sql`` reads through it:
    # the server reports integers as strings (``'1'``), so the fixture stops it
    # before the embedded reopen.
    discard_server(root / "ledger")
    assert not (root / "ledger" / ".dolt" / "sql-server.info").exists(), (
        "a cached sql-server would route the fixture's embedded reads through it"
    )
    ledger = open_ledger(root / "ledger", env={**env, "TETRAMARK_DRIVER": "embedded"})
    try:
        yield ledger, built
    finally:
        ledger.close()


@pytest.fixture
def invoke_log(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every result format passed to ``EmbeddedDriver._invoke`` from now on."""
    calls: list[str] = []
    original = EmbeddedDriver._invoke

    def counting(self: EmbeddedDriver, sql: str, result_format: str) -> str:
        calls.append(result_format)
        return original(self, sql, result_format)

    monkeypatch.setattr(EmbeddedDriver, "_invoke", counting)
    return calls


def build_rows(ledger: Ledger) -> dict[str, Any]:
    """Rows of every object type the list/show commands read."""
    item = ledger.entities.add("item", name="alpha", values={"category": "lab"})
    plasmid = ledger.entities.add(
        "plasmid", name="pUC19", values={"sequence": "ATCG", "backbone": "puc"}
    )
    everything = ledger.entities.add(
        "everything", name="thing", values={"code": "ABC"}
    )
    lot = ledger.stocks.add(
        "lot", entity=item["id"], values={"supplier": "acme"}, aliquots=2
    )
    bacterial = ledger.stocks.add(
        "bacterial", entity=plasmid["id"], values={"strain": "DH5a"}
    )
    # No tubes: the list table reports this stock as empty.
    prep = ledger.stocks.add("prep", entity=everything["id"], aliquots=0)

    grid = ledger.boxes.add("grid96", "BOX-1")
    shelf = ledger.boxes.add("shelf", "SHELF-1")
    inner = ledger.boxes.add("rack", "INNER-1", parent="SHELF-1")
    # A child box in a slot (the other occupant kind of a slotted box).
    rack = ledger.boxes.add("rack", "RACK-1")
    nested = ledger.boxes.add("rack", "INNER-2", parent="RACK-1", slot="top")
    # The ``full`` preset's grid9x9 has slot_capacity 2, so its capacity is
    # twice its 81 slots.
    grid2 = ledger.boxes.add("grid9x9", "GRID-2")
    ledger.aliases.add("box", grid["id"], "LEGACY-BOX")
    placed = ledger.stocks.add(
        "lot",
        entity=item["id"],
        values={"supplier": "acme"},
        box="BOX-1",
        slots=("A1",),
        quantity="5",
        unit="ml",
    )
    ledger.aliquots.add(lot["id"], box="SHELF-1")
    return {
        "entities": [item, plasmid, everything],
        "stocks": [lot, bacterial, prep, placed],
        "boxes": [grid, shelf, inner, rack, nested, grid2],
        "grid": grid,
        "shelf": shelf,
        "lot": lot,
    }


def per_query_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``query_many`` run one ``query`` per statement (the old path).

    The statements are independent, so the historical path is replayed on a
    small thread pool: the rows and the number of ``dolt sql`` processes are
    identical, and the regression check does not spend a minute doing what the
    batch does in one process.
    """
    def per_query(
        self: EmbeddedDriver, statements: list[tuple[str, Any]], **kwargs: Any
    ) -> list[list[dict[str, Any]]]:
        def run(statement: tuple[str, Any]) -> list[dict[str, Any]]:
            return self.query(statement[0], statement[1], **kwargs)

        with ThreadPoolExecutor(max_workers=4) as pool:
            return list(pool.map(run, statements))

    monkeypatch.setattr(EmbeddedDriver, "query_many", per_query)


def clear(log: list[str]) -> list[str]:
    log.clear()
    return log


# ---------------------------------------------------------------------------
# Process budgets
# ---------------------------------------------------------------------------


def test_entity_list_runs_one_batched_read(
    read_ledger: tuple[Ledger, dict[str, Any]], invoke_log: list[str]
) -> None:
    ledger, built = read_ledger

    clear(invoke_log)

    rows = ledger.entities.ls()

    # Kinds are visited in sorted order (everything, item, plasmid), and each
    # kind's rows keep the query's ORDER BY id.
    assert [row["id"] for row in rows] == [
        built["entities"][2]["id"],
        built["entities"][0]["id"],
        built["entities"][1]["id"],
    ]
    # The stock counts come from the same batch: item/alpha has two active
    # stocks (lot and placed) and two in-stock tubes; everything's prep has no
    # aliquots (empty) and plasmid's bacterial stock is depleted, so both are
    # 0/0.
    assert [
        (row["name"], row["stocks_available"], row["aliquots_in_stock"])
        for row in rows
    ] == [("thing", 0, 0), ("alpha", 2, 2), ("pUC19", 0, 0)]
    # One JSON batch for every kind's rows, one CSV batch for the column
    # names JSON omits for NULL values.  The kinds are all in one process.
    assert invoke_log == ["json"]
    assert len(invoke_log) <= 2


def test_entity_list_filtered_by_kind_runs_one_batched_read(
    read_ledger: tuple[Ledger, dict[str, Any]], invoke_log: list[str]
) -> None:
    ledger, _ = read_ledger
    clear(invoke_log)

    rows = ledger.entities.ls("plasmid")

    assert [row["name"] for row in rows] == ["pUC19"]
    assert invoke_log == ["json"]


def test_stock_list_runs_one_batched_read(
    read_ledger: tuple[Ledger, dict[str, Any]], invoke_log: list[str]
) -> None:
    ledger, built = read_ledger
    clear(invoke_log)

    rows = ledger.stocks.ls()

    # The default scope is active-only: the `lot` with a tube in SHELF-1 and
    # the placed stock, in kind order (bacterial, lot, prep) and then by id
    # within the kind.
    assert [row["kind"] for row in rows] == ["lot", "lot"]
    assert [row["id"] for row in rows] == [
        built["stocks"][0]["id"],
        built["stocks"][3]["id"],
    ]
    assert_single_read_process(invoke_log)

    # The columns the list table prints come from the same batched read: the
    # owning design's name, how many tubes are still on the shelf and the
    # status derived from them.
    by_id = {row["id"]: row for row in rows}
    placed = by_id[built["stocks"][3]["id"]]
    assert placed["entity_name"] == "alpha"
    assert placed["aliquots_in_stock"] == 1
    assert placed["status"] == "active"

    # ``all_statuses=True`` is the same single batch: the empty and depleted
    # rows come back in the same kind-then-id order.
    clear(invoke_log)
    all_rows = ledger.stocks.ls(all_statuses=True)
    assert [row["kind"] for row in all_rows] == ["bacterial", "lot", "lot", "prep"]
    assert [row["id"] for row in all_rows] == [
        built["stocks"][1]["id"],
        built["stocks"][0]["id"],
        built["stocks"][3]["id"],
        built["stocks"][2]["id"],
    ]
    assert_single_read_process(invoke_log)

    all_by_id = {row["id"]: row for row in all_rows}
    depleted = all_by_id[built["stocks"][1]["id"]]
    assert depleted["aliquots_in_stock"] == 0
    assert depleted["status"] == "depleted"

    prep = all_by_id[built["stocks"][2]["id"]]
    assert prep["aliquots_in_stock"] == 0
    assert prep["status"] == "empty"


def test_aliquot_list_runs_one_batched_read(
    read_ledger: tuple[Ledger, dict[str, Any]], invoke_log: list[str]
) -> None:
    ledger, built = read_ledger
    clear(invoke_log)

    rows = ledger.aliquots.ls()

    # `lot` has two unplaced tubes (pending) and one placed in the slotless
    # SHELF-1; `placed` has one tube in BOX-1 A1.  The default in-stock scope
    # returns exactly the two placed tubes.
    assert len(rows) == 2
    assert all(row["in_stock"] for row in rows)
    assert {row["path"] for row in rows} == {"BOX-1:A1", "SHELF-1"}
    # The rows and their aliases: one JSON batch, one CSV pass for the column
    # names JSON omits for NULL values.
    assert invoke_log == ["json"]

    # A box filter (a recursive box tree in the same statement) and a stock
    # filter (a resolved id in the same statement) stay one process too.
    clear(invoke_log)
    shelf = ledger.aliquots.ls(box="SHELF-1")
    assert [(row["stock_id"], row["seq"]) for row in shelf] == [
        (built["lot"]["id"], 3)
    ]
    assert invoke_log == ["json"]

    clear(invoke_log)
    assert [row["seq"] for row in ledger.aliquots.ls(built["lot"]["id"])] == [3]
    assert invoke_log == ["json"]

    # A pinned status is the one deliberate second batch: the status vocabulary
    # is the stock kind's, so the stock is resolved first (unknown/ambiguous
    # refs and the precise "choose from" message need its kind).
    clear(invoke_log)
    assert [
        row["seq"]
        for row in ledger.aliquots.ls(built["lot"]["id"], status=("stored",))
    ] == [3]
    assert invoke_log == ["json", "json"]


def test_box_list_runs_one_batched_read(
    read_ledger: tuple[Ledger, dict[str, Any]], invoke_log: list[str]
) -> None:
    ledger, _ = read_ledger
    clear(invoke_log)

    rows = ledger.boxes.ls()

    # Every kind's boxes plus the all-box path and occupancy rows, in one batch.
    assert invoke_log == ["json"]
    paths = {row["name"]: row["path"] for row in rows}
    assert paths["BOX-1"] == "BOX-1"
    assert paths["INNER-1"] == "SHELF-1/INNER-1"
    # Occupancy counts the aliquots and child boxes directly in a box (items,
    # not slots); nested contents do not roll up, so SHELF-1 stays at 2 even
    # though INNER-1 sits inside it.  Capacity is slots times slot_capacity for
    # slotted kinds and the declared capacity for slotless ones.
    occupancy = {row["name"]: (row["occupied"], row["capacity"]) for row in rows}
    assert occupancy == {
        "BOX-1": (1, 96),
        "SHELF-1": (2, 4),
        "INNER-1": (0, 3),
        "RACK-1": (1, 3),
        "INNER-2": (0, 3),
        "GRID-2": (0, 162),
    }


def test_box_show_runs_one_batched_read(
    read_ledger: tuple[Ledger, dict[str, Any]], invoke_log: list[str]
) -> None:
    ledger, built = read_ledger
    box_id = built["grid"]["id"]

    for ref in ("BOX-1", box_id, "LEGACY-BOX"):
        clear(invoke_log)
        view = ledger.boxes.show(ref)
        assert view["box"] == "BOX-1"
        assert view["aliases"] == ["LEGACY-BOX"]
        assert len(invoke_log) <= 3
        assert invoke_log == ["json"]

    # A slotted box with a child box in one of its slots reports that
    # occupant, too (both occupant kinds come from the same batch).
    clear(invoke_log)
    rack = ledger.boxes.show("RACK-1")
    top = next(slot for slot in rack["slots"] if slot["label"] == "top")
    assert top["occupants"] == [
        {"type": "box", "id": built["boxes"][4]["id"], "name": "INNER-2", "status": None}
    ]
    assert invoke_log == ["json"]

    # A slotless box reads its occupants from the same batch (no extra process
    # for the shape it does not use).
    clear(invoke_log)
    shelf = ledger.boxes.show("SHELF-1")
    assert shelf["geometry"]["type"] == "slotless"
    assert [occupant["name"] for occupant in shelf["occupants"] if "name" in occupant] == [
        "INNER-1"
    ]
    assert invoke_log == ["json"]


def test_show_entity_and_stock_resolve_the_kind_after_the_core_row(
    read_ledger: tuple[Ledger, dict[str, Any]], invoke_log: list[str]
) -> None:
    """Core + aliases + extras, then only the resolved kind's table.

    Two small batches instead of one per kind: a kind the schema declares but
    whose table is not applied yet cannot break an unrelated show.
    """
    ledger, built = read_ledger

    clear(invoke_log)
    entity = ledger.entities.show(built["entities"][0]["id"])
    assert entity["category"] == "lab"
    assert len(invoke_log) <= 4
    assert invoke_log == ["json", "json"]

    clear(invoke_log)
    stock = ledger.stocks.show(built["lot"]["id"])
    assert len(stock["aliquots"]) == 3
    assert len(invoke_log) <= 4
    assert invoke_log == ["json", "json"]


def test_empty_list_does_not_start_a_process(
    ledger_factory: Any, invoke_log: list[str]
) -> None:
    ledger = ledger_factory(MANY_KINDS, name="list-batch-empty")
    clear(invoke_log)

    rows = ledger.entities.ls()

    assert rows == []
    # Every kind's SELECT returned no rows, so there is nothing to recover
    # column names for: one JSON batch, no CSV pass, no per-kind process.
    assert invoke_log == ["json"]


# ---------------------------------------------------------------------------
# Output identity: the batch returns exactly the per-query rows
# ---------------------------------------------------------------------------


def test_per_query_path_replaces_query_many(
    read_ledger: tuple[Ledger, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    invoke_log: list[str],
) -> None:
    """The comparison is only an oracle while the per-query path is installed."""
    ledger, _ = read_ledger
    original = EmbeddedDriver.query_many

    per_query_path(monkeypatch)

    assert EmbeddedDriver.query_many is not original, (
        "per_query_path did not patch query_many: both phases would run the batch"
    )
    # Both stock scopes still fan out to one query per kind on the old path.
    for scope in ({}, {"all_statuses": True}):
        clear(invoke_log)
        rows = ledger.stocks.ls(**scope)
        assert rows, f"the fixture has stocks (scope={scope})"
        json_calls = [fmt for fmt in invoke_log if fmt == "json"]
        assert len(json_calls) > 1, (
            f"per_query_path still made {len(json_calls)} JSON dolt sql "
            f"call(s) (scope={scope})"
        )


def test_list_and_show_rows_match_per_query_results(
    read_ledger: tuple[Ledger, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Batching changes the process count, never the rows or their order."""
    ledger, built = read_ledger

    batched = {
        "entities": ledger.entities.ls(),
        "entities_item": ledger.entities.ls("item"),
        "stocks": ledger.stocks.ls(),
        "stocks_all": ledger.stocks.ls(all_statuses=True),
        "boxes": ledger.boxes.ls(),
        "box_grid": ledger.boxes.show("BOX-1"),
        "box_shelf": ledger.boxes.show("SHELF-1"),
        "box_alias": ledger.boxes.show("LEGACY-BOX"),
        "entity": ledger.entities.show(built["entities"][0]["id"]),
        "stock": ledger.stocks.show(built["lot"]["id"]),
    }

    per_query_path(monkeypatch)

    assert ledger.entities.ls() == batched["entities"]
    assert ledger.entities.ls("item") == batched["entities_item"]
    assert ledger.stocks.ls() == batched["stocks"]
    assert ledger.stocks.ls(all_statuses=True) == batched["stocks_all"]
    assert ledger.boxes.ls() == batched["boxes"]
    assert ledger.boxes.show("BOX-1") == batched["box_grid"]
    assert ledger.boxes.show("SHELF-1") == batched["box_shelf"]
    assert ledger.boxes.show("LEGACY-BOX") == batched["box_alias"]
    assert ledger.entities.show(built["entities"][0]["id"]) == batched["entity"]
    assert ledger.stocks.show(built["lot"]["id"]) == batched["stock"]


def test_unknown_refs_keep_their_errors(
    read_ledger: tuple[Ledger, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, _ = read_ledger

    for per_query in (False, True):
        if per_query:
            per_query_path(monkeypatch)
        with pytest.raises(LedgerError, match="unknown box 'NOPE'"):
            ledger.boxes.show("NOPE")
        with pytest.raises(LedgerError, match="unknown entity 'NOPE'"):
            ledger.entities.show("NOPE")
        with pytest.raises(LedgerError, match="unknown stock 'NOPE'"):
            ledger.stocks.show("NOPE")


def test_embedded_fixture_reads_typed_integers(
    read_ledger: tuple[Ledger, dict[str, Any]],
) -> None:
    """The embedded fixture reads integers, never the server's JSON strings.

    A ``dolt sql-server`` left alive by the fixture's server-built ledger makes
    Dolt route the embedded ``dolt sql -r json`` reads through that server,
    which reports integer columns as strings (``'1'``).  With the server gone
    the same reads come back as Python ints.
    """
    ledger, _ = read_ledger

    seqs = [row["seq"] for row in ledger.driver.query("SELECT seq FROM aliquot")]
    assert seqs, "the fixture has aliquots"
    assert all(type(seq) is int for seq in seqs), seqs

    totals = [row["aliquots_total"] for row in ledger.stocks.ls()]
    assert totals, "the fixture has stocks"
    assert all(type(total) is int for total in totals), totals
