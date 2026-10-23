"""Shared output helpers for the object command groups (spec §6).

Writes print the SQL under ``--dry-run`` and the created/changed objects under
``--json``.  Reads print a tab-separated table (the richer grid/table renderer
arrives with ticket 11) or JSON.
"""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from typing import Any

import typer

from doltledger.core.ledger import Ledger

__all__ = ["emit_read", "emit_write"]


def _json_default(value: Any) -> Any:
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
    return str(value)


def _dump(payload: Any) -> None:
    typer.echo(
        json.dumps(
            payload,
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
            default=_json_default,
        )
    )


def emit_write(
    ledger: Ledger,
    result: dict[str, Any],
    message: str,
    *,
    as_json: bool,
) -> None:
    """Report the result of a write command (or its SQL under ``--dry-run``)."""
    session = ledger.last_write
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


def emit_read(
    data: Any,
    *,
    as_json: bool,
    columns: list[str] | None = None,
    empty: str = "(none)",
) -> None:
    """Print one record or a list of records."""
    if as_json:
        _dump(data)
        return

    if isinstance(data, dict):
        for key, value in data.items():
            typer.echo(f"{key}\t{_cell(value)}")
        return

    rows = list(data)
    if not rows:
        typer.echo(empty)
        return
    if columns is None:
        columns = list(rows[0])
    typer.echo("\t".join(columns))
    for row in rows:
        typer.echo("\t".join(_cell(row.get(column)) for column in columns))
