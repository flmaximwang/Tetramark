"""Shared read-side output: table, JSON and CSV (spec §6, ticket 11).

Every ``list``/``show`` command funnels through :func:`emit_read` (or
:func:`emit_payload` for a shape that needs custom rendering).  Format selection
is uniform: a command's ``--json`` wins, then the global ``--json``, then the
global ``--format``, else the table.

Tables are rendered with ``rich`` so wide values wrap instead of breaking the
columns; JSON keeps the native shapes (lists, dicts, aliases) and CSV
stringifies only what a cell cannot hold.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

import typer
from rich.cells import cell_len, chop_cells
from rich.console import Console
from rich.table import Table

from tetramark.cli.options import GlobalOptions, echo_dry_run_sql, target_branch
from tetramark.core.ids import UuidIds
from tetramark.errors import UsageError

__all__ = [
    "FORMATS",
    "NOTES_COLUMNS",
    "emit_payload",
    "emit_read",
    "emit_write",
    "is_uuid4",
    "json_default",
    "notes_cell",
    "output_format",
    "short_uuid_ids",
]

FORMATS: tuple[str, ...] = ("table", "json", "csv")

#: The ledger's own uuid id strategy defines what a uuid id is (36-char
#: 8-4-4-4-12, case-insensitive) — reuse it rather than keeping a second regex.
_UUID_IDS = UuidIds()

#: Shortest uuid prefix shown by :func:`short_uuid_ids` (git-style short hash).
SHORT_UUID_LENGTH = 8

#: Table columns that hold free-form notes and are shortened by
#: :func:`notes_cell`.  Other commands can add a notes-like column (for
#: example the aliquot list's ``stock_notes``) to this set instead of writing a
#: second formatter.
NOTES_COLUMNS: frozenset[str] = frozenset({"notes"})

#: Width, in terminal cells, of the notes preview before the ellipsis.
NOTES_CELL_LENGTH = 40


# ---------------------------------------------------------------------------
# Format selection
# ---------------------------------------------------------------------------


def output_format(ctx: typer.Context, local_json: bool = False) -> str:
    """Resolve the output format for one read command."""
    options = ctx.obj
    selected: str | None = None
    if local_json:
        selected = "json"
    elif isinstance(options, GlobalOptions):
        if options.json:
            selected = "json"
        else:
            selected = options.format
    if selected is None:
        selected = "table"
    text = str(selected).lower()
    if text not in FORMATS:
        raise UsageError(
            f"invalid format {selected!r}: expected one of {', '.join(FORMATS)}"
        )
    return text


# ---------------------------------------------------------------------------
# Value conversion
# ---------------------------------------------------------------------------


def json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    return str(value)


# ---------------------------------------------------------------------------
# Short uuid ids (table output only, like git's short hashes)
# ---------------------------------------------------------------------------


def is_uuid4(value: Any) -> bool:
    """Whether ``value`` is a canonical uuid id (8-4-4-4-12, case-insensitive).

    The shape is the ledger's uuid id strategy (:class:`~tetramark.core.ids.UuidIds`),
    so datecode, sequence and aliquot ids are never shortened.
    """
    return _UUID_IDS.validate(value)


def short_uuid_ids(
    values: Sequence[Any], *, min_length: int = SHORT_UUID_LENGTH
) -> list[Any]:
    """Shorten uuid ids to prefixes that are unique within ``values``.

    Every canonical uuid id becomes its shortest prefix of at least
    ``min_length`` characters that no other uuid id in ``values`` shares
    (compared case-insensitively); colliding prefixes grow together until they
    differ.  Non-uuid values pass through unchanged, and the result is aligned
    with ``values`` and deterministic.
    """
    uuids = [value for value in values if is_uuid4(value)]
    prefixes: dict[str, str] = {}
    for value in uuids:
        folded = value.lower()
        width = min_length
        while width < len(folded) and any(
            other.lower()[:width] == folded[:width]
            for other in uuids
            if other != value
        ):
            width += 1
        prefixes[value] = value[:width]
    return [
        prefixes[value] if isinstance(value, str) and value in prefixes else value
        for value in values
    ]


def _shorten_uuid_columns(
    rows: list[dict[str, Any]], columns: Sequence[str]
) -> None:
    """Replace uuid values in ``columns`` with unique short prefixes, in place."""
    for column in columns:
        shortened = short_uuid_ids([row.get(column) for row in rows])
        for row, value in zip(rows, shortened, strict=True):
            row[column] = value


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return ", ".join(_cell(item) for item in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, default=json_default)
    return str(value)


def notes_cell(value: Any) -> str:
    """The one-line preview of a notes value used by list tables.

    The algorithm is:

    * ``None``, an empty string and a string of only blank lines all yield an
      empty cell.
    * The first non-empty line is taken (leading blank lines are skipped) and
      whitespace-stripped, so a note that starts with indentation still reads
      naturally.
    * That line is measured in terminal cells with :func:`rich.cells.cell_len`
      so CJK and Greek glyphs count their real width.  A line of exactly
      :data:`NOTES_CELL_LENGTH` cells is not cut; a longer one keeps the first
      :data:`NOTES_CELL_LENGTH` cells (never splitting a wide character or a
      combining sequence: :func:`rich.cells.chop_cells` does the cut) and gets
      a trailing ``…``.
    * If further non-empty lines follow, ``" (+N lines)"`` is appended, where
      ``N`` counts those further non-empty lines (blank lines do not count).

    Only the human table path uses this; ``--json`` and ``--format csv`` keep
    the full note.
    """
    if value is None:
        return ""
    lines = [line.strip() for line in str(value).splitlines()]
    non_empty = [line for line in lines if line]
    if not non_empty:
        return ""
    first = non_empty[0]
    if cell_len(first) > NOTES_CELL_LENGTH:
        first = chop_cells(first, NOTES_CELL_LENGTH)[0] + "…"
    extra_lines = len(non_empty) - 1
    if extra_lines:
        return f"{first} (+{extra_lines} lines)"
    return first


def _table_cell(name: str, value: Any) -> str:
    """Format one table cell: notes columns get the shared short preview."""
    if name in NOTES_COLUMNS:
        return notes_cell(value)
    return _cell(value)


def _dump(payload: Any) -> None:
    typer.echo(
        json.dumps(
            payload,
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
            default=json_default,
        )
    )


# ---------------------------------------------------------------------------
# Public emitters
# ---------------------------------------------------------------------------


def emit_payload(payload: Any, *, fmt: str) -> None:
    """Print one already-shaped payload as JSON (the only generic form)."""
    if fmt == "json":
        _dump(payload)
        return
    raise UsageError(f"format {fmt!r} is not supported for this command")


def emit_read(
    data: Any,
    *,
    fmt: str,
    columns: list[str] | None = None,
    empty: str = "(none)",
    short_uuid_columns: Sequence[str] = (),
) -> None:
    """Print one record or a list of records in the requested format.

    ``short_uuid_columns`` names list-table columns whose uuid ids are shown as
    unique short prefixes; JSON and CSV always keep the full ids (they are the
    machine formats, like git plumbing).
    """
    if fmt == "json":
        _dump(data)
        return

    if isinstance(data, dict):
        _emit_dict(data, fmt=fmt)
        return

    rows = [dict(row) for row in data]
    if fmt == "csv":
        typer.echo(_csv(rows, columns=columns))
        return
    _shorten_uuid_columns(rows, short_uuid_columns)
    typer.echo(_table(rows, columns=columns, empty=empty))


def emit_write(
    ledger: Any,
    result: dict[str, Any],
    message: str,
    *,
    as_json: bool,
) -> None:
    """Report a write command's result (or its SQL under ``--dry-run``)."""
    session = getattr(ledger, "last_write", None)
    if session is not None and session.dry_run:
        if as_json:
            _dump(
                {
                    "dry_run": True,
                    "branch": target_branch(ledger),
                    "sql": session.sql,
                    "result": result,
                }
            )
        else:
            echo_dry_run_sql(ledger, session.sql)
        return
    if as_json:
        _dump(result)
    else:
        typer.echo(message)


