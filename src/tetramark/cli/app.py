"""Typer root for the ``tm`` command-line interface.

Command groups are registered here as their tickets land; the core objects
(entity/stock/aliquot/box) live in sibling modules and only depend on
:class:`GlobalOptions` for the resolved ledger path and its options.

The root help is grouped into five Rich panels -- Setup, Inventory, Lookup,
Data and Versioning -- that follow the order of a real session: set the ledger
up, work with the objects in it, look things up, move data in and out, then
version it.  A ledger is one self-contained directory, so there is no
workspace layer and no server group.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import typer
from typer.core import TyperGroup

from tetramark import __version__
from tetramark.cli.alias import alias_app
from tetramark.cli.aliquot import aliquot_app
from tetramark.cli.box import box_app
from tetramark.cli.branch import branch_app
from tetramark.cli.check import check_command
from tetramark.cli.config import config_app
from tetramark.cli.csv_io import export_command, import_command
from tetramark.cli.entity import entity_app
from tetramark.cli.find import find_command
from tetramark.cli.history import history_command
from tetramark.cli.ledger import init_command, presets_app
from tetramark.cli.lineage import lineage_command
from tetramark.cli.options import (
    HELP_CONTEXT_SETTINGS,
    GlobalOptions,
    help_on_no_command,
)
from tetramark.cli.remote import backup_command, remote_app
from tetramark.cli.schema import schema_app
from tetramark.cli.stock import stock_app
from tetramark.errors import EXIT_OK, LedgerError

#: Names of the Rich help panels of the root command tree.  They are defined
#: once here so that the CLI and its tests always agree on one spelling.
PANEL_SETUP = "Setup"
PANEL_INVENTORY = "Inventory"
PANEL_LOOKUP = "Lookup"
PANEL_DATA = "Data"
PANEL_VERSIONING = "Versioning"

#: The panels of the root help, in the order they are shown.
ROOT_PANELS: tuple[str, ...] = (
    PANEL_SETUP,
    PANEL_INVENTORY,
    PANEL_LOOKUP,
    PANEL_DATA,
    PANEL_VERSIONING,
)

#: Every root command and command group, in the order the root help lists
#: them.  This tuple is the single source of truth for the order of the
#: panels and of the commands inside them.
ROOT_COMMAND_ORDER: tuple[str, ...] = (
    # Setup
    "presets",
    "init",
    "schema",
    "config",
    # Inventory
    "entity",
    "stock",
    "aliquot",
    "box",
    # Lookup
    "find",
    "alias",
    "history",
    "lineage",
    # Data
    "import",
    "export",
    "check",
    # Versioning
    "branch",
    "remote",
    "backup",
)


class _RootGroup(TyperGroup):
    """The root command group, listing its commands in the workflow order.

    Typer builds a group from two lists: every standalone command first and
    every command group afterwards, whatever order the ``app.command()`` and
    ``app.add_typer()`` calls appear in.  The root help would therefore list
    all commands before all groups, and the panels would follow that order
    too.  Reordering ``self.commands`` here fixes both at once:
    ``TyperGroup.list_commands`` returns the names in insertion order and the
    Rich help walks them in that order, printing one panel per panel name.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        ordered = {
            name: self.commands[name]
            for name in ROOT_COMMAND_ORDER
            if name in self.commands
        }
        # A command that is not named in ROOT_COMMAND_ORDER keeps its own
        # place at the end, so nothing ever disappears from the help.
        ordered.update(
            {
                name: command
                for name, command in self.commands.items()
                if name not in ordered
            }
        )
        self.commands = ordered


app = typer.Typer(
    name="tm",
    help="A version-controlled inventory ledger on Dolt.",
    no_args_is_help=False,
    invoke_without_command=True,
    context_settings=HELP_CONTEXT_SETTINGS,
    add_completion=False,
    cls=_RootGroup,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"tetramark {__version__}")
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
    ledger: Path | None = typer.Option(
        None,
        "--ledger",
        "-l",
        help=(
            "Ledger directory: the one holding .dolt/ and ledger.yaml (default: "
            "search upward from the current directory)."
        ),
    ),
    branch: str | None = typer.Option(
        None,
        "--branch",
        "-b",
        help=(
            "Session branch: all reads, writes and commits of this command happen "
            "on BRANCH (default: 'branch:' in ledger.yaml, else the checked-out "
            "branch). A missing branch is an error; it is never created. This is "
            "not the remote branch: 'tm remote push/pull -b' keeps naming the "
            "branch on the remote."
        ),
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
        ledger=ledger,
        branch=branch,
        dry_run=dry_run,
        no_commit=no_commit,
        json=as_json,
        format=None if output_format is None else output_format.lower(),
    )
    help_on_no_command(ctx)


# -- Setup -------------------------------------------------------------------
app.add_typer(presets_app, name="presets", rich_help_panel=PANEL_SETUP)
app.command("init", rich_help_panel=PANEL_SETUP)(init_command)
app.add_typer(schema_app, name="schema", rich_help_panel=PANEL_SETUP)
app.add_typer(config_app, name="config", rich_help_panel=PANEL_SETUP)

# -- Inventory ---------------------------------------------------------------
app.add_typer(entity_app, name="entity", rich_help_panel=PANEL_INVENTORY)
app.add_typer(stock_app, name="stock", rich_help_panel=PANEL_INVENTORY)
app.add_typer(aliquot_app, name="aliquot", rich_help_panel=PANEL_INVENTORY)
app.add_typer(box_app, name="box", rich_help_panel=PANEL_INVENTORY)

# -- Lookup ------------------------------------------------------------------
app.command("find", rich_help_panel=PANEL_LOOKUP)(find_command)
app.add_typer(alias_app, name="alias", rich_help_panel=PANEL_LOOKUP)
app.command("history", rich_help_panel=PANEL_LOOKUP)(history_command)
app.command("lineage", rich_help_panel=PANEL_LOOKUP)(lineage_command)

# -- Data --------------------------------------------------------------------
app.command("import", rich_help_panel=PANEL_DATA)(import_command)
app.command("export", rich_help_panel=PANEL_DATA)(export_command)
app.command("check", rich_help_panel=PANEL_DATA)(check_command)

# -- Versioning --------------------------------------------------------------
app.add_typer(branch_app, name="branch", rich_help_panel=PANEL_VERSIONING)
app.add_typer(remote_app, name="remote", rich_help_panel=PANEL_VERSIONING)
app.command("backup", rich_help_panel=PANEL_VERSIONING)(backup_command)


def main() -> None:
    """Console-script entry point; maps :class:`LedgerError` to its exit code."""
    try:
        app()
    except LedgerError as error:
        typer.echo(f"Error: {error}", err=True)
        # ``typer.Exit`` is only translated to an exit status inside Click's
        # standalone runner; here we are outside it, so raise SystemExit.
        raise SystemExit(error.exit_code) from error
