"""The parent stock's note in ``tm aliquot list`` and ``tm aliquot show``.

An aliquot has no note of its own worth reading: it borrows the owning stock's.
The list and the show therefore expose ``stock_notes`` -- a value that is not
the tube's own property, marked with the ``*`` convention:

* the human list header is ``*notes`` (no space, so the column keeps its
  width); the table shows the same one-line preview the stock/entity/box lists
  use, and every row repeats its stock's note;
* the human show prints a ``* notes`` key/value row with the **full** note and
  a ``* inherited from stock <id>`` line with the other ``*`` lines;
* JSON and CSV keep the clean field name ``stock_notes`` and the full text;
* ``--where stock_notes~x`` (and ``--where-any``) filter on the stock's note,
  with the text-column empty rule for ``stock_notes=``.

Everything runs against the real ``dolt`` binary, like the rest of the CLI
suite.
"""

from __future__ import annotations

import csv
import io
import json

import pytest
from typer.testing import CliRunner

from tetramark.cli.app import app
from tetramark.cli.output import notes_cell
from tetramark.errors import UsageError

from conftest import PRESETS

BASE = PRESETS / "base"

#: A stock note whose first line is 60 cells and which has two more lines: the
#: table must show the shared preview, JSON/CSV/show the whole text.
NOTE = "miniprep 2026-11-07, keep at -20 C, do not refreeze\nsecond line\nthird line"
PREVIEW = notes_cell(NOTE)


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, dolt_env: dict[str, str]) -> None:
    for key, value in dolt_env.items():
        monkeypatch.setenv(key, value)


def invoke(ledger, *args: str):
    return CliRunner().invoke(app, ["--ledger", str(ledger.path), *args])


def _json(ledger, *args: str):
    result = invoke(ledger, *args)
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def _csv_rows(ledger, *args: str) -> list[list[str]]:
    result = invoke(ledger, "--format", "csv", *args)
    assert result.exit_code == 0, result.output
    return list(csv.reader(io.StringIO(result.output)))


@pytest.fixture
def noted_ledger(cli_env: None, ledger_factory):
    """Two stocks (one noted, one not) with two in-stock tubes each.

    The first stock's note is :data:`NOTE`; the second has none.  Two tubes per
    stock prove that every row repeats its stock's note.
    """
    ledger = ledger_factory([BASE], name="aliquot-stock-notes", id_prefix="77")
    item = ledger.entities.add("item", name="alpha")
    ledger.boxes.add("grid96", "BOX")
    noted = ledger.stocks.add(
        "lot",
        entity=item["id"],
        values={"supplier": "acme", "notes": NOTE},
        aliquots=2,
        box="BOX",
        slots=("A1", "A2"),
    )
    plain = ledger.stocks.add(
        "lot",
        entity=item["id"],
        values={"supplier": "acme"},
        aliquots=2,
        box="BOX",
        slots=("B1", "B2"),
    )
    return {
        "ledger": ledger,
        "entity": item,
        "noted": noted,
        "plain": plain,
    }


# ---------------------------------------------------------------------------
# The human list
# ---------------------------------------------------------------------------


def test_list_header_is_the_starred_notes_column(noted_ledger) -> None:
    """The last column is ``*notes``; the clean name is not printed there."""
    ledger = noted_ledger["ledger"]
    result = invoke(ledger, "aliquot", "list")
    assert result.exit_code == 0, result.output
    header = result.output.splitlines()[0].split()
    assert header[-1] == "*notes"
    assert "stock_notes" not in header


def test_list_shows_the_preview_and_repeats_every_rows_stock_note(
    noted_ledger,
) -> None:
    """Both tubes of the noted stock show the preview; the plain stock is blank."""
    made = noted_ledger
    ledger = made["ledger"]
    result = invoke(ledger, "aliquot", "list")
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()[1:]

    noted_rows = [line for line in lines if line.startswith(made["noted"]["id"])]
    plain_rows = [line for line in lines if line.startswith(made["plain"]["id"])]
    assert len(noted_rows) == 2 and len(plain_rows) == 2
    # Every row repeats the stock's note -- no blanked repeat -- and the full
    # multi-line note never reaches the table.
    assert all(PREVIEW in line for line in noted_rows)
    assert NOTE not in result.output
    # A stock without a note prints an empty last cell: the row ends at the
    # path column (the notes cell is the trailing blank field).
    assert all(PREVIEW not in line for line in plain_rows)
    assert all(line.split()[-1].endswith(("B1", "B2")) for line in plain_rows)


