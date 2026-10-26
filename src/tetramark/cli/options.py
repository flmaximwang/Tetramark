"""Values of the global CLI options, resolved once by the root callback."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import typer

from tetramark.core.ledger import Ledger, open as open_ledger


@dataclass
class GlobalOptions:
    """Values of the global options, passed to subcommands via ``ctx.obj``."""

    workspace: Path | None = None
    ledger: str | None = None
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
    """Open the ledger selected by the global options."""
    options = ctx.obj
    if not isinstance(options, GlobalOptions):
        raise RuntimeError("the CLI root callback did not set GlobalOptions")
    return open_ledger(
        options.workspace,
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
