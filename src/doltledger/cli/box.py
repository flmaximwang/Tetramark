"""``ledger box`` commands (spec §6; ``box show`` arrives with ticket 11)."""

from __future__ import annotations

import typer

from doltledger.cli.objects_common import emit_read, emit_write
from doltledger.cli.options import json_output, resolve_ledger

box_app = typer.Typer(
    help="Boxes: containers with a geometry; they nest inside each other.",
    no_args_is_help=True,
)


@box_app.command("add")
def add_box_command(
    ctx: typer.Context,
    kind: str = typer.Argument(..., help="Box kind (e.g. grid96, rack, shelf)."),
    name: str = typer.Argument(..., help="Unique human box name (e.g. WFL-08-01)."),
    parent: str | None = typer.Option(
        None, "--in", "-i", help="Parent box name/id/alias to nest this box in."
    ),
    slot: str | None = typer.Option(
        None, "--slot", "-s", help="Slot in the parent box (slotted parents only)."
    ),
    notes: str | None = typer.Option(None, "--notes", help="Free-text notes."),
    as_json: bool = typer.Option(False, "--json", help="Print the created box as JSON."),
) -> None:
    """Create a box and materialise its slots from the kind geometry."""
    ledger = resolve_ledger(ctx)
    try:
        result = ledger.boxes.add(
            kind, name, parent=parent, slot=slot, notes=notes
        )
        emit_write(
            ledger,
            result,
            f"Created box {result['name']} ({result['kind']}) "
            f"with {result['slots']} slot(s)",
            as_json=json_output(ctx, as_json),
        )
    finally:
        ledger.close()


@box_app.command("move")
def move_box_command(
    ctx: typer.Context,
    ref: str = typer.Argument(..., help="Box name, id or alias."),
    parent: str = typer.Option(..., "--in", "-i", help="New parent box."),
    slot: str | None = typer.Option(None, "--slot", "-s", help="Slot in the parent."),
    as_json: bool = typer.Option(False, "--json", help="Print the changed box as JSON."),
) -> None:
    """Move a box inside another box (nesting must stay acyclic)."""
    ledger = resolve_ledger(ctx)
    try:
        result = ledger.boxes.move(ref, parent=parent, slot=slot)
        emit_write(
            ledger,
            result,
            f"Moved box {result['name']} into {parent}",
            as_json=json_output(ctx, as_json),
        )
    finally:
        ledger.close()


@box_app.command("ls")
def list_boxes_command(
    ctx: typer.Context,
    kind: str | None = typer.Option(None, "--kind", "-k", help="Only this box kind."),
    where: list[str] = typer.Option(
        [], "--where", "-w", help="Filter as field=value; repeat to AND filters."
    ),
    as_json: bool = typer.Option(False, "--json", help="Print JSON."),
) -> None:
    """List boxes with their nested path."""
    ledger = resolve_ledger(ctx)
    try:
        rows = ledger.boxes.ls(kind, where=where)
        emit_read(rows, as_json=json_output(ctx, as_json), columns=["name", "kind", "path"])
    finally:
        ledger.close()
