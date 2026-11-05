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
        help="Filter as 'field=value' (exact), 'field~pattern' (regex), "
        "'field!=value', 'field!~pattern' or 'field<value' / '<=' / '>' / "
        "'>='; repeat to AND filters; 'FIELD=' matches empty, 'FIELD!=' "
        "matches a value. Aliquots are excluded; with no QUERY all matching "
        "objects are listed.",
    ),
    with_rewrite: bool = typer.Option(
        False,
        "--with-rewrite",
        help="Also try the ledger's find.rewrites rules, in addition to QUERY "
        "(literal queries only; original hits rank first).",
    ),
    as_json: bool = typer.Option(False, "--json", help="Print JSON."),
) -> None:
    """Find objects by id, alias, name, kind text or --where filters."""
    text = "" if query is None else str(query).strip()
    ledger = resolve_ledger(ctx)
    try:
        results = ledger.find(
            query, regex=regex, where=where, with_rewrite=with_rewrite
        )
        # A miss names the rewrites that were tried; ``-E`` and a filter-only
        # listing never rewrite, and a failed rewrite is still worth reporting.
        tried = (
            ledger.config.find.rewrites_for(text) if text and not regex else []
        )
    finally:
        ledger.close()
    if not results:
        if text:
            message = f"no match for {text!r}"
            if tried:
                listed = ", ".join(
                    f"{rewrite.query!r} via {rewrite.rule}" for rewrite in tried
                )
                message = f"{message} (also tried: {listed})"
            raise LedgerError(message)
        raise LedgerError("no objects match the --where filters")
    if not with_rewrite and not regex:
        # The default fallback only runs when the query itself had no hits, so
        # an annotated first row is exactly the case the stderr hint explains.
        applied = results[0].get("rewrite")
        if isinstance(applied, dict):
            typer.echo(
                f"no match for {text!r}; showing matches for "
                f"{applied['query']!r} (rule {applied['rule']!r})",
                err=True,
            )
    emit_read(
        results,
        fmt=output_format(ctx, as_json),
        columns=FIND_COLUMNS,
    )
