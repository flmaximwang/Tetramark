"""``tm box`` commands (spec §6)."""

from __future__ import annotations

import typer
from rich.console import Console

from tetramark.cli.form_commands import run_add_form_command
from tetramark.cli.messages import box_add_message
from tetramark.cli.objects_common import emit_write
from tetramark.cli.options import group, json_output, resolve_ledger, write_command
from tetramark.cli.output import emit_payload, emit_read, output_format
from tetramark.ops.render import box_csv_rows, render_box

box_app = group("Boxes: containers with a geometry; they nest inside each other.")


@box_app.command("add")
@write_command
def add_box_command(
    ctx: typer.Context,
    kind: str = typer.Argument(..., help="Box kind (e.g. grid8x12, rack, shelf)."),
    name: str = typer.Argument(..., help="Unique human box name (e.g. WFL-08-01)."),
    parent: str | None = typer.Option(
        None,
        "--in",
        "-i",
        help="Parent box name/id/alias to nest this box in (not a grid box).",
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
            box_add_message(result),
            as_json=json_output(ctx, as_json),
        )
    finally:
        ledger.close()


@box_app.command("add-form")
def add_box_form_command(
    ctx: typer.Context,
    as_json: bool = typer.Option(False, "--json", help="Print the created box as JSON."),
) -> None:
    """Interactively create a box (for people; agents use 'add')."""
    run_add_form_command(ctx, "box", as_json=as_json)


@box_app.command("show")
def show_box_command(
    ctx: typer.Context,
    ref: str = typer.Argument(..., help="Box name, id or alias."),
    as_json: bool = typer.Option(False, "--json", help="Print JSON."),
) -> None:
    """Show a box as a grid/list (or its JSON shape)."""
    ledger = resolve_ledger(ctx)
    try:
        view = ledger.boxes.show(ref)
        fmt = output_format(ctx, as_json)
        if fmt == "json":
            emit_payload(view, fmt=fmt)
        elif fmt == "csv":
            emit_read(
                box_csv_rows(view),
                fmt="csv",
                columns=["slot", "row", "col", "occupants"],
            )
        else:
            Console().print(render_box(view))
    finally:
        ledger.close()


@box_app.command("list")
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
        emit_read(
            rows,
            fmt=output_format(ctx, as_json),
            columns=["name", "kind", "path"],
        )
    finally:
        ledger.close()


@box_app.command("move")
@write_command
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


@box_app.command("rename")
@write_command
def rename_box_command(
    ctx: typer.Context,
    ref: str = typer.Argument(..., help="Box name, id or alias."),
    new_name: str = typer.Argument(..., help="New box name (view-safe, unique ignoring case)."),
    as_json: bool = typer.Option(False, "--json", help="Print the renamed box as JSON."),
) -> None:
    """Rename a box; the old name stays as an alias and its view follows."""
    ledger = resolve_ledger(ctx)
    try:
        result = ledger.boxes.rename(ref, new_name)
        emit_write(
            ledger,
            result,
            f"Renamed box {result['old_name']} -> {result['name']} "
            f"(old name kept as alias)",
            as_json=json_output(ctx, as_json),
        )
    finally:
        ledger.close()
