"""``ledger entity`` commands (spec §6)."""

from __future__ import annotations

import typer

from doltledger.cli.objects_common import emit_read, emit_write
from doltledger.cli.options import json_output, resolve_ledger

entity_app = typer.Typer(
    help="Entities: the abstract thing a stock is a batch of.",
    no_args_is_help=True,
)


@entity_app.command("add")
def add_entity_command(
    ctx: typer.Context,
    kind: str = typer.Argument(..., help="Entity kind (e.g. plasmid, item)."),
    set_values: list[str] = typer.Option(
        [], "--set", "-s", help="Field value as field=value; repeat for more fields."
    ),
    name: str | None = typer.Option(
        None, "--name", "-n", help="Entity name (also fills a kind's name field)."
    ),
    alias: list[str] = typer.Option(
        [], "--alias", "-a", help="Alias for the new entity; repeat for several."
    ),
    explicit_id: str | None = typer.Option(
        None, "--id", help="Explicit entity id (back-filling/migration)."
    ),
    as_json: bool = typer.Option(False, "--json", help="Print the created object as JSON."),
) -> None:
    """Create an entity and its kind-specific row."""
    ledger = resolve_ledger(ctx)
    try:
        result = ledger.entities.add(
            kind,
            values=set_values,
            name=name,
            aliases=alias,
            id=explicit_id,
        )
        message = f"Added entity {result['kind']} {result['id']}"
        if result.get("aliases"):
            message += " (alias: " + ", ".join(result["aliases"]) + ")"
        emit_write(ledger, result, message, as_json=json_output(ctx, as_json))
    finally:
        ledger.close()


@entity_app.command("edit")
def edit_entity_command(
    ctx: typer.Context,
    ref: str = typer.Argument(..., help="Entity id or alias."),
    set_values: list[str] = typer.Option(
        [], "--set", "-s", help="Field value as field=value; repeat for more fields."
    ),
    as_json: bool = typer.Option(False, "--json", help="Print the changed object as JSON."),
) -> None:
    """Update fields of an existing entity."""
    ledger = resolve_ledger(ctx)
    try:
        result = ledger.entities.edit(ref, values=set_values)
        emit_write(
            ledger,
            result,
            f"Edited entity {result['kind']} {result['id']}",
            as_json=json_output(ctx, as_json),
        )
    finally:
        ledger.close()


@entity_app.command("show")
def show_entity_command(
    ctx: typer.Context,
    ref: str = typer.Argument(..., help="Entity id or alias."),
    as_json: bool = typer.Option(False, "--json", help="Print JSON."),
) -> None:
    """Show one entity (core columns plus kind fields)."""
    ledger = resolve_ledger(ctx)
    try:
        emit_read(ledger.entities.show(ref), as_json=json_output(ctx, as_json))
    finally:
        ledger.close()


@entity_app.command("ls")
def list_entities_command(
    ctx: typer.Context,
    kind: str | None = typer.Argument(None, help="Only this entity kind."),
    where: list[str] = typer.Option(
        [], "--where", "-w", help="Filter as field=value; repeat to AND filters."
    ),
    as_json: bool = typer.Option(False, "--json", help="Print JSON."),
) -> None:
    """List entities (optionally one kind and/or filtered)."""
    ledger = resolve_ledger(ctx)
    try:
        rows = ledger.entities.ls(kind, where=where)
        emit_read(rows, as_json=json_output(ctx, as_json), columns=["id", "kind", "name"])
    finally:
        ledger.close()
