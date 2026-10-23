"""``ledger history``: events and commits for one object (spec §6)."""

from __future__ import annotations

import typer

from doltledger.cli.options import resolve_ledger
from doltledger.cli.output import emit_payload, emit_read, output_format
from doltledger.ops.history import HISTORY_COLUMNS, history_rows, render_history_text

__all__ = ["history_command"]


def history_command(
    ctx: typer.Context,
    ref: str = typer.Argument(..., help="Object id, box name or alias."),
    as_json: bool = typer.Option(False, "--json", help="Print JSON."),
) -> None:
    """Show the aliquot events and Dolt commits touching one object."""
    ledger = resolve_ledger(ctx)
    try:
        result = ledger.history(ref)
    finally:
        ledger.close()
    fmt = output_format(ctx, as_json)
    if fmt == "json":
        emit_payload(result, fmt=fmt)
    elif fmt == "csv":
        emit_read(history_rows(result), fmt="csv", columns=list(HISTORY_COLUMNS))
    else:
        typer.echo(render_history_text(result))
