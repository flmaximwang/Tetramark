"""Round-trip budget: one read-side operation must not pay one dolt process per query.

The embedded driver starts one ``dolt sql`` process per statement (plus one
``-r csv`` process when a non-empty result needs its column names).  A whole
read operation therefore has a small, explicit budget: every read that can be
answered from the same snapshot belongs to *one* ``query_many`` batch.

These tests pin that budget by counting ``EmbeddedDriver._invoke`` calls, so a
later refactor cannot quietly reintroduce a query per kind or per hit.
"""

from __future__ import annotations

import pytest

from tetramark.driver.embedded import EmbeddedDriver

#: These tests pin budgets by counting the embedded driver's ``dolt sql``
#: processes; the opt-in server backend has no such per-statement process, so
#: the ``embedded_driver`` marker makes conftest switch them to that
#: backend even though the rest of the session defaults to the server.
pytestmark = pytest.mark.embedded_driver


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


def test_find_searches_in_one_batched_read(ledger_factory, invoke_log: list[str]) -> None:
    """``tm find``: one JSON batch plus one CSV header batch, not one per kind."""
    ledger = ledger_factory()
    item = ledger.entities.add("item", name="alpha")
    invoke_log.clear()

    hits = ledger.find("alpha")

    assert [hit["id"] for hit in hits][:1] == [item["id"]]
    # query_many runs the whole search in one ``-r json`` process; a result set
    # with rows costs exactly one extra ``-r csv`` run for the NULL-omitted
    # column names.
    assert invoke_log == ["json"]


def test_find_without_hits_skips_the_csv_run(
    ledger_factory, invoke_log: list[str]
) -> None:
    """An empty ledger has no row to recover column names for: JSON only."""
    ledger = ledger_factory()
    invoke_log.clear()

    assert ledger.find("nothing-like-this") == []
    assert invoke_log == ["json"]


def test_aliquot_show_reads_one_tube_in_one_batch(
    ledger_factory, invoke_log: list[str]
) -> None:
    """``tm aliquot show``: one JSON batch and one CSV run, not one per query.

    The command is a thin wrapper over ``ledger.aliquots.show``, whose record
    needs five reads (the tube, its sorted aliases, its stock's kind/entity,
    its ``v_aliquot_location`` path and its own ``aliquot_event`` rows).  They
    belong to one ``query_many`` batch, so showing a tube costs two ``dolt sql``
    processes whatever the record contains -- not the five (plus five CSV runs)
    a query-per-read would.
    """
    ledger = ledger_factory()
    item = ledger.entities.add("item", name="alpha")
    ledger.boxes.add("grid96", "BOX")
    stock = ledger.stocks.add(
        "lot",
        entity=item["id"],
        quantity=100,
        unit="ul",
        box="BOX",
        slots=["A3"],
    )
    alq = stock["aliquots"][0]
    invoke_log.clear()

    shown = ledger.aliquots.show(alq)

    # The marker's autouse fixture selected the embedded driver for this test;
    # the counts below are that driver's processes.
    assert isinstance(ledger.driver, EmbeddedDriver)
    assert shown["id"] == alq
    assert shown["path"] == "BOX:A3"
    # The events ride in the same batch: the tube's own create event.
    assert [event["event"] for event in shown["events"]] == ["create"]
    # One ``-r json`` process for the whole batch, one ``-r csv`` process for
    # the column names JSON omits when a value is NULL (``label``, ``notes``,
    # ``box_id``, ``slot`` and ``path`` are NULL on a typical tube).
    assert invoke_log == ["json"]
