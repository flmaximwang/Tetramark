"""``tm aliquot`` commands (spec §6)."""

from __future__ import annotations

from typing import Any

import typer

from tetramark.cli.form_commands import run_add_form_command
from tetramark.cli.messages import aliquots_add_message
from tetramark.cli.objects_common import emit_write
from tetramark.cli.options import group, json_output, resolve_ledger, write_command
from tetramark.cli.output import emit_read, output_format, short_uuid_ids

aliquot_app = group("Aliquots: the physical tubes that occupy box slots.")

#: Record fields the human table does not print as key/value rows: ``path``
#: gets its own line and ``object`` (always ``aliquot``) is noise for a human.
#: JSON keeps both, exactly as the core returns them.
_TEXT_ONLY_KV_KEYS: tuple[str, ...] = ("path", "object")

#: Record fields holding a uuid id, shortened in the human table only (the way
#: ``tm stock list`` shortens ids); JSON and CSV keep the full ids.
_TEXT_UUID_KEYS: tuple[str, ...] = ("stock_id", "entity_id", "box_id")

#: The narrow columns ``tm aliquot list`` prints in table and CSV form; JSON
#: keeps the full :func:`~tetramark.core.objects.show_aliquot` record.  The
#: aliquot id is never shortened (spec §6), and the set holds no uuid-only
#: column, so there is nothing else for the list to abbreviate.
ALIQUOT_LIST_COLUMNS: tuple[str, ...] = (
    "id",
    "stock_kind",
    "status",
    "quantity",
    "unit",
    "label",
    "path",
)


def _text_record(record: dict[str, Any]) -> dict[str, Any]:
    """One aliquot record as the entity/stock show key/value table renders it.

    The fields keep the record's own order; uuid ids become their shortest
    unique prefixes.  ``path`` is dropped here because it prints on its own
    line after the table, and ``object`` is dropped as human noise.
    """
    rows = {
        key: value for key, value in record.items() if key not in _TEXT_ONLY_KV_KEYS
    }
    shortened = short_uuid_ids([rows.get(key) for key in _TEXT_UUID_KEYS])
    for key, value in zip(_TEXT_UUID_KEYS, shortened, strict=True):
        if key in rows:
            rows[key] = value
    return rows


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


@aliquot_app.command("show")
def show_aliquot_command(
    ctx: typer.Context,
    ref: str = typer.Argument(
        ..., help="Aliquot id (<stock>.<seq>, never shortened) or one of its aliases."
    ),
    as_json: bool = typer.Option(False, "--json", help="Print JSON."),
) -> None:
    """Show one tube: its status, quantity, stock and box path.

    The record covers the aliquot row, its sorted aliases, the owning stock's
    kind and entity, the box/slot and the path read from the box nesting.  The
    tube's events are in `tm history ALQ`; the stock kind's own fields are in
    `tm stock show`, and the rich per-stock listing is `tm aliquot list STOCK`.
    """
    ledger = resolve_ledger(ctx)
    try:
        record = ledger.aliquots.show(ref)
        fmt = output_format(ctx, as_json)
        if fmt != "table":
            emit_read(record, fmt=fmt)
            return
        emit_read(_text_record(record), fmt="table")
        if record.get("path"):
            typer.echo(f"path: {record['path']}")
    finally:
        ledger.close()


@aliquot_app.command("list")
def list_aliquots_command(
    ctx: typer.Context,
    stock: str | None = typer.Argument(
        None, help="Only the tubes of this stock (id or alias)."
    ),
    kind: str | None = typer.Option(
        None, "--kind", "-k", help="Only tubes of this stock kind."
    ),
    box: str | None = typer.Option(
        None,
        "--box",
        "-b",
        help="Only tubes in this box (name/id/alias) or any box nested inside it.",
    ),
    status: list[str] = typer.Option(
        [],
        "--status",
        help=(
            "Only these statuses (repeatable; an explicit status turns the "
            "default in-stock scope off)."
        ),
    ),
    all_statuses: bool = typer.Option(
        False,
        "--all",
        help="Every status, including the tubes that left stock.",
    ),
    not_in_stock: bool = typer.Option(
        False,
        "--not-in-stock",
        help="Only the tubes that are not in stock (missing/consumed/thawed audit).",
    ),
    where: list[str] = typer.Option(
        [],
        "--where",
        "-w",
        help="Filter as field=value; repeat to AND. Filter status with --status.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Print JSON."),
) -> None:
    """List the tubes an experiment can use (in stock by default).

    Add `--all` for every status and `--not-in-stock` for the tubes that left
    stock.  The table prints id, stock kind, status, quantity, unit, label and
    path; `--json` prints the same record `tm aliquot show` prints.
    """
    ledger = resolve_ledger(ctx)
    try:
        rows = ledger.aliquots.ls(
            stock,
            kind=kind,
            box=box,
            status=status,
            in_stock=False if not_in_stock else None,
            all_statuses=all_statuses,
            where=where,
        )
        emit_read(
            rows,
            fmt=output_format(ctx, as_json),
            columns=list(ALIQUOT_LIST_COLUMNS),
        )
    finally:
        ledger.close()


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
    message: str = typer.Option(
        ...,
        "--message",
        "-m",
        help=(
            "Why the tube was moved (required; trimmed and stored on the "
            "move event)."
        ),
    ),
    as_json: bool = typer.Option(False, "--json", help="Print the changed aliquot as JSON."),
) -> None:
    """Move an aliquot to another box/slot; -m/--message is required.

    Placing the tube puts it back in stock when it was not; -m/--message is
    trimmed, refused when empty, and recorded on the move event.
    """
    ledger = resolve_ledger(ctx)
    try:
        result = ledger.aliquots.move(
            ref, box=box, slot=slot, status=status, message=message
        )
        location = box if slot is None else f"{box}:{slot}"
        line = f"Moved aliquot {result['id']} to {location}"
        if result.get("old_status") != result.get("status"):
            line += f" (status {result['old_status']} -> {result['status']})"
        emit_write(
            ledger,
            result,
            line,
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
