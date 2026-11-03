"""Shared CLI plumbing: the global option values and the help-preserving groups.

``GlobalOptions`` is resolved once by the root callback; :func:`group` builds the
command groups whose help answers ``-h`` and a bare invocation (see
:func:`help_on_no_command`).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import typer

from tetramark.core.ledger import Ledger, open as open_ledger
from tetramark.errors import EXIT_OK

#: ``--help`` is spelled ``-h`` too, on every command and command group. Click
#: inherits ``help_option_names`` from the root context down the command tree,
#: so the root alone would be enough; groups carry it too, so a group never
#: depends on how it was reached.
HELP_CONTEXT_SETTINGS: dict[str, list[str]] = {"help_option_names": ["-h", "--help"]}


def help_on_no_command(ctx: typer.Context) -> None:
    """Print this command's help and succeed when no subcommand was given.

    Asking for help is not a usage error: a bare ``tm`` or ``tm box`` exits 0
    after printing its help. Real misuse -- an unknown option, an unknown
    command, a missing argument -- still exits 2 through Click's own usage
    handling.
    """
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help(), color=ctx.color)
        raise typer.Exit(EXIT_OK)


def group(help: str, *, name: str | None = None) -> typer.Typer:
    """A command group whose help answers ``-h`` and a bare invocation."""
    return typer.Typer(
        name=name,
        help=help,
        no_args_is_help=False,
        invoke_without_command=True,
        context_settings=HELP_CONTEXT_SETTINGS,
        callback=help_on_no_command,
    )


@dataclass
class GlobalOptions:
    """Values of the global options, passed to subcommands via ``ctx.obj``."""

    #: ``-l/--ledger PATH``: the ledger directory; ``None`` = discover upward.
    ledger: Path | None = None
    branch: str | None = None
    dry_run: bool = False
    no_commit: bool = False
    json: bool = False
    format: str | None = None


def json_output(ctx: typer.Context, local: bool = False) -> bool:
    """True when either the command's ``--json`` or the global one is set."""
    options = ctx.obj
    return bool(local or (isinstance(options, GlobalOptions) and options.json))


def resolve_ledger(ctx: typer.Context) -> Ledger:
    """Open the ledger selected by ``-l/--ledger`` or discovered from the cwd."""
    options = ctx.obj
    if not isinstance(options, GlobalOptions):
        raise RuntimeError("the CLI root callback did not set GlobalOptions")
    return open_ledger(
        options.ledger,
        branch=options.branch,
        dry_run=options.dry_run,
        no_commit=options.no_commit,
    )


def target_branch(ledger: Ledger) -> str:
    """The branch this command operates on (the checked-out one when unset)."""
    selected = getattr(ledger, "branch", None)
    if selected:
        return str(selected)
    try:
        rows = ledger.driver.query("SELECT active_branch() AS `branch`")
    except Exception:  # pragma: no cover - a dry run must not fail on this
        return ""
    if not rows:
        return ""
    value = rows[0].get("branch")
    return "" if value is None else str(value)


def echo_dry_run_sql(ledger: Ledger, sql: str) -> None:
    """Print a dry run's SQL, naming the target branch first (spec A3)."""
    branch = target_branch(ledger)
    if branch:
        typer.echo(f"-- target branch: {branch}")
    typer.echo(sql)
