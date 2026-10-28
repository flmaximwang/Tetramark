"""``tm aliquot`` commands (spec §6)."""

from __future__ import annotations

import typer

from tetramark.cli.objects_common import emit_write
from tetramark.cli.options import json_output, resolve_ledger

aliquot_app = typer.Typer(
    help="Aliquots: the physical tubes that occupy box slots.",
    no_args_is_help=True,
)


@aliquot_app.command("add")
def add_aliquots_command(
    ctx: typer.Context,
    stock: str = typer.Argument(..., help="Stock id or alias."),
    count: int = typer.Option(1, "--count", "-n", help="How many aliquots to add."),
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
    as_json: bool = typer.Option(False, "--json", help="Print the created objects as JSON."),
) -> None:
    """Add aliquots to an existing stock."""
    ledger = resolve_ledger(ctx)
    try:
        result = ledger.aliquots.add(
            stock,
            count=count,
            box=box,
            slots=slot,
            quantity=quantity,
            unit=unit,
        )
        ids = result["ids"]
        message = f"Added {len(ids)} aliquot(s) to {result['stock_id']}"
        if ids:
            message += ": " + ", ".join(ids)
        emit_write(ledger, result, message, as_json=json_output(ctx, as_json))
    finally:
        ledger.close()


@aliquot_app.command("move")
def move_aliquot_command(
    ctx: typer.Context,
    ref: str = typer.Argument(..., help="Aliquot id or alias."),
    box: str = typer.Option(..., "--box", "-b", help="Destination box name/id/alias."),
    slot: str | None = typer.Option(None, "--slot", "-s", help="Destination slot."),
    as_json: bool = typer.Option(False, "--json", help="Print the changed aliquot as JSON."),
) -> None:
    """Move an aliquot to another box/slot."""
    ledger = resolve_ledger(ctx)
    try:
        result = ledger.aliquots.move(ref, box=box, slot=slot)
        location = box if slot is None else f"{box}:{slot}"
        emit_write(
            ledger,
            result,
            f"Moved aliquot {result['id']} to {location}",
            as_json=json_output(ctx, as_json),
        )
    finally:
        ledger.close()


@aliquot_app.command("use")
def use_aliquot_command(
    ctx: typer.Context,
    ref: str = typer.Argument(..., help="Aliquot id or alias."),
    amount: str = typer.Argument(..., help="Amount consumed (decimal)."),
    unit: str = typer.Argument(..., help="Unit; must match the aliquot's unit."),
    purpose: str | None = typer.Option(None, "--purpose", help="Why it was used."),
    note: str | None = typer.Option(None, "--note", help="Free-text note."),
    as_json: bool = typer.Option(False, "--json", help="Print the changed aliquot as JSON."),
) -> None:
    """Consume part or all of an aliquot; reaching zero marks it consumed."""
    ledger = resolve_ledger(ctx)
    try:
        result = ledger.aliquots.use(ref, amount, unit, purpose=purpose, note=note)
        emit_write(
            ledger,
            result,
            f"Used {amount} {unit} from {result['id']}; "
            f"remaining {result['quantity']} {result['unit']} ({result['status']})",
            as_json=json_output(ctx, as_json),
        )
    finally:
        ledger.close()


@aliquot_app.command("status")
def status_aliquot_command(
    ctx: typer.Context,
    ref: str = typer.Argument(..., help="Aliquot id or alias."),
    status: str = typer.Argument(..., help="New status (from the ledger vocabulary)."),
    note: str | None = typer.Option(None, "--note", help="Free-text note."),
    as_json: bool = typer.Option(False, "--json", help="Print the changed aliquot as JSON."),
) -> None:
    """Change an aliquot's status."""
    ledger = resolve_ledger(ctx)
    try:
        result = ledger.aliquots.status(ref, status, note=note)
        emit_write(
            ledger,
            result,
            f"Status of {result['id']}: {result['old_status']} -> {result['status']}",
            as_json=json_output(ctx, as_json),
        )
    finally:
        ledger.close()


@aliquot_app.command("purge")
def purge_aliquot_command(
    ctx: typer.Context,
    ref: str = typer.Argument(..., help="Aliquot id or alias."),
    as_json: bool = typer.Option(False, "--json", help="Print the purged id as JSON."),
) -> None:
    """Hard-delete an aliquot and its events (explicit; no soft status)."""
    ledger = resolve_ledger(ctx)
    try:
        result = ledger.aliquots.purge(ref)
        emit_write(
            ledger,
            result,
            f"Purged aliquot {result['id']} ({result['events']} event(s) removed)",
            as_json=json_output(ctx, as_json),
        )
    finally:
        ledger.close()
