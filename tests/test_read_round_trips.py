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
    assert invoke_log == ["json", "csv"]


def test_find_without_hits_skips_the_csv_run(
    ledger_factory, invoke_log: list[str]
) -> None:
    """An empty ledger has no row to recover column names for: JSON only."""
    ledger = ledger_factory()
    invoke_log.clear()

    assert ledger.find("nothing-like-this") == []
    assert invoke_log == ["json"]
