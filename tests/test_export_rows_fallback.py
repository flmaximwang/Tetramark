"""The export fallback must not re-read a key it has already queried."""

from __future__ import annotations

from typing import Any

import pytest

from tetramark.errors import LedgerError
from tetramark.ops.io_csv import _ExportRows

pytestmark = pytest.mark.fast


class _FailingBatchDriver:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def query_many(self, statements: Any) -> Any:
        raise LedgerError("simulated: one read in the batch failed")

    def query(self, sql: str) -> list[dict[str, Any]]:
        self.queries.append(sql)
        return [{"sql": sql}]


def test_the_per_query_fallback_caches_each_key() -> None:
    driver = _FailingBatchDriver()
    rows = _ExportRows(driver, [("a", "SELECT a"), ("b", "SELECT b")])

    assert rows.rows("a") == [{"sql": "SELECT a"}]
    assert rows.rows("a") == [{"sql": "SELECT a"}]
    assert rows.rows("b") == [{"sql": "SELECT b"}]
    assert driver.queries == ["SELECT a", "SELECT b"]
