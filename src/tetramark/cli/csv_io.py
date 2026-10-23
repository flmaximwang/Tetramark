"""``tm import`` and ``tm export`` (spec §6, ticket 12)."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from tetramark.cli.options import json_output, resolve_ledger
from tetramark.ops import io_csv


def import_command(
    ctx: typer.Context,
    kind: str = typer.Argument(
        ...,
        help="Kind to import, a core object (entity/stock/aliquot/box), or 'all'.",
    ),
    file: Path = typer.Argument(
        ...,
        help="CSV file, or a directory when KIND is 'all'.",
    ),
    object_type: str | None = typer.Option(
        None,
        "--object",
        "-o",
        help="Force the object type: entity, stock, aliquot or box.",
    ),
    as_json: bool = typer.Option(
        False, "--json", help="Print the import summary as JSON."
    ),
) -> None:
    """Import a CSV (all rows validated first; one commit or nothing)."""
    ledger = resolve_ledger(ctx)
    try:
        result = io_csv.import_csv(
            ledger,
            kind,
            file,
            object_type=object_type,
            dry_run=ledger.dry_run,
        )
        if result.dry_run:
            if result.sql:
                typer.echo(result.sql)
            else:
                typer.echo("Nothing to import.")
            return
        if json_output(ctx, as_json):
            typer.echo(
                json.dumps(result.as_dict(), indent=2, sort_keys=True, ensure_ascii=False)
            )
            return
        files = ", ".join(path.name for path in result.files)
        detail = ", ".join(
            f"{name}={count}" for name, count in sorted(result.counts.items())
        )
        message = f"Imported {result.rows} row(s) from {files}"
        if detail:
            message += f" ({detail})"
        typer.echo(message)
    finally:
        ledger.close()


def export_command(
    ctx: typer.Context,
    kind: str = typer.Argument(
        "all",
        help="Kind to export (or 'all', boxes, aliquots, aliases).",
    ),
    out: Path | None = typer.Option(
        None,
        "--out",
        "-o",
        help="Directory for the CSV files (default: the current directory).",
    ),
    as_json: bool = typer.Option(
        False, "--json", help="Print the exported file list as JSON."
    ),
) -> None:
    """Export one kind (or everything) as CSVs importable by 'tm import'."""
    ledger = resolve_ledger(ctx)
    try:
        result = io_csv.export_csv(ledger, kind, out_dir=out)
        if json_output(ctx, as_json):
            typer.echo(
                json.dumps(result.as_dict(), indent=2, sort_keys=True, ensure_ascii=False)
            )
            return
        typer.echo(f"Exported {len(result.files)} file(s) to {result.directory}")
        for path in result.files:
            typer.echo(f"{path.name}\t{result.counts.get(path.name, 0)}")
    finally:
        ledger.close()
