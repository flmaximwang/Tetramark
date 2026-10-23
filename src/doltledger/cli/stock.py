"""``ledger stock`` commands (spec §6)."""

from __future__ import annotations

import typer

from doltledger.cli.objects_common import emit_write
from doltledger.cli.options import json_output, resolve_ledger
from doltledger.cli.output import emit_read, output_format

stock_app = typer.Typer(
    help="Stocks: logical batches/preparations of one entity.",
    no_args_is_help=True,
)


@stock_app.command("add")
def add_stock_command(
    ctx: typer.Context,
    kind: str = typer.Argument(..., help="Stock kind (e.g. bacterial, lot)."),
    entity: str = typer.Option(..., "--entity", "-e", help="Entity id or alias."),
    set_values: list[str] = typer.Option(
        [], "--set", "-s", help="Field value as field=value; repeat for more fields."
    ),
    parent: str | None = typer.Option(
        None, "--parent", help="Parent stock id or alias (lineage)."
    ),
    source_aliquot: str | None = typer.Option(
        None, "--source-aliquot", help="Aliquot id or alias used to make this stock."
    ),
    aliquots: int = typer.Option(
        1, "--aliquots", help="Number of aliquots to create (0 for none)."
    ),
    box: str | None = typer.Option(
        None, "--box", "-b", help="Box name (or id/alias) to place aliquots in."
    ),
    slot: list[str] = typer.Option(
        [], "--slot", help="Slot per aliquot, in order; repeat for several."
    ),
    quantity: str | None = typer.Option(
        None, "--quantity", "-q", help="Quantity for each new aliquot."
    ),
    unit: str | None = typer.Option(None, "--unit", "-u", help="Unit of --quantity."),
    aliquot_values: list[str] = typer.Option(
        [],
        "--aliquot-set",
        help="Kind aliquot field as field=value; repeat for more fields.",
    ),
    explicit_id: str | None = typer.Option(
        None, "--id", help="Explicit stock id (back-filling/migration)."
    ),
    as_json: bool = typer.Option(False, "--json", help="Print the created object as JSON."),
) -> None:
    """Create a stock (one aliquot by default) and optionally place it."""
    ledger = resolve_ledger(ctx)
    try:
        result = ledger.stocks.add(
            kind,
            entity=entity,
            values=set_values,
            parent=parent,
            source_aliquot=source_aliquot,
            aliquots=aliquots,
            box=box,
            slots=slot,
            quantity=quantity,
            unit=unit,
            aliquot_values=aliquot_values,
            id=explicit_id,
        )
        message = (
            f"Added stock {result['kind']} {result['id']} with "
            f"{len(result['aliquots'])} aliquot(s)"
        )
        if result["aliquots"]:
            message += ": " + ", ".join(result["aliquots"])
        emit_write(ledger, result, message, as_json=json_output(ctx, as_json))
    finally:
        ledger.close()


@stock_app.command("edit")
def edit_stock_command(
    ctx: typer.Context,
    ref: str = typer.Argument(..., help="Stock id or alias."),
    set_values: list[str] = typer.Option(
        [], "--set", "-s", help="Field value as field=value; repeat for more fields."
    ),
    as_json: bool = typer.Option(False, "--json", help="Print the changed object as JSON."),
) -> None:
    """Update fields of an existing stock."""
    ledger = resolve_ledger(ctx)
    try:
        result = ledger.stocks.edit(ref, values=set_values)
        emit_write(
            ledger,
            result,
            f"Edited stock {result['kind']} {result['id']}",
            as_json=json_output(ctx, as_json),
        )
    finally:
        ledger.close()


@stock_app.command("show")
def show_stock_command(
    ctx: typer.Context,
    ref: str = typer.Argument(..., help="Stock id or alias."),
    as_json: bool = typer.Option(False, "--json", help="Print JSON."),
) -> None:
    """Show one stock, its kind fields and its aliquots."""
    ledger = resolve_ledger(ctx)
    try:
        emit_read(
            ledger.stocks.show(ref), fmt=output_format(ctx, as_json)
        )
    finally:
        ledger.close()


@stock_app.command("ls")
def list_stocks_command(
    ctx: typer.Context,
    kind: str | None = typer.Argument(None, help="Only this stock kind."),
    where: list[str] = typer.Option(
        [], "--where", "-w", help="Filter as field=value; repeat to AND filters."
    ),
    as_json: bool = typer.Option(False, "--json", help="Print JSON."),
) -> None:
    """List stocks (optionally one kind and/or filtered)."""
    ledger = resolve_ledger(ctx)
    try:
        rows = ledger.stocks.ls(kind, where=where)
        emit_read(
            rows,
            fmt=output_format(ctx, as_json),
            columns=["id", "kind", "entity_id"],
        )
    finally:
        ledger.close()
