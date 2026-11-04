"""The open fallback closes the replacement driver it discards."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tetramark.core import ledger as ledger_module
from tetramark.core.ledger import _open_read_state
from tetramark.errors import LedgerError

pytestmark = pytest.mark.fast


class _BaseDriver:
    def query_many(self, statements: Any, **kwargs: Any) -> Any:
        raise LedgerError("simulated: the one-process open read failed")


class _ReplacementDriver:
    def __init__(self) -> None:
        self.closed = False

    def query(self, sql: str, **kwargs: Any) -> Any:
        raise RuntimeError("simulated: unexpected failure after re-opening")

    def close(self) -> None:
        self.closed = True


def test_the_fallback_closes_the_replacement_on_an_unexpected_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replacement = _ReplacementDriver()
    monkeypatch.setattr(
        ledger_module, "driver_for", lambda *args, **kwargs: replacement
    )

    with pytest.raises(RuntimeError, match="unexpected failure"):
        _open_read_state(
            _BaseDriver(), Path("/tmp/ledger"), "feature", {}  # noqa: S108
        )
    assert replacement.closed
