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
from tetramark.core.ledger import Ledger, init_ledger, open as open_ledger
from tetramark.driver.embedded import EmbeddedDriver
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
    prep = ledger.stocks.add("prep", entity=everything["id"])

    grid = ledger.boxes.add("grid96", "BOX-1")
    shelf = ledger.boxes.add("shelf", "SHELF-1")
    inner = ledger.boxes.add("rack", "INNER-1", parent="SHELF-1")
    # A child box in a slot (the other occupant kind of a slotted box).
    rack = ledger.boxes.add("rack", "RACK-1")
    nested = ledger.boxes.add("rack", "INNER-2", parent="RACK-1", slot="top")
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
        "boxes": [grid, shelf, inner, rack, nested],
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
    # One JSON batch for every kind's rows, one CSV batch for the column
    # names JSON omits for NULL values.  The kinds are all in one process.
    assert invoke_log == ["json", "csv"]
    assert len(invoke_log) <= 2


def test_entity_list_filtered_by_kind_runs_one_batched_read(
    read_ledger: tuple[Ledger, dict[str, Any]], invoke_log: list[str]
) -> None:
    ledger, _ = read_ledger
    clear(invoke_log)

    rows = ledger.entities.ls("plasmid")

    assert [row["name"] for row in rows] == ["pUC19"]
    assert invoke_log == ["json", "csv"]


def test_stock_list_runs_one_batched_read(
    read_ledger: tuple[Ledger, dict[str, Any]], invoke_log: list[str]
) -> None:
    ledger, built = read_ledger
    clear(invoke_log)

    rows = ledger.stocks.ls()

    # Kinds in sorted order (bacterial, lot, prep), each kind's rows by id.
    assert [row["kind"] for row in rows] == ["bacterial", "lot", "lot", "prep"]
    assert [row["id"] for row in rows] == [
        built["stocks"][1]["id"],
        built["stocks"][0]["id"],
        built["stocks"][3]["id"],
        built["stocks"][2]["id"],
    ]
    assert invoke_log == ["json", "csv"]


def test_box_list_runs_one_batched_read(
    read_ledger: tuple[Ledger, dict[str, Any]], invoke_log: list[str]
) -> None:
    ledger, _ = read_ledger
    clear(invoke_log)

    rows = ledger.boxes.ls()

    # Every kind's boxes plus the all-box path rows, in one batch.
    assert invoke_log == ["json", "csv"]
    paths = {row["name"]: row["path"] for row in rows}
    assert paths["BOX-1"] == "BOX-1"
    assert paths["INNER-1"] == "SHELF-1/INNER-1"


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
        assert invoke_log == ["json", "csv"]

    # A slotted box with a child box in one of its slots reports that
    # occupant, too (both occupant kinds come from the same batch).
    clear(invoke_log)
    rack = ledger.boxes.show("RACK-1")
    top = next(slot for slot in rack["slots"] if slot["label"] == "top")
    assert top["occupants"] == [
        {"type": "box", "id": built["boxes"][4]["id"], "name": "INNER-2", "status": None}
    ]
    assert invoke_log == ["json", "csv"]

    # A slotless box reads its occupants from the same batch (no extra process
    # for the shape it does not use).
    clear(invoke_log)
    shelf = ledger.boxes.show("SHELF-1")
    assert shelf["geometry"]["type"] == "slotless"
    assert [occupant["name"] for occupant in shelf["occupants"] if "name" in occupant] == [
        "INNER-1"
    ]
    assert invoke_log == ["json", "csv"]


def test_show_entity_and_stock_run_one_batched_read(
    read_ledger: tuple[Ledger, dict[str, Any]], invoke_log: list[str]
) -> None:
    ledger, built = read_ledger

    clear(invoke_log)
    entity = ledger.entities.show(built["entities"][0]["id"])
    assert entity["category"] == "lab"
    assert invoke_log == ["json", "csv"]

    clear(invoke_log)
    stock = ledger.stocks.show(built["lot"]["id"])
    assert len(stock["aliquots"]) == 3
    assert invoke_log == ["json", "csv"]


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


def test_list_and_show_rows_match_per_query_results(
    read_ledger: tuple[Ledger, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Batching changes the process count, never the rows or their order."""
    ledger, built = read_ledger

    batched = {
        "entities": ledger.entities.ls(),
        "entities_item": ledger.entities.ls("item"),
        "stocks": ledger.stocks.ls(),
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

    for batched in (False, True):
        if batched:
            per_query_path(monkeypatch)
        with pytest.raises(LedgerError, match="unknown box 'NOPE'"):
            ledger.boxes.show("NOPE")
        with pytest.raises(LedgerError, match="unknown entity 'NOPE'"):
            ledger.entities.show("NOPE")
        with pytest.raises(LedgerError, match="unknown stock 'NOPE'"):
            ledger.stocks.show("NOPE")
