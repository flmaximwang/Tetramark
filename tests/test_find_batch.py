"""Pin ``tm find`` to one batched driver round trip.

``find`` used to issue a separate ``dolt sql`` process for every statement it
needs (roughly two per query: one for the JSON rows, one for the CSV header),
which scaled with the number of kinds in the preset.  The batched read API must
make a search cost at most two processes no matter how many kinds the ledger
has: one ``-r json`` batch for the rows and one ``-r csv`` pass for the column
lists.  The test counts ``_invoke`` calls while ``find`` runs, so a regression
to per-statement queries fails here, and it keeps the ranking assertions from
``tests/test_read_side.py`` so batching cannot change what is returned.

Real Dolt, never a mock, like the rest of the suite.
"""

from __future__ import annotations

from typing import Any

import pytest

from conftest import PRESETS
from tetramark.core.ledger import Ledger
from tetramark.driver.embedded import EmbeddedDriver

#: These tests pin budgets by counting the embedded driver's ``dolt sql``
#: processes; the opt-in server backend has no such per-statement process, so
#: ``tests/conftest.py`` skips them when ``TETRAMARK_DRIVER=server``.
pytestmark = pytest.mark.embedded_driver

#: ``full`` extends ``chain``/``base`` with every field type and geometry:
#: 12 entity/stock/box kinds against base's 9.  The old per-statement path paid
#: two dolt processes per kind, so this shape makes the fan-out obvious.
MANY_KINDS = [str(PRESETS / "full")]


@pytest.fixture
def find_ledger(ledger_factory: Any) -> Ledger:
    """A ledger whose preset has many kinds (a copy of a session template)."""
    return ledger_factory(MANY_KINDS, name="find-batch-many-kinds")


def add_item(ledger: Ledger, name: str, **values: Any) -> dict[str, Any]:
    return ledger.entities.add("item", name=name, values=values)


def test_find_runs_one_batched_driver_round_trip(
    find_ledger: Ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = find_ledger
    alpha = add_item(ledger, "alpha")
    alphabet = add_item(ledger, "alphabet")
    beta = add_item(ledger, "beta", description="alpha appears in this text")
    ledger.aliases.add("entity", alpha["id"], "ALP")

    query_many_calls: list[list[tuple[str, Any]]] = []
    invoke_calls: list[str] = []
    original_query_many = EmbeddedDriver.query_many
    original_invoke = EmbeddedDriver._invoke

    def counted_query_many(
        self: EmbeddedDriver, statements: list[tuple[str, Any]]
    ) -> list[list[dict[str, Any]]]:
        query_many_calls.append(list(statements))
        return original_query_many(self, statements)

    def counted_invoke(
        self: EmbeddedDriver, sql: str, result_format: str
    ) -> str:
        invoke_calls.append(result_format)
        return original_invoke(self, sql, result_format)

    monkeypatch.setattr(EmbeddedDriver, "query_many", counted_query_many)
    monkeypatch.setattr(EmbeddedDriver, "_invoke", counted_invoke)

    results = ledger.find("alpha")

    # Ranking and enrichment are unchanged by the batching.
    assert [row["id"] for row in results[:3]] == [
        alpha["id"],
        alphabet["id"],
        beta["id"],
    ]
    assert [row["match"] for row in results[:3]] == ["name", "name", "field"]
    assert [row["rank"] for row in results[:3]] == [1, 2, 3]
    assert results[0]["aliases"] == ["ALP"]

    # The whole search is one batched read: one query_many call and exactly two
    # dolt processes (JSON rows + CSV headers), regardless of the kind count.
    assert len(query_many_calls) == 1, (
        f"find issued {len(query_many_calls)} query_many calls"
    )
    assert invoke_calls == ["json", "csv"], (
        f"find issued {len(invoke_calls)} dolt processes: {invoke_calls}"
    )
    # Sanity check that a real batch was built (4 id lookups + 1 alias lookup +
    # one scan per kind + enrichment maps), not a single trivial statement.
    assert len(query_many_calls[0]) >= 10

    # A search with no hits still batches: every scan returns zero rows, and
    # those empty result sets must not shift the enrichment sets that follow.
    query_many_calls.clear()
    invoke_calls.clear()
    assert ledger.find("nothing-like-this") == []
    assert len(query_many_calls) == 1
    assert invoke_calls == ["json", "csv"], (
        f"empty find issued {len(invoke_calls)} dolt processes: {invoke_calls}"
    )
