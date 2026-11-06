"""``tm aliquot`` commands (spec §6)."""

from __future__ import annotations

from typing import Any

import typer

from tetramark.cli.form_commands import run_add_form_command
from tetramark.cli.messages import aliquots_add_message
from tetramark.cli.objects_common import emit_write
from tetramark.cli.options import group, json_output, resolve_ledger, write_command
from tetramark.cli.output import emit_read, output_format, short_uuid_ids
from tetramark.cli.related import related_lines, without_related
from tetramark.core.objects import plain_decimal
from tetramark.ops.history import event_line

aliquot_app = group(
    "Aliquots: the physical tubes that occupy box slots.\n\n"
    "Change placement with move, quantity with use, and status with status."
)

#: Record fields the human table does not print as key/value rows: ``path``
#: gets its own line, ``related`` its ``*`` lines, ``events`` its own section
#: and ``object`` (always ``aliquot``) is noise for a human.  JSON keeps them
#: all, exactly as the core returns them.
_TEXT_ONLY_KV_KEYS: tuple[str, ...] = ("path", "related", "events", "object")

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
    line after the table, ``related`` because it prints as the ``*`` lines
    between them, ``events`` because it prints as its own section below them,
    and ``object`` is dropped as human noise.
    """
    rows = {
        key: value for key, value in record.items() if key not in _TEXT_ONLY_KV_KEYS
    }
    shortened = short_uuid_ids([rows.get(key) for key in _TEXT_UUID_KEYS])
    for key, value in zip(_TEXT_UUID_KEYS, shortened, strict=True):
        if key in rows:
            rows[key] = value
    return rows


def _csv_record(record: dict[str, Any]) -> dict[str, Any]:
    """The record as the flat CSV row prints it: the tube's own fields only.

    The events are a list of rows, not a cell, so the flat record leaves them
    out; ``tm history ALQ --format csv`` prints them, one row per event.  The
    ``related`` object is dropped too (:func:`~tetramark.cli.related.without_related`),
    so the CSV keeps exactly the columns it had before the relation lines.
    """
    flat = without_related(record)
    return {key: value for key, value in flat.items() if key != "events"}


def _echo_events(events: list[dict[str, Any]]) -> None:
    """The tube's event trail: a count and one line per event, oldest first.

    The lines are the history renderer's own event lines, so the same event
    reads the same here and in ``tm history``.
    """
    typer.echo(f"events  {len(events)}")
    for event in events:
        typer.echo(f"  {event_line(event)}")


def _use_line(result: dict[str, Any]) -> str:
    """The human line of ``tm aliquot use`` (spec §6).

    The line names the amount used and ends with the message, e.g.
    ``Used all 200 ml from X (consumed): <msg>``.  A tube that is emptied
    says "all" (an explicit use of the whole remainder says the same), a
    partial use names what is left, and a tube without a quantity says
    "all of" because the amount is unknown.
    """
    aliquot_id = str(result.get("id"))
    status = str(result.get("status"))
    message = str(result.get("message") or "")
    used_amount = result.get("used_amount")
    used_unit = result.get("used_unit")
    if used_amount is None:
        return f"Used all of {aliquot_id} ({status}): {message}"
    amount_text = f"{used_amount} {used_unit}".strip()
    left = result.get("quantity")
    if left is None or plain_decimal(left) == "0":
        return f"Used all {amount_text} from {aliquot_id} ({status}): {message}"
    left_text = f"{plain_decimal(left)} {used_unit}".strip()
    return (
        f"Used {amount_text} from {aliquot_id} ({status}, {left_text} left): "
        f"{message}"
    )


def run_add_aliquots(
    ctx: typer.Context,
    stock: str,
    *,
    count: int,
    box: str | None,
    slots: list[str],
    quantity: str | None,
    unit: str | None,
    status: str | None,
    as_json: bool,
) -> None:
    """The one write behind ``tm aliquot add`` and ``tm stock aliquot``.

    Both commands are CLI sugar over ``ledger.aliquots.add``; sharing this body
    is what keeps their human line, JSON, Dolt commit and exit codes identical.
    The caller holds the ledger's write lock through ``@write_command``.
    """
    ledger = resolve_ledger(ctx)
    try:
        result = ledger.aliquots.add(
            stock,
            count=count,
            box=box,
            slots=slots,
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
    run_add_aliquots(
        ctx,
        stock,
        count=count,
        box=box,
        slots=slot,
        quantity=quantity,
        unit=unit,
        status=status,
        as_json=as_json,
    )


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
    """Show one tube: its status, quantity, stock, box path and events.

    The record covers the aliquot row, its sorted aliases, the owning stock's
    kind and entity, the box/slot, the path read from the box nesting and the
    tube's own events, oldest first.  The table prints the fields, then the
    `* entity` line of the owning stock's entity (its JSON `related` object),
    the path and one line per event; `--json` prints the whole record, events
    and `related` included, and `--format csv` the flat record without the
    events (they are rows, not a cell: `tm history ALQ --format csv` prints
    them) and without `related`.  The stock kind's own fields are in
    `tm stock show`, and the Dolt commits are in `tm history`.  The rich
    per-stock listing is `tm aliquot list STOCK`.
    """
    ledger = resolve_ledger(ctx)
    try:
        record = ledger.aliquots.show(ref)
        fmt = output_format(ctx, as_json)
        if fmt == "json":
            emit_read(record, fmt=fmt)
            return
        if fmt == "csv":
            emit_read(_csv_record(record), fmt=fmt)
            return
        emit_read(_text_record(record), fmt="table")
        for line in related_lines(record.get("related")):
            typer.echo(line)
        if record.get("path"):
            typer.echo(f"path: {record['path']}")
        _echo_events(list(record.get("events") or []))
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
        help="Filter as field=value; repeat to AND; FIELD= matches empty. "
        "Filter status with --status.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Print JSON."),
) -> None:
    """List the tubes an experiment can use (in stock by default).

    Add `--all` for every status and `--not-in-stock` for the tubes that left
    stock.  The table prints id, stock kind, status, quantity, unit, label and
    path; `--json` prints the same record `tm aliquot show` prints, without
    the events: a list never reads a tube's event trail.
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
    amount: str | None = typer.Argument(
        None,
        help=(
            "Amount consumed (decimal); it comes with UNIT. Leave both out "
            "to use everything left."
        ),
    ),
    unit: str | None = typer.Argument(
        None, help="Unit of AMOUNT; it must match the aliquot's unit."
    ),
    message: str = typer.Option(
        ...,
        "--message",
        "-m",
        help=(
            "Why the material was used (required; trimmed and stored on the "
            "use event)."
        ),
    ),
    as_json: bool = typer.Option(False, "--json", help="Print the changed aliquot as JSON."),
) -> None:
    """Consume part or all of an aliquot; -m/--message is required.

    AMOUNT and UNIT come together: passing only one of them is a usage
    error.  Leaving both out uses everything left, which is the same as
    passing the remaining quantity and its unit.  Reaching zero marks the
    tube ``consumed`` and clears its box slot; the message is trimmed,
    refused when empty, and recorded on the use event.
    """
    ledger = resolve_ledger(ctx)
    try:
        result = ledger.aliquots.use(ref, amount, unit, message=message)
        emit_write(
            ledger,
            result,
            _use_line(result),
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
    message: str | None = typer.Option(
        None,
        "-m",
        "--message",
        help=(
            "Why the status changed; required with --force. Trimmed and "
            "stored in aliquot_event.message."
        ),
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help=(
            "Override a transition rule; requires -m/--message and records "
            "aliquot_event.forced = TRUE (tm check accepts the event)."
        ),
    ),
    as_json: bool = typer.Option(False, "--json", help="Print the changed aliquot as JSON."),
) -> None:
    """Change an aliquot's status, following its stock kind's transition rules."""
    ledger = resolve_ledger(ctx)
    try:
        result = ledger.aliquots.status(ref, status, message=message, force=force)
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


