"""Golden table for ``tm stock list`` (Tetramark-uue.1).

``tests/data/golden/stock_list.table`` pins the default table of ``tm stock
list``: the column set and order (``id kind entity_id entity_name
aliquots_in_stock status``), the shortened uuid ``entity_id``, an entity name
long enough to prove names print in full, and the derived ``status`` values --
``empty`` for a stock without aliquots, ``depleted`` when every aliquot has
left stock and ``active`` while at least one is still in stock.

The rows are built with explicit ids (uuid entities, datecode stocks) so the
golden is stable run to run.  The comparison is line by line after ``rstrip``:
rich pads the last column with trailing spaces, the golden file itself has no
trailing whitespace.  Regenerate the file after an intended output change
with::

    TETRAMARK_UPDATE_GOLDENS=1 uv run pytest tests/test_list_tables_golden.py

The table is the same on both backends: ``list_stocks`` normalises the view's
DOUBLE ``aliquots_in_stock`` SUM to an int, so the count prints ``0``/``1`` on
the server and embedded lanes alike.  The module is deliberately **not**
marked, like the other CLI tests: it runs on the session's default backend.
"""

from __future__ import annotations

import difflib
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tetramark.cli.app import app

from conftest import REPO_PRESETS

GOLDEN = Path(__file__).parent / "data" / "golden"

#: Setting this to a non-empty, non-zero value rewrites the golden file with
#: the table the code currently prints (the maintainability escape hatch; it
#: is never set by the lanes).
UPDATE_GOLDENS_ENV = "TETRAMARK_UPDATE_GOLDENS"

#: The two designs, and their ids: short, and long enough that a truncating
#: table would show it.
ALPHA = "11111111-1111-4111-8111-111111111111"
LONG_NAME = "22222222-2222-4222-8222-222222222222"


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, dolt_env: dict[str, str]) -> None:
    """Run the CLI with the same isolated Dolt/Git environment as the ledger."""
    for key, value in dolt_env.items():
        monkeypatch.setenv(key, value)


def _stock_list_table(ledger) -> str:
    """The default table ``tm stock list`` prints, without trailing spaces."""
    result = CliRunner().invoke(
        app, ["--ledger", str(ledger.path), "stock", "list"]
    )
    assert result.exit_code == 0, result.output
    lines = [line.rstrip() for line in result.output.splitlines()]
    return "\n".join(lines) + "\n"


def _build_stocks(ledger) -> None:
    """Three lots covering the three statuses, one for a long-named design."""
    alpha = ledger.entities.add("item", id=ALPHA, name="alpha")
    design = ledger.entities.add(
        "item",
        id=LONG_NAME,
        name="a-rather-long-design-name-for-full-printing",
    )
    ledger.boxes.add("grid8x12", "G-1")

    # empty: a lot with no aliquots yet.
    ledger.stocks.add(
        "lot",
        id="7701AB01",
        entity=alpha["id"],
        values={"supplier": "acme"},
        aliquots=0,
    )
    # depleted: two tubes, both unplaced, so neither is in stock.
    ledger.stocks.add(
        "lot",
        id="7701AB02",
        entity=alpha["id"],
        values={"supplier": "acme"},
        aliquots=2,
    )
    # active: one of the two tubes is moved into G-1 A1.
    active = ledger.stocks.add(
        "lot",
        id="7701AB03",
        entity=design["id"],
        values={"supplier": "acme"},
        aliquots=2,
    )
    ledger.aliquots.move(active["aliquots"][0], box="G-1", slot="A1")


def test_stock_list_golden_table(cli_env: None, ledger_factory) -> None:
    """The default list table: columns, full entity name, derived statuses."""
    ledger = ledger_factory(
        [REPO_PRESETS / "base.yaml"], name="golden-stock-list", id_prefix="77"
    )
    _build_stocks(ledger)

    rendered = _stock_list_table(ledger)
    golden = GOLDEN / "stock_list.table"
    if os.environ.get(UPDATE_GOLDENS_ENV, "").strip() not in ("", "0"):
        golden.write_text(rendered, encoding="utf-8")
    stored = golden.read_text(encoding="utf-8")
    if stored != rendered:
        diff = "".join(
            difflib.unified_diff(
                stored.splitlines(keepends=True),
                rendered.splitlines(keepends=True),
                fromfile="stock_list.table (stored)",
                tofile="stock_list.table (printed)",
            )
        )
        pytest.fail(
            "stock_list.table is out of date; regenerate it with "
            f"{UPDATE_GOLDENS_ENV}=1 uv run pytest "
            f"{Path(__file__).name}\n{diff}"
        )
