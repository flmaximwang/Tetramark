"""``tm stock`` commands (spec §6)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import typer
from rich.console import Console

from tetramark.cli.aliquot import run_add_aliquots
from tetramark.cli.form_commands import run_add_form_command
from tetramark.cli.messages import stock_add_message
from tetramark.cli.objects_common import emit_write
from tetramark.cli.options import (
    echo_dry_run_sql,
    group,
    json_output,
    resolve_ledger,
    write_command,
)
from tetramark.cli.output import emit_read, output_format
from tetramark.cli.related import related_lines, without_related
from tetramark.core.ledger import Ledger
from tetramark.errors import UsageError
from tetramark.form.editor import edit_plain_text
from tetramark.ops.render import render_stock_aliquots

stock_app = group("Stocks: logical batches/preparations of one entity.")


@stock_app.command("add")
@write_command
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
    status: str | None = typer.Option(
        None,
        "--status",
        help=(
            "Status of the new aliquots at creation: any status of the stock "
            "kind's set (creation is not a transition)."
        ),
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
            id=explicit_id,
            status=status,
        )
        emit_write(
            ledger,
            result,
            stock_add_message(result),
            as_json=json_output(ctx, as_json),
        )
    finally:
        ledger.close()


@stock_app.command("add-form")
def add_stock_form_command(
    ctx: typer.Context,
    as_json: bool = typer.Option(False, "--json", help="Print the created stock as JSON."),
) -> None:
    """Interactively create a stock without aliquots (for people; agents use 'add').
    Add aliquots afterwards with 'tm aliquot add-form'."""
    run_add_form_command(ctx, "stock", as_json=as_json)


@stock_app.command("aliquot")
@write_command
def aliquot_stock_command(
    ctx: typer.Context,
    stock: str = typer.Argument(..., help="Stock id or alias."),
    amount: str = typer.Argument(
        ...,
        help="Quantity of each new aliquot (decimal); it comes with UNIT.",
    ),
    unit: str = typer.Argument(..., help="Unit of AMOUNT."),
    count: int = typer.Option(1, "--count", "-n", help="How many aliquots to add."),
    box: str | None = typer.Option(
        None, "--box", "-b", help="Box name (or id/alias) to place aliquots in."
    ),
    slot: list[str] = typer.Option(
        [], "--slot", help="Slot per aliquot, in order; repeat for several."
    ),
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
    """Add aliquots of AMOUNT UNIT each to an existing stock.

    `tm stock aliquot STOCK AMOUNT UNIT [opts]` is exactly
    `tm aliquot add STOCK --quantity AMOUNT --unit UNIT [opts]`: AMOUNT is the
    quantity of each new tube and --count still sets how many tubes to add.
    Both commands share one write, so the output, the Dolt commit and the exit
    codes are identical.
    """
    run_add_aliquots(
        ctx,
        stock,
        count=count,
        box=box,
        slots=slot,
        quantity=amount,
        unit=unit,
        status=status,
        as_json=as_json,
    )


@stock_app.command("show")
def show_stock_command(
    ctx: typer.Context,
    ref: str = typer.Argument(..., help="Stock id or alias."),
    as_json: bool = typer.Option(False, "--json", help="Print JSON."),
) -> None:
    """Show one stock, its kind fields, its related objects and its tubes.

    After the field block, plain `*` lines name the objects the id columns
    point at: the stock's entity (`kind · name`), its parent stock and its
    source aliquot, each with its own entity. `--json` carries the same data
    as `related`; `--format csv` keeps its previous columns (no `related`),
    and `tm aliquot list STOCK` is the tabular tube view.
    """
    ledger = resolve_ledger(ctx)
    try:
        record = ledger.stocks.show(ref)
        fmt = output_format(ctx, as_json)
        if fmt == "json":
            emit_read(record, fmt=fmt)
            return
        if fmt == "csv":
            emit_read(without_related(record), fmt=fmt)
            return
        emit_read(
            {
                key: value
                for key, value in record.items()
                if key not in {"aliquots", "related"}
            },
            fmt="table",
        )
        for line in related_lines(record.get("related")):
            typer.echo(line)
        typer.echo()
        Console().print(render_stock_aliquots(record.get("aliquots") or []))
    finally:
        ledger.close()


@stock_app.command("list")
def list_stocks_command(
    ctx: typer.Context,
    kind: str | None = typer.Argument(None, help="Only this stock kind."),
    all_flag: bool = typer.Option(
        False,
        "--all",
        help="Every stock, including empty and depleted ones.",
    ),
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
    """List stocks: only active stocks (at least one aliquot in stock) by
    default; --all adds empty and depleted stocks. `--where` ANDs, and
    `--where-any` is one OR group ANDed with them.
    """
    ledger = resolve_ledger(ctx)
    try:
        rows = ledger.stocks.ls(
            kind,
            where=where,
            where_any=where_any,
            all_statuses=all_flag,
        )
        emit_read(
            rows,
            fmt=output_format(ctx, as_json),
            columns=[
                "id",
                "kind",
                "entity_id",
                "entity_name",
                "aliquots_in_stock",
                "status",
            ],
            short_uuid_columns=("id", "entity_id"),
        )
    finally:
        ledger.close()


@stock_app.command("edit")
@write_command
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


def _note_editor(ledger: Ledger, ref: str, *, append: bool) -> str | None:
    """The editor result for ``tm stock note``; ``None`` means unchanged.

    A plain edit starts from the current note; a bare ``--append`` starts from
    an empty buffer, because its result is the text to add, not the whole
    note.  An unchanged buffer is detected against exactly the text the editor
    was handed, so whitespace the stored note already carried never counts as
    an edit.
    """
    if append:
        edited = edit_plain_text(
            "", description="text to append to the stock's notes"
        )
        return edited if edited.strip() else None
    current = str(ledger.stocks.show(ref).get("notes") or "")
    edited = edit_plain_text(
        current,
        description="the stock's notes; an empty result needs --clear",
    )
    return edited if edited != current.rstrip("\n") else None


@stock_app.command("note")
@write_command
def note_stock_command(
    ctx: typer.Context,
    ref: str = typer.Argument(..., help="Stock id or alias."),
    text: str | None = typer.Argument(
        None,
        help=(
            "Text to append; requires --append. Positional because this CLI "
            "cannot express an option with an optional value."
        ),
    ),
    append: bool = typer.Option(
        False,
        "--append",
        help="Append TEXT, or the editor's result when TEXT is omitted.",
    ),
    clear: bool = typer.Option(
        False, "--clear", help="Remove the notes (set the notes column to NULL)."
    ),
    as_json: bool = typer.Option(
        False, "--json", help="Print the changed object as JSON."
    ),
) -> None:
    """Edit one stock's notes in $VISUAL/$EDITOR, append to them, or clear them.

    `tm stock note REF [TEXT] [--append] [--clear]`: a bare REF opens the
    current note in the editor, `--append TEXT` appends TEXT, a bare
    `--append` opens an empty buffer and appends its result, and `--clear`
    sets the notes to NULL. TEXT is positional because this typer release
    cannot express an option with an optional value.
    """
    if clear and (append or text is not None):
        raise UsageError("--clear cannot be combined with --append or TEXT")
    if text is not None and not append:
        raise UsageError(
            "TEXT needs --append; run `tm stock note REF` to edit the note"
        )
    ledger = resolve_ledger(ctx)
    try:
        if clear:
            result = ledger.stocks.note(ref, clear=True)
        elif append and text is not None:
            result = ledger.stocks.note(ref, append=text)
        else:
            edited = _note_editor(ledger, ref, append=append)
            if edited is None:
                typer.echo("no changes")
                return
            result = (
                ledger.stocks.note(ref, append=edited)
                if append
                else ledger.stocks.note(ref, text=edited)
            )
        emit_write(
            ledger,
            result,
            f"Edited stock {result['kind']} {result['id']}",
            as_json=json_output(ctx, as_json),
        )
    finally:
        ledger.close()


def _echo_renumber_plan(result: dict[str, Any]) -> None:
    """Print the old → new mapping of a renumber run (stocks then aliquots)."""
    for rename in result["renames"]:
        typer.echo(f"stock   {rename['old']} -> {rename['new']}")
        for aliquot in rename["aliquots"]:
            typer.echo(f"aliquot {aliquot['old']} -> {aliquot['new']}")


@stock_app.command("renumber")
@write_command
def renumber_stocks_command(
    ctx: typer.Context,
    ref: list[str] = typer.Argument(
        None, help="One or more stock ids or aliases (required without --map)."
    ),
    new_id: str | None = typer.Option(
        None,
        "--id",
        help="New stock id for the single REF; must match the stock id strategy.",
    ),
    auto: bool = typer.Option(
        False,
        "--auto",
        help="Generate the new id(s) from the ledger's stock id strategy.",
    ),
    map_path: Path | None = typer.Option(
        None,
        "--map",
        help=(
            "Batch mapping file, one rename per line: OLD<TAB>NEW or OLD,NEW "
            "(OLD alone with --auto); # comments and blank lines are ignored."
        ),
    ),
    as_json: bool = typer.Option(
        False, "--json", help="Print the old -> new mapping as JSON."
    ),
) -> None:
    """Renumber stocks to strategy-shaped ids, keeping the old ids as aliases."""
    ledger = resolve_ledger(ctx)
    try:
        result = ledger.stocks.renumber(
            list(ref or []), new_id=new_id, auto=auto, map_path=map_path
        )
        session = ledger.last_write
        if json_output(ctx, as_json):
            emit_write(ledger, result, "", as_json=True)
        elif session is not None and session.dry_run:
            _echo_renumber_plan(result)
            echo_dry_run_sql(ledger, session.sql)
        else:
            _echo_renumber_plan(result)
            typer.echo(
                f"Renumbered {len(result['renames'])} stock(s) in one commit"
            )
    finally:
        ledger.close()


@stock_app.command("purge")
@write_command
def purge_stock_command(
    ctx: typer.Context,
    ref: str = typer.Argument(..., help="Stock id or alias."),
    as_json: bool = typer.Option(False, "--json", help="Print the purged object as JSON."),
) -> None:
    """Hard-delete a stock and its own aliquots (explicit; no other reference may remain)."""
    ledger = resolve_ledger(ctx)
    try:
        result = ledger.stocks.purge(ref)
        emit_write(
            ledger,
            result,
            f"Purged stock {result['id']} ({result['aliquots']} aliquot(s), "
            f"{result['events']} event(s) removed)",
            as_json=json_output(ctx, as_json),
        )
    finally:
        ledger.close()
