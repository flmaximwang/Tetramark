"""Notes in the list tables and the shared one-line preview (Tetramark-sa1).

``notes`` is the last column of ``tm stock list``, ``tm entity list`` and
``tm box list``.  The human table shows :func:`~tetramark.cli.output.notes_cell`
(a single line, cut at 40 terminal cells, with a ``(+N lines)`` suffix); JSON
and CSV keep the note exactly as stored.  The formatter tests are Dolt-free
(fast lane); the list tests run against a real ledger.
"""

from __future__ import annotations

import csv
import io
import json

import pytest
from rich.cells import cell_len
from typer.testing import CliRunner

from tetramark.cli.app import app
from tetramark.cli.output import notes_cell

from conftest import REPO_PRESETS

BASE = REPO_PRESETS / "base.yaml"

#: A note whose first line is 60 cells, followed by two more non-empty lines.
NOTE = "x" * 60 + "\nsecond line\nthird line"
#: What the table prints for :data:`NOTE`.
PREVIEW = "x" * 40 + "… (+2 lines)"


# ---------------------------------------------------------------------------
# The formatter (Dolt-free)
# ---------------------------------------------------------------------------


@pytest.mark.fast
@pytest.mark.parametrize("value", [None, "", "   ", "\n", "\t\n  \n\t"])
def test_blank_notes_print_an_empty_cell(value: object) -> None:
    assert notes_cell(value) == ""


@pytest.mark.fast
def test_first_non_empty_line_is_stripped_and_leading_blanks_skipped() -> None:
    assert (
        notes_cell("\n\t\n   first line   \nsecond line\n") == "first line (+1 lines)"
    )


@pytest.mark.fast
def test_one_short_line_prints_unchanged() -> None:
    assert notes_cell("a short note") == "a short note"
    assert notes_cell("6\u00d7His") == "6\u00d7His"


@pytest.mark.fast
def test_exactly_forty_cells_is_not_cut() -> None:
    line = "x" * 40
    assert notes_cell(line) == line


@pytest.mark.fast
def test_forty_one_cells_is_cut_at_forty_plus_an_ellipsis() -> None:
    assert notes_cell("x" * 41) == "x" * 40 + "…"


@pytest.mark.fast
def test_further_non_empty_lines_are_counted_and_blanks_ignored() -> None:
    note = "first\n\n second \n\nthird\n\n"
    assert notes_cell(note) == "first (+2 lines)"


@pytest.mark.fast
def test_cjk_and_greek_width_is_measured_in_terminal_cells() -> None:
    # 20 double-width glyphs are exactly 40 cells: not cut.
    exact = "日本語" * 6 + "日本"
    assert notes_cell(exact) == exact
    # 42 glyphs are 84 cells: cut to 40 cells without splitting a wide glyph.
    cut = notes_cell("日本語" * 14)
    assert cut.endswith("…")
    assert cell_len(cut[:-1]) == 40
    assert cell_len(cut) == 41
    # Greek letters are one cell each, like Latin text.
    greek = "αβγδε" * 8
    assert notes_cell(greek) == greek


@pytest.mark.fast
def test_trailing_spaces_are_stripped_and_tabs_survive_inside_a_line() -> None:
    assert notes_cell("  spaced note  ") == "spaced note"
    assert notes_cell("\talpha\tbeta\t") == "alpha\tbeta"


# ---------------------------------------------------------------------------
# The list tables (real Dolt)
# ---------------------------------------------------------------------------


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, dolt_env: dict[str, str]) -> None:
    for key, value in dolt_env.items():
        monkeypatch.setenv(key, value)


def invoke(ledger, *args: str):
    return CliRunner().invoke(app, ["--ledger", str(ledger.path), *args])


@pytest.fixture
def noted_ledger(cli_env: None, ledger_factory):
    """One entity, one active stock and one box, each carrying ``NOTE``."""
    ledger = ledger_factory([BASE], name="list-notes")

    entity = ledger.entities.add("item", name="alpha", values={"notes": NOTE})
    # A stock is active (and so in the default list) once a tube is in a box.
    box = ledger.boxes.add("grid8x12", "G-1", notes=NOTE)
    stock = ledger.stocks.add(
        "lot",
        entity=entity["id"],
        values={"supplier": "acme", "notes": NOTE},
        aliquots=1,
        box="G-1",
        slots=["A1"],
    )
    return ledger, entity, stock, box


def test_notes_is_the_last_column_of_the_three_list_tables(noted_ledger) -> None:
    ledger, *_ = noted_ledger
    for args in (
        ("stock", "list"),
        ("entity", "list"),
        ("box", "list"),
    ):
        result = invoke(ledger, *args)
        assert result.exit_code == 0, result.output
        header = result.output.splitlines()[0].split()
        assert header[-1] == "notes", (args, result.output)


def test_table_shows_the_one_line_preview(noted_ledger) -> None:
    ledger, entity, stock, box = noted_ledger
    for args, marker in (
        (("stock", "list"), stock["id"]),
        (("entity", "list"), entity["id"][:8]),
        (("box", "list"), box["id"][:8]),
    ):
        result = invoke(ledger, *args)
        assert result.exit_code == 0, result.output
        line = next(line for line in result.output.splitlines() if marker in line)
        assert PREVIEW in line, (args, result.output)
        assert NOTE not in result.output


def test_json_keeps_the_full_note(noted_ledger) -> None:
    ledger, *_ = noted_ledger
    assert json.loads(invoke(ledger, "--json", "entity", "list").output)[0][
        "notes"
    ] == NOTE
    assert json.loads(invoke(ledger, "--json", "stock", "list").output)[0][
        "notes"
    ] == NOTE
    assert json.loads(invoke(ledger, "--json", "box", "list").output)[0][
        "notes"
    ] == NOTE


def test_csv_keeps_the_full_note(noted_ledger) -> None:
    ledger, *_ = noted_ledger
    for args in (
        ("stock", "list"),
        ("entity", "list"),
        ("box", "list"),
    ):
        result = invoke(ledger, "--format", "csv", *args)
        assert result.exit_code == 0, result.output
        rows = list(csv.reader(io.StringIO(result.output)))
        assert rows[0][-1] == "notes", (args, rows[0])
        assert rows[1][-1] == NOTE, (args, rows[1])
