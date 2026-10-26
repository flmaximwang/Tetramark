"""``tm branch ...``: list, create and delete Dolt branches (no merge).

Branch selection for a *command* is the global ``-b/--branch`` option: it
opens a ``db/branch`` revision database so the repository's checked-out branch
never moves.  This module manages the branches themselves; merging stays a
native Dolt operation (``CALL DOLT_MERGE(branch, '--ff-only')`` at cutover).
"""

from __future__ import annotations

import json

import typer

from tetramark.cli.options import GlobalOptions, json_output, resolve_ledger
from tetramark.driver.base import render_sql
from tetramark.errors import UsageError

branch_app = typer.Typer(
    help=(
        "List, create and delete branches. Merging is deliberately not part of "
        "tm; cut over with Dolt's own CALL DOLT_MERGE(branch, '--ff-only')."
    ),
    no_args_is_help=True,
)

__all__ = ["branch_app"]


def _active_branch(ledger) -> str | None:
    rows = ledger.driver.query("SELECT active_branch() AS `branch`")
    if not rows:
        return None
    value = rows[0].get("branch")
    return None if value is None else str(value)


def _check_name(name: str) -> str:
    text = str(name).strip()
    if not text:
        raise UsageError("a branch name must not be empty")
    return text


@branch_app.command("ls")
def branch_ls(ctx: typer.Context) -> None:
    """List branches; the checked-out one (what other sessions see) is marked."""
    ledger = resolve_ledger(ctx)
    rows = ledger.driver.query(
        "SELECT `name`, `hash` FROM dolt_branches ORDER BY `name`"
    )
    active = _active_branch(ledger)
    entries = [
        {
            "name": str(row["name"]),
            "hash": str(row.get("hash") or ""),
            "active": str(row["name"]) == active,
        }
        for row in rows
    ]
    if json_output(ctx):
        typer.echo(json.dumps(entries, indent=2, sort_keys=True))
        return
    for entry in entries:
        marker = "*" if entry["active"] else " "
        typer.echo(f"{marker} {entry['name']}\t{entry['hash']}")


@branch_app.command("create")
def branch_create(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="New branch name."),
    from_ref: str | None = typer.Option(
        None, "--from", help="Create from this branch/tag/commit (default: HEAD)."
    ),
) -> None:
    """Create a branch (``DOLT_BRANCH``); it is not checked out."""
    ledger = resolve_ledger(ctx)
    options: GlobalOptions = ctx.obj
    target = _check_name(name)
    statement = (
        "CALL DOLT_BRANCH(%s, %s)"
        if from_ref is not None
        else "CALL DOLT_BRANCH(%s)"
    )
    params = (target, from_ref) if from_ref is not None else (target,)
    if options.dry_run:
        typer.echo(render_sql(statement, params))
        return
    ledger.driver.execute(statement, params)
    origin = f" from {from_ref}" if from_ref is not None else ""
    typer.echo(f"created branch {target}{origin}")


@branch_app.command("delete")
def branch_delete(
    ctx: typer.Context,
    name: str = typer.Argument(...,
                              help="Branch to delete (Dolt requires it fully merged)."),
) -> None:
    """Delete a fully merged branch (``DOLT_BRANCH('-d')``)."""
    ledger = resolve_ledger(ctx)
    options: GlobalOptions = ctx.obj
    target = _check_name(name)
    statement = "CALL DOLT_BRANCH('-d', %s)"
    if options.dry_run:
        typer.echo(render_sql(statement, (target,)))
        return
    ledger.driver.execute(statement, (target,))
    typer.echo(f"deleted branch {target}")
