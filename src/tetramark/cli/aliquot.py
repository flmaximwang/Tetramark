"""``tm aliquot`` commands (spec §6)."""

from __future__ import annotations

import typer

from tetramark.cli.form_commands import run_add_form_command
from tetramark.cli.messages import aliquots_add_message
from tetramark.cli.objects_common import emit_write
from tetramark.cli.options import group, json_output, resolve_ledger, write_command

aliquot_app = group("Aliquots: the physical tubes that occupy box slots.")


@aliquot_app.command("add")
@write_command
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
    status: str | None = typer.Option(
        None,
        "--status",
        help=(
            "Status at creation: any status of the stock kind's set (creation "
            "is not a transition). Default: stored when --box is given, else "
            "pending (or the set's first in-stock status)."
        ),
    ),
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
            status=status,
        )
        emit_write(
            ledger,
            result,
            aliquots_add_message(result),
            as_json=json_output(ctx, as_json),
        )
    finally:
        ledger.close()


@aliquot_app.command("add-form")
def add_aliquots_form_command(
    ctx: typer.Context,
    as_json: bool = typer.Option(False, "--json", help="Print the created objects as JSON."),
) -> None:
    """Interactively add aliquots to a stock (for people; agents use 'add')."""
    run_add_form_command(ctx, "aliquot", as_json=as_json)


@aliquot_app.command("move")
@write_command
def move_aliquot_command(
    ctx: typer.Context,
    ref: str = typer.Argument(..., help="Aliquot id or alias."),
    box: str = typer.Option(..., "--box", "-b", help="Destination box name/id/alias."),
    slot: str | None = typer.Option(None, "--slot", "-s", help="Destination slot."),
    status: str | None = typer.Option(
        None,
        "--status",
        help=(
            "Status the tube takes when it was not in stock (default: the set's "
            "single in-stock status; required when it has several). When the "
            "tube is already in stock, --status must name another in-stock "
            "status and the normal transition rule applies."
        ),
    ),
    as_json: bool = typer.Option(False, "--json", help="Print the changed aliquot as JSON."),
) -> None:
    """Move an aliquot to another box/slot (placing it puts it back in stock)."""
    ledger = resolve_ledger(ctx)
    try:
        result = ledger.aliquots.move(ref, box=box, slot=slot, status=status)
        location = box if slot is None else f"{box}:{slot}"
        message = f"Moved aliquot {result['id']} to {location}"
        if result.get("old_status") != result.get("status"):
            message += f" (status {result['old_status']} -> {result['status']})"
        emit_write(
            ledger,
            result,
            message,
            as_json=json_output(ctx, as_json),
        )
    finally:
        ledger.close()


@aliquot_app.command("use")
@write_command
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
@write_command
def status_aliquot_command(
    ctx: typer.Context,
    ref: str = typer.Argument(..., help="Aliquot id or alias."),
    status: str = typer.Argument(
        ..., help="New status (from the stock kind's status set)."
    ),
    note: str | None = typer.Option(
        None,
        "--note",
        help="Free-text note; required with --force, recorded on the event.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help=(
            "Override a transition rule; requires --note and records "
            "aliquot_event.forced = TRUE (tm check accepts the event)."
        ),
    ),
    as_json: bool = typer.Option(False, "--json", help="Print the changed aliquot as JSON."),
) -> None:
    """Change an aliquot's status, following its stock kind's transition rules."""
    ledger = resolve_ledger(ctx)
    try:
        result = ledger.aliquots.status(ref, status, note=note, force=force)
        message = (
            f"Status of {result['id']}: {result['old_status']} -> {result['status']}"
        )
        if result.get("forced"):
            message += " (forced)"
        emit_write(
            ledger,
            result,
            message,
            as_json=json_output(ctx, as_json),
        )
    finally:
        ledger.close()


@aliquot_app.command("purge")
@write_command
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
