"""Golden tables for ``tm stock list``, ``tm entity list`` and ``tm box list``.

``tests/data/golden/stock_list.table`` pins the default table of ``tm stock
list``: the column set and order (``id kind entity_id entity_name
aliquots_in_stock status notes``), the shortened uuid ``entity_id``, an entity
name long enough to prove names print in full, the notes preview and only the
``active`` row -- the default scope hides the stocks whose derived ``status``
is ``empty`` (no aliquots) or ``depleted`` (every aliquot has left stock).

``tests/data/golden/stock_list_all.table`` pins the same table for ``tm stock
list --all``: the empty and depleted rows come back with the active one, in
the same column set and order, so the ``--all`` widening has its own stable
golden and the default file stays active-only.  Between the two files the three
fixture notes pin the preview: a two-line note prints ``(+1 lines)``, a note
wider than 40 terminal cells is cut with ``…``, and a CJK/Greek note is
measured in terminal cells.

``tests/data/golden/entity_list.table`` pins the default table of ``tm entity
list``: the column set and order (``id kind name stocks_available
aliquots_in_stock notes``), the shortened uuid ``id``, an entity name long
enough to prove names print in full, the two counts and the notes preview.
``stocks_available`` counts the entity's stocks whose derived status is
``active``; depleted and empty stocks are not counted.  ``aliquots_in_stock``
sums the in-stock tubes of all the entity's stocks, so an entity with no
stocks prints ``0``.

``tests/data/golden/box_list.table`` pins the default table of ``tm box list``:
the column set and order (``id name kind path occupied capacity notes``), the
nested ``path``, the two counts and the notes preview (blank for the boxes
without notes).  ``occupied`` counts the aliquots plus the immediate child
boxes directly in a box (items, not slots); nested contents do not roll up.
``capacity`` is slots times ``slot_capacity`` for ``grid``/``named`` geometry,
the declared capacity for slotless geometry, and blank for an unlimited
slotless box.

The rows are built with explicit ids (uuid entities, datecode stocks) so the
goldens are stable run to run.  The comparison is line by line after ``rstrip``:
rich pads the last column with trailing spaces, the golden files themselves
have no trailing whitespace.  Regenerate the files after an intended output
change with::

    TETRAMARK_UPDATE_GOLDENS=1 uv run pytest tests/test_list_tables_golden.py

The tables are the same on both backends: ``list_stocks``/``list_entities``
normalise the view's DOUBLE SUM to an int and ``list_boxes`` int-casts the
occupancy SUM, so the counts print ``0``/``1`` on the server and embedded lanes
alike.  The module is deliberately **not** marked, like the other CLI tests: it
runs on the session's default backend.
"""

from __future__ import annotations

import difflib
import json
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

#: The three notes shapes the preview has to pin: two lines, wider than 40
#: terminal cells, and mixed CJK/Greek text (with surrounding blanks to prove
#: the first line is stripped).
NOTE_TWO_LINES = "first line of the note\nsecond line"
NOTE_LONG = "a long note that is definitely wider than forty terminal cells"
NOTE_UNICODE = "  αβγ 抗体 prep  "


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, dolt_env: dict[str, str]) -> None:
    """Run the CLI with the same isolated Dolt/Git environment as the ledger."""
    for key, value in dolt_env.items():
        monkeypatch.setenv(key, value)


def _table(ledger, *args: str) -> str:
    """The default table a list command prints, without trailing spaces."""
    result = CliRunner().invoke(
        app, ["--ledger", str(ledger.path), *args]
    )
    assert result.exit_code == 0, result.output
    lines = [line.rstrip() for line in result.output.splitlines()]
    return "\n".join(lines) + "\n"


def _json(ledger, *args: str) -> list[dict]:
    """The JSON payload a list command prints."""
    result = CliRunner().invoke(
        app, ["--ledger", str(ledger.path), *args]
    )
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def _assert_golden(rendered: str, golden_name: str) -> None:
    """Compare a rendered table with its checked-in golden file."""
    golden = GOLDEN / golden_name
    if os.environ.get(UPDATE_GOLDENS_ENV, "").strip() not in ("", "0"):
        golden.write_text(rendered, encoding="utf-8")
    stored = golden.read_text(encoding="utf-8")
    if stored != rendered:
        diff = "".join(
            difflib.unified_diff(
                stored.splitlines(keepends=True),
                rendered.splitlines(keepends=True),
                fromfile=f"{golden_name} (stored)",
                tofile=f"{golden_name} (printed)",
            )
        )
        pytest.fail(
            f"{golden_name} is out of date; regenerate it with "
            f"{UPDATE_GOLDENS_ENV}=1 uv run pytest "
            f"{Path(__file__).name}\n{diff}"
        )


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
        values={"supplier": "acme", "notes": NOTE_TWO_LINES},
        aliquots=0,
    )
    # depleted: two tubes, both unplaced, so neither is in stock.
    ledger.stocks.add(
        "lot",
        id="7701AB02",
        entity=alpha["id"],
        values={"supplier": "acme", "notes": NOTE_LONG},
        aliquots=2,
    )
    # active: one of the two tubes is moved into G-1 A1.
    active = ledger.stocks.add(
        "lot",
        id="7701AB03",
        entity=design["id"],
        values={"supplier": "acme", "notes": NOTE_UNICODE},
        aliquots=2,
    )
    ledger.aliquots.move(active["aliquots"][0], box="G-1", slot="A1", message="moved")


