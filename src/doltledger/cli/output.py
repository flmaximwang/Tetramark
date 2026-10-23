"""Shared read-side output: table, JSON and CSV (spec §6, ticket 11).

Every ``ls``/``show`` command funnels through :func:`emit_read` (or
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
from decimal import Decimal
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from doltledger.cli.options import GlobalOptions
from doltledger.errors import UsageError

__all__ = [
    "FORMATS",
    "emit_payload",
    "emit_read",
    "emit_write",
    "json_default",
    "output_format",
]

FORMATS: tuple[str, ...] = ("table", "json", "csv")


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
) -> None:
    """Print one record or a list of records in the requested format."""
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
            _dump({"dry_run": True, "sql": session.sql, "result": result})
        else:
            typer.echo(session.sql)
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
        table.add_row(*(_cell(row.get(name)) for name in names))
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
