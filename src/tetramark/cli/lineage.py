"""``tm lineage``: the descent graph of entities and stocks (spec §6)."""

from __future__ import annotations

import typer

from tetramark.cli.options import json_output, resolve_ledger
from tetramark.cli.output import emit_payload, output_format
from tetramark.errors import UsageError
from tetramark.ops.lineage import lineage, render_dot, render_text

__all__ = ["TEXT_FORMATS", "lineage_command"]

#: The shapes of the human-readable rendering (``--json`` is the machine one).
TEXT_FORMATS: tuple[str, ...] = ("text", "dot")


def lineage_command(
    ctx: typer.Context,
    ref: str = typer.Argument(..., help="Entity, stock or aliquot id or alias."),
    up: bool = typer.Option(False, "--up", help="Walk to the ancestors (parents)."),
    down: bool = typer.Option(False, "--down", help="Walk to the descendants (children)."),
    depth: int | None = typer.Option(
        None, "--depth", help="Stop after N edges from the root (default: unlimited)."
    ),
    as_json: bool = typer.Option(False, "--json", help="Print JSON."),
    text_format: str | None = typer.Option(
        None, "--format", help="Rendering: text (default) or dot (Graphviz)."
    ),
) -> None:
    """Show where material came from and what derives from it (read-only)."""
    if up and down:
        raise UsageError(
            "--up and --down are mutually exclusive; pass neither for both "
            "directions"
        )
    if depth is not None and depth < 0:
        raise UsageError("--depth must not be negative")
    fmt = _output_format(ctx, as_json, text_format)

    ledger = resolve_ledger(ctx)
    try:
        result = lineage(ledger, ref, up=not down, down=not up, depth=depth)
    finally:
        ledger.close()

    if fmt == "json":
        emit_payload(result.as_payload(), fmt="json")
    elif fmt == "dot":
        typer.echo(render_dot(result))
    else:
        typer.echo(render_text(result))


def _output_format(
    ctx: typer.Context, as_json: bool, text_format: str | None
) -> str:
    """``--json`` beats ``--format``, which beats the global ``--format``."""
    if as_json:
        return "json"
    if text_format is not None:
        selected = text_format.lower()
        if selected not in TEXT_FORMATS:
            raise UsageError(
                f"invalid format {text_format!r}: expected one of "
                f"{', '.join(TEXT_FORMATS)} (or --json)"
            )
        return selected
    if json_output(ctx):
        return "json"
    selected = output_format(ctx)
    if selected == "json":
        return "json"
    if selected == "table":
        return "text"
    raise UsageError(
        "format 'csv' is not supported for tm lineage: use --format text|dot "
        "or --json"
    )