def _build_entities(ledger) -> None:
    """One entity with an active lot, one with only depleted/empty lots."""
    alpha = ledger.entities.add(
        "item", id=ALPHA, name="alpha", values={"notes": NOTE_TWO_LINES}
    )
    design = ledger.entities.add(
        "item",
        id=LONG_NAME,
        name="a-rather-long-design-name-for-full-printing",
        values={"notes": NOTE_UNICODE},
    )
    ledger.boxes.add("grid8x12", "G-1")

    # active: two tubes, one of them moved into G-1 A1.
    active = ledger.stocks.add(
        "lot", entity=alpha["id"], values={"supplier": "acme"}, aliquots=2
    )
    ledger.aliquots.move(active["aliquots"][0], box="G-1", slot="A1", message="moved")
    # depleted: two tubes, both unplaced, so neither is in stock.
    ledger.stocks.add(
        "lot", entity=design["id"], values={"supplier": "acme"}, aliquots=2
    )
    # empty: a lot with no aliquots yet.
    ledger.stocks.add(
        "lot", entity=design["id"], values={"supplier": "acme"}, aliquots=0
    )


def _build_boxes(ledger) -> None:
    """One box per kind: a nested drawer, empty slotless boxes, placed tubes."""
    item = ledger.entities.add("item", name="alpha")
    # The unlimited incubator holds one child box (the drawer), which makes it
    # the box with one direct occupant; the drawer itself is empty.
    ledger.boxes.add("incubator", "I-1", notes=NOTE_TWO_LINES)
    ledger.boxes.add("drawer", "D-1", parent="I-1", notes=NOTE_LONG)
    ledger.boxes.add("freezer", "F-1")
    ledger.boxes.add("grid8x12", "G-1", notes=NOTE_UNICODE)
    ledger.boxes.add("grid9x9", "G-2")
    ledger.boxes.add("rack", "R-1")
    ledger.boxes.add("shelf", "S-1")

    stock = ledger.stocks.add(
        "lot", entity=item["id"], values={"supplier": "acme"}, aliquots=4
    )
    ledger.aliquots.move(stock["aliquots"][0], box="G-1", slot="A1", message="moved")
    ledger.aliquots.move(stock["aliquots"][1], box="G-1", slot="A2", message="moved")
    ledger.aliquots.move(stock["aliquots"][2], box="R-1", slot="1", message="moved")
    ledger.aliquots.move(stock["aliquots"][3], box="R-1", slot="2", message="moved")


def test_stock_list_golden_table(cli_env: None, ledger_factory) -> None:
    """The default table is active-only; ``--all`` adds the other statuses."""
    ledger = ledger_factory(
        [REPO_PRESETS / "base.yaml"], name="golden-stock-list", id_prefix="77"
    )
    _build_stocks(ledger)

    # The default scope: exactly the stock with a tube still in stock.
    _assert_golden(_table(ledger, "stock", "list"), "stock_list.table")
    # ``--all``: the same columns with the empty and depleted rows restored.
    _assert_golden(
        _table(ledger, "stock", "list", "--all"), "stock_list_all.table"
    )


def test_entity_list_golden_table(cli_env: None, ledger_factory) -> None:
    """The default list table: columns, full name, per-entity stock counts."""
    ledger = ledger_factory(
        [REPO_PRESETS / "base.yaml"], name="golden-entity-list", id_prefix="77"
    )
    _build_entities(ledger)

    _assert_golden(_table(ledger, "entity", "list"), "entity_list.table")


def test_box_list_golden_table(cli_env: None, ledger_factory) -> None:
    """The default table: columns, nested path, direct occupancy and capacity."""
    ledger = ledger_factory(
        [REPO_PRESETS / "base.yaml"], name="golden-box-list", id_prefix="77"
    )
    _build_boxes(ledger)

    table = _table(ledger, "box", "list")
    boxes = _json(ledger, "box", "list", "--json")
    # Box ids are generated; pin their rendered width while keeping the golden stable.
    for row in boxes:
        assert row["id"][:8] in table
        table = table.replace(row["id"][:8], "<box-id>")
    _assert_golden(table, "box_list.table")

    # JSON keeps the native types: both counts are ints, and the unlimited
    # incubator's capacity is null (the table prints it blank).
    rows = {row["name"]: row for row in _json(ledger, "box", "list", "--json")}
    assert rows["G-1"]["occupied"] == 2
    assert type(rows["G-1"]["occupied"]) is int
    assert rows["G-1"]["capacity"] == 96
    assert type(rows["G-1"]["capacity"]) is int
    assert rows["I-1"]["occupied"] == 1
    assert rows["I-1"]["capacity"] is None
