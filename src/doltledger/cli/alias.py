"""``ledger alias`` commands (spec §6)."""

from __future__ import annotations

import typer

from doltledger.cli.objects_common import emit_write
from doltledger.cli.options import json_output, resolve_ledger

alias_app = typer.Typer(
    help="Aliases: short human names for any entity/stock/aliquot/box.",
    no_args_is_help=True,
)


@alias_app.command("add")
def add_alias_command(
    ctx: typer.Context,
    object_type: str = typer.Argument(
        ..., help="entity, stock, aliquot or box."
    ),
    object_id: str = typer.Argument(..., help="ID of the object."),
    alias: str = typer.Argument(..., help="Alias text (unique per object type)."),
    as_json: bool = typer.Option(False, "--json", help="Print the alias as JSON."),
) -> None:
    """Attach an alias to an object."""
    ledger = resolve_ledger(ctx)
    try:
        result = ledger.aliases.add(object_type, object_id, alias)
        emit_write(
            ledger,
            result,
            f"Alias {result['alias']} -> {result['object_type']} "
            f"{result['object_id']}",
            as_json=json_output(ctx, as_json),
        )
    finally:
        ledger.close()


@alias_app.command("rm")
def remove_alias_command(
    ctx: typer.Context,
    first: str = typer.Argument(
        ..., help="Object type (entity, stock, aliquot, box) or the alias itself."
    ),
    alias: str | None = typer.Argument(
        None,
        help="Alias to remove; omit when the first argument already is the alias.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Print the removed alias as JSON."),
) -> None:
    """Remove one alias (from one object type, or from every type)."""
    ledger = resolve_ledger(ctx)
    try:
        object_type = first if alias is not None else None
        text = alias if alias is not None else first
        result = ledger.aliases.rm(object_type, text)
        emit_write(
            ledger,
            result,
            f"Removed alias {result['alias']}",
            as_json=json_output(ctx, as_json),
        )
    finally:
        ledger.close()