def test_list_json_keeps_the_full_note_under_the_clean_name(noted_ledger) -> None:
    """JSON rows carry ``stock_notes`` (full, no star) and keep ``notes`` too."""
    made = noted_ledger
    rows = _json(made["ledger"], "aliquot", "list", "--json")
    by_stock: dict[str, list[dict]] = {}
    for row in rows:
        by_stock.setdefault(row["stock_id"], []).append(row)
    assert all(row["stock_notes"] == NOTE for row in by_stock[made["noted"]["id"]])
    assert all(row["stock_notes"] is None for row in by_stock[made["plain"]["id"]])
    # The tube's own ``notes`` column is gone (spec 8): no list row has it.
    assert all("notes" not in row for row in rows)


def test_list_csv_keeps_the_full_note_under_the_clean_name(noted_ledger) -> None:
    rows = _csv_rows(noted_ledger["ledger"], "aliquot", "list")
    assert rows[0][-1] == "stock_notes"
    assert "*notes" not in rows[0]
    column = rows[0].index("stock_notes")
    assert any(row[column] == NOTE for row in rows[1:])
    assert any(row[column] == "" for row in rows[1:])


# ---------------------------------------------------------------------------
# The human show
# ---------------------------------------------------------------------------


def test_show_prints_the_star_notes_row_and_the_inherited_footer(
    noted_ledger,
) -> None:
    """``* notes`` carries the full note; the footer names the stock."""
    made = noted_ledger
    aliquot = made["noted"]["aliquots"][0]
    result = invoke(made["ledger"], "aliquot", "show", aliquot)
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()

    notes_row = lines.index(next(line for line in lines if line.startswith("* notes")))
    # The value is the full note, never the table preview: every line of it is
    # printed (rich lays a multi-line cell out under the value column).
    assert all(part in result.output for part in NOTE.splitlines())
    assert PREVIEW not in result.output
    assert lines[notes_row].startswith("* notes")
    # The footer follows the related ``* entity`` line, before the path line.
    footer = f"* inherited from stock {made['noted']['id']}"
    assert footer in lines
    entity_at = next(
        index for index, line in enumerate(lines) if line.startswith("* entity")
    )
    assert lines.index(footer) > entity_at
    assert lines.index(footer) < lines.index(next(
        line for line in lines if line.startswith("path:")
    ))


def test_show_json_and_csv_keep_the_full_note_clean(noted_ledger) -> None:
    made = noted_ledger
    aliquot = made["noted"]["aliquots"][0]
    record = _json(made["ledger"], "aliquot", "show", aliquot, "--json")
    assert record["stock_notes"] == NOTE
    assert "* notes" not in record

    rows = _csv_rows(made["ledger"], "aliquot", "show", aliquot)
    header = rows[0]
    assert "stock_notes" in header
    assert "* notes" not in header
    assert rows[1][header.index("stock_notes")] == NOTE


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