@aliquot_app.command("label")
@write_command
def label_aliquot_command(
    ctx: typer.Context,
    ref: str = typer.Argument(..., help="Aliquot id or alias."),
    label: str = typer.Argument(..., help="The label to write on the tube."),
    replace: bool = typer.Option(
        False,
        "--replace",
        help=(
            "Replace an existing label: the old label is kept as an alias of "
            "the aliquot and the new one is set."
        ),
    ),
    as_json: bool = typer.Option(False, "--json", help="Print the changed aliquot as JSON."),
) -> None:
    """Set an aliquot's label; --replace moves an existing label to its aliases.

    A tube without a label gets LABEL.  A tube that already has a different
    label is refused unless --replace is given; then the old label becomes an
    alias (it keeps resolving the tube) and LABEL is set, in one commit.
    """
    ledger = resolve_ledger(ctx)
    try:
        result = ledger.aliquots.label(ref, label, replace=replace)
        line = f"Labelled aliquot {result['id']} {result['label']!r}"
        if result.get("old_label"):
            line += f" (old label {result['old_label']!r} kept as an alias)"
        emit_write(
            ledger,
            result,
            line,
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
    """Hard-delete an aliquot, its events and its aliases (explicit; no soft status)."""
    ledger = resolve_ledger(ctx)
    try:
        result = ledger.aliquots.purge(ref)
        emit_write(
            ledger,
            result,
            f"Purged aliquot {result['id']} ({result['events']} event(s), "
            f"{result['aliases']} alias(es) removed)",
            as_json=json_output(ctx, as_json),
        )
    finally:
        ledger.close()
