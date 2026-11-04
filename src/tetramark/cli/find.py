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
    query: str | None = typer.Argument(
        None, help="Object id, alias, name, or text to search for."
    ),
    regex: bool = typer.Option(
        False,
        "--regex",
        "-E",
        help="Treat QUERY as a regular expression (case-insensitive).",
    ),
    where: list[str] = typer.Option(
        [],
        "--where",
        "-w",
        help="Filter as field=value (exact) or field~pattern (regex); repeat to "
        "AND filters. Aliquots are excluded; with no QUERY all matching objects "
        "are listed.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Print JSON."),
) -> None:
    """Find objects by id, alias, name, kind text or --where filters."""
    text = "" if query is None else str(query)
    ledger = resolve_ledger(ctx)
    try:
        results = ledger.find(query, regex=regex, where=where)
    finally:
        ledger.close()
    if not results:
        if text.strip():
            raise LedgerError(f"no match for {query!r}")
        raise LedgerError("no objects match the --where filters")
    emit_read(
        results,
        fmt=output_format(ctx, as_json),
        columns=FIND_COLUMNS,
    )