def test_where_stock_notes_filters_on_the_stock_text(noted_ledger) -> None:
    """``~``, ``!~`` and ``=`` all read ``s.notes`` (the stock's column)."""
    made = noted_ledger
    ledger = made["ledger"]

    matched = _json(ledger, "aliquot", "list", "--where", "stock_notes~miniprep", "--json")
    assert {row["stock_id"] for row in matched} == {made["noted"]["id"]}
    assert len(matched) == 2

    unmatched = _json(ledger, "aliquot", "list", "--where", "stock_notes!~miniprep", "--json")
    assert {row["stock_id"] for row in unmatched} == {made["plain"]["id"]}

    # Empty value: a NULL or '' text column; the plain stock has no note.
    empty = _json(ledger, "aliquot", "list", "--where", "stock_notes=", "--json")
    assert {row["stock_id"] for row in empty} == {made["plain"]["id"]}

    filled = _json(ledger, "aliquot", "list", "--where", "stock_notes!=", "--json")
    assert {row["stock_id"] for row in filled} == {made["noted"]["id"]}

    # An ordered comparison keeps the database's collation order (and a NULL
    # never matches), so the noted stock sorts at or after "miniprep".
    ordered = _json(
        ledger, "aliquot", "list", "--where", "stock_notes>=miniprep", "--json"
    )
    assert {row["stock_id"] for row in ordered} == {made["noted"]["id"]}

    # ``--where-any`` is one OR group over the same expression map.
    either = _json(
        ledger,
        "aliquot",
        "list",
        "--where-any",
        "stock_notes~miniprep",
        "--where-any",
        "stock_notes=",
        "--json",
    )
    assert {row["stock_id"] for row in either} == {
        made["noted"]["id"],
        made["plain"]["id"],
    }


def test_the_tubes_own_notes_column_is_gone(noted_ledger) -> None:
    """Spec 8 dropped ``aliquot.notes``: ``notes`` is no longer a filter field."""
    made = noted_ledger
    ledger = made["ledger"]
    aliquot = made["noted"]["aliquots"][0]
    refused = invoke(ledger, "aliquot", "list", "--where", "notes~tube-local")
    assert isinstance(refused.exception, UsageError)
    assert refused.exception.exit_code == 2
    assert "unknown field(s) 'notes'" in str(refused.exception)
    assert "stock_notes" in str(refused.exception)

    stock = _json(ledger, "aliquot", "list", "--where", "stock_notes~miniprep", "--json")
    assert aliquot in {row["id"] for row in stock}


def test_the_inherited_footer_names_the_stock_with_the_tables_short_id(
    cli_env: dict[str, str], ledger_factory, tmp_path
) -> None:
    """The footer id is the ``stock_id`` row's value, not a second shortening.

    ``short_uuid_ids`` lengthens a prefix only when two ids of the *same*
    shortening set collide: the show table shortens ``stock_id`` together with
    ``entity_id`` and ``box_id``, so a robust ledger (a uuid stock-id strategy)
    whose stock uuid shares its first eight hex characters with the owning
    entity's uuid prints a longer prefix there.  The footer must repeat that
    exact prefix instead of shortening the stock id on its own (which would
    print an ambiguous eight characters).

    The default ledger's stock ids are datecodes, so the strategy is switched
    to ``uuid`` in ``ledger.yaml`` before the first object is created.
    """
    from tetramark.core.ledger import open as open_ledger

    ledger = ledger_factory([BASE], name="aliquot-footer-prefix", id_prefix="77")
    config_path = ledger.path / "ledger.yaml"
    config = config_path.read_text(encoding="utf-8")
    assert '"strategy": "datecode"' in config or "strategy: datecode" in config
    config_path.write_text(
        config.replace("datecode", "uuid"), encoding="utf-8"
    )
    ledger.close()
    ledger = open_ledger(ledger.path, env=cli_env)

    entity = ledger.entities.add(
        "item", name="alpha", id="aaaaaaaa-1111-4111-8111-111111111111"
    )
    stock = ledger.stocks.add(
        "lot",
        entity=entity["id"],
        id="aaaaaaaa-2222-4222-8222-222222222222",
        aliquots=1,
    )
    aliquot = stock["aliquots"][0]

    result = invoke(ledger, "aliquot", "show", aliquot)
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    footer = next(line for line in lines if line.startswith("* inherited from stock "))
    stock_row = next(line for line in lines if line.startswith("stock_id "))
    table_id = stock_row.split()[-1]

    # The collision makes the table's prefix longer than the eight-character
    # minimum, and the footer repeats it exactly instead of the ambiguous
    # eight characters a separate shortening of the stock id would print.
    assert len(table_id) > 8
    assert table_id != stock["id"][:8]
    assert footer == f"* inherited from stock {table_id}"
    assert stock["id"] not in footer
    # The entity's own id is the other half of the collision, shown separately.
    entity_row = next(line for line in lines if line.startswith("entity_id "))
    assert entity_row.split()[-1] != table_id
