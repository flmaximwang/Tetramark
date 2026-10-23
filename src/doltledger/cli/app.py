"""Typer root for the ``ledger`` command-line interface.

Command groups are registered here as their tickets land; the core objects
(entity/stock/aliquot/box) live in sibling modules and only depend on
:class:`GlobalOptions` for the resolved workspace and ledger.
"""

from __future__ import annotations

from pathlib import Path

import typer

from doltledger import __version__
from doltledger.cli.alias import alias_app
from doltledger.cli.aliquot import aliquot_app
from doltledger.cli.box import box_app
from doltledger.cli.check import check_command
from doltledger.cli.config import config_app
from doltledger.cli.entity import entity_app
from doltledger.cli.find import find_command
from doltledger.cli.history import history_command
from doltledger.cli.ledger import init_command, presets_app
from doltledger.cli.options import GlobalOptions
from doltledger.cli.remote import backup_command, remote_app
from doltledger.cli.schema import schema_app
from doltledger.cli.stock import stock_app
from doltledger.cli.workspace import server_app, workspace_app
from doltledger.errors import EXIT_OK, LedgerError

app = typer.Typer(
    name="ledger",
    help="A version-controlled inventory ledger on Dolt.",
    no_args_is_help=True,
    add_completion=False,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"ledger {__version__}")
        raise typer.Exit(EXIT_OK)


@app.callback()
def root(
    ctx: typer.Context,
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show the version and exit.",
    ),
    workspace: Path | None = typer.Option(
        None,
        "--workspace",
        "-w",
        help="Workspace data directory (default: search upward for workspace.yaml).",
    ),
    ledger: str | None = typer.Option(
        None,
        "--ledger",
        "-l",
        help="Ledger name (default: the workspace's default ledger).",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print/collect the SQL of a write without executing it.",
    ),
    no_commit: bool = typer.Option(
        False,
        "--no-commit",
        help="Execute a write but skip its Dolt commit.",
    ),
    as_json: bool = typer.Option(
        False,
        "--json",
        help="Print read results and created/changed objects as JSON.",
    ),
    output_format: str | None = typer.Option(
        None,
        "--format",
        "-f",
        help="Read output format: table, json or csv.",
    ),
) -> None:
    """A version-controlled inventory ledger on Dolt."""
    if output_format is not None and output_format.lower() not in (
        "table",
        "json",
        "csv",
    ):
        raise typer.BadParameter(
            "expected one of table, json, csv", param_hint="--format"
        )
    ctx.obj = GlobalOptions(
        workspace=workspace,
        ledger=ledger,
        dry_run=dry_run,
        no_commit=no_commit,
        json=as_json,
        format=None if output_format is None else output_format.lower(),
    )


app.add_typer(workspace_app, name="workspace")
app.add_typer(server_app, name="server")
app.add_typer(presets_app, name="presets")
app.add_typer(schema_app, name="schema")
app.add_typer(entity_app, name="entity")
app.add_typer(stock_app, name="stock")
app.add_typer(aliquot_app, name="aliquot")
app.add_typer(box_app, name="box")
app.add_typer(alias_app, name="alias")
app.command("init")(init_command)
app.command("check")(check_command)
app.add_typer(remote_app, name="remote")
app.add_typer(config_app, name="config")
app.command("backup")(backup_command)
app.command("find")(find_command)
app.command("history")(history_command)


def main() -> None:
    """Console-script entry point; maps :class:`LedgerError` to its exit code."""
    try:
        app()
    except LedgerError as error:
        typer.echo(f"Error: {error}", err=True)
        # ``typer.Exit`` is only translated to an exit status inside Click's
        # standalone runner; here we are outside it, so raise SystemExit.
        raise SystemExit(error.exit_code) from error
