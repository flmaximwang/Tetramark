"""``tm find``: search ids, aliases, names and kind fields (spec §6)."""

from __future__ import annotations

import typer

from tetramark.cli.options import resolve_ledger
from tetramark.cli.output import emit_read, output_format
from tetramark.errors import LedgerError

__all__ = ["find_command"]

#: Columns shown by the table/CSV forms; JSON keeps every field (score too).
FIND_COLUMNS = ["rank", "object", "kind", "id", "summary", "location", "match"]


def find_command(
    ctx: typer.Context,
    query: str = typer.Argument(
        ..., help="Object id, alias, name, or text to search for."
    ),
    as_json: bool = typer.Option(False, "--json", help="Print JSON."),
) -> None:
    """Find objects by exact id, alias, then name/kind text (case-insensitive)."""
    ledger = resolve_ledger(ctx)
    try:
        results = ledger.find(query)
    finally:
        ledger.close()
    if not results:
        raise LedgerError(f"no match for {query!r}")
    emit_read(
        results,
        fmt=output_format(ctx, as_json),
        columns=FIND_COLUMNS,
    )