# ---------------------------------------------------------------------------
# Table / CSV helpers
# ---------------------------------------------------------------------------


def _emit_dict(data: dict[str, Any], *, fmt: str) -> None:
    if fmt == "csv":
        typer.echo(_csv([data], columns=sorted(data)))
        return
    table = Table(box=None, show_header=False, pad_edge=False)
    table.add_column("field", style="bold")
    table.add_column("value")
    for key, value in data.items():
        table.add_row(str(key), _cell(value))
    Console().print(table)


def _table(
    rows: list[dict[str, Any]], *, columns: list[str] | None, empty: str
) -> str:
    if not rows:
        return empty
    names = list(columns) if columns is not None else list(rows[0])
    table = Table(box=None, show_header=True, header_style="bold", pad_edge=False)
    for name in names:
        table.add_column(str(name))
    for row in rows:
        table.add_row(*(_table_cell(name, row.get(name)) for name in names))
    buffer = io.StringIO()
    console = Console(
        file=buffer,
        width=200,
        color_system=None,
        force_terminal=False,
        highlight=False,
        legacy_windows=False,
    )
    console.print(table)
    return buffer.getvalue().rstrip("\n")


def _csv(rows: list[dict[str, Any]], *, columns: list[str] | None = None) -> str:
    if not rows and columns is None:
        return ""
    names = list(columns) if columns is not None else list(rows[0])
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(names)
    for row in rows:
        writer.writerow([_cell(row.get(name)) for name in names])
    return buffer.getvalue().rstrip("\n")
