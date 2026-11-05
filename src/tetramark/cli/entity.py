"""``tm entity`` commands (spec §6)."""

from __future__ import annotations

import typer

from tetramark.cli.form_commands import run_add_form_command
from tetramark.cli.messages import entity_add_message
from tetramark.cli.objects_common import emit_write
from tetramark.cli.options import group, json_output, resolve_ledger, write_command
from tetramark.cli.output import emit_read, output_format

entity_app = group("Entities: the abstract thing a stock is a batch of.")


@entity_app.command("add")
@write_command
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
        emit_write(
            ledger,
            result,
            entity_add_message(result),
            as_json=json_output(ctx, as_json),
        )
    finally:
        ledger.close()


@entity_app.command("add-form")
def add_entity_form_command(
    ctx: typer.Context,
    as_json: bool = typer.Option(False, "--json", help="Print the created entity as JSON."),
) -> None:
    """Interactively create an entity (for people; agents use 'add')."""
    run_add_form_command(ctx, "entity", as_json=as_json)


@entity_app.command("show")
def show_entity_command(
    ctx: typer.Context,
    ref: str = typer.Argument(..., help="Entity id or alias."),
    as_json: bool = typer.Option(False, "--json", help="Print JSON."),
) -> None:
    """Show one entity (core columns plus kind fields)."""
    ledger = resolve_ledger(ctx)
    try:
        emit_read(
            ledger.entities.show(ref), fmt=output_format(ctx, as_json)
        )
    finally:
        ledger.close()


@entity_app.command("list")
def list_entities_command(
    ctx: typer.Context,
    kind: str | None = typer.Argument(None, help="Only this entity kind."),
    where: list[str] = typer.Option(
        [],
        "--where",
        "-w",
        help="Filter as 'field=value' (exact), 'field~pattern' (regex), "
        "'field!=value', 'field!~pattern' or 'field<value' / '<=' / '>' / "
        "'>='; repeat to AND filters; 'FIELD=' matches empty, 'FIELD!=' "
        "matches a value. A regex is case-sensitive; '(?i)' ignores case.",
    ),
    where_any: list[str] = typer.Option(
        [],
        "--where-any",
        help="One OR group: a row must match one of these filters, ANDed "
        "with every --where. Same syntax and operators as --where; a field "
        "may repeat here, e.g. 'resistance=kan' or 'resistance=amp'.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Print JSON."),
) -> None:
    """List entities (optionally one kind and/or filtered)."""
    ledger = resolve_ledger(ctx)
    try:
        rows = ledger.entities.ls(kind, where=where, where_any=where_any)
        emit_read(
            rows,
            fmt=output_format(ctx, as_json),
            columns=[
                "id",
                "kind",
                "name",
                "stocks_available",
                "aliquots_in_stock",
            ],
            short_uuid_columns=("id",),
        )
    finally:
        ledger.close()


@entity_app.command("edit")
@write_command
def edit_entity_command(
    ctx: typer.Context,
    ref: str = typer.Argument(..., help="Entity id or alias."),
    set_values: list[str] = typer.Option(
        [], "--set", "-s", help="Field value as field=value; repeat for more fields."
    ),
    as_json: bool = typer.Option(False, "--json", help="Print the changed object as JSON."),
) -> None:
    """Update notes or kind fields; names change only through rename."""
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


@entity_app.command("rename")
@write_command
def rename_entity_command(
    ctx: typer.Context,
    ref: str = typer.Argument(..., help="Entity id or alias."),
    new_name: str = typer.Argument(..., help="New entity name."),
    as_json: bool = typer.Option(False, "--json", help="Print the renamed entity as JSON."),
) -> None:
    """Rename an entity; the old name stays as an alias."""
    ledger = resolve_ledger(ctx)
    try:
        result = ledger.entities.rename(ref, new_name)
        emit_write(
            ledger,
            result,
            f"Renamed entity {result['old_name']} -> {result['name']} "
            f"(old name kept as alias)",
            as_json=json_output(ctx, as_json),
        )
    finally:
        ledger.close()


@entity_app.command("purge")
@write_command
def purge_entity_command(
    ctx: typer.Context,
    ref: str = typer.Argument(..., help="Entity id or alias."),
    as_json: bool = typer.Option(False, "--json", help="Print the purged object as JSON."),
) -> None:
    """Hard-delete an entity added by mistake (explicit; no reference may remain)."""
    ledger = resolve_ledger(ctx)
    try:
        result = ledger.entities.purge(ref)
        emit_write(
            ledger,
            result,
            f"Purged entity {result['kind']} {result['id']} "
            f"({result['aliases']} alias(es) removed)",
            as_json=json_output(ctx, as_json),
        )
    finally:
        ledger.close()
