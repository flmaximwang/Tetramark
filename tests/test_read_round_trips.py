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
    """A no-match search has no rows, so there is no column list to recover."""
    ledger = ledger_factory()
    ledger.entities.add("item", name="alpha")
    invoke_log.clear()

    assert ledger.find("nothing-like-this") == []
    assert invoke_log == ["json"]
