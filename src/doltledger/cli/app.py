"""Typer root for the ``ledger`` command-line interface.

Command groups are registered here as their tickets land; the core objects
(entity/stock/aliquot/box) live in sibling modules and only depend on
:class:`GlobalOptions` for the resolved workspace and ledger.
"""

from __future__ import annotations

from pathlib import Path

import typer

from doltledger import __version__
from doltledger.cli.options import GlobalOptions
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
) -> None:
    """A version-controlled inventory ledger on Dolt."""
    ctx.obj = GlobalOptions(workspace=workspace, ledger=ledger)


app.add_typer(workspace_app, name="workspace")
app.add_typer(server_app, name="server")


def main() -> None:
    """Console-script entry point; maps :class:`LedgerError` to its exit code."""
    try:
        app()
    except LedgerError as error:
        typer.echo(f"Error: {error}", err=True)
        # ``typer.Exit`` is only translated to an exit status inside Click's
        # standalone runner; here we are outside it, so raise SystemExit.
        raise SystemExit(error.exit_code) from error
