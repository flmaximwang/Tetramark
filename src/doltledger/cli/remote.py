"""``ledger remote ...`` and ``ledger backup`` commands (spec §6).

``backup`` is deliberately a single command rather than a group: the spec uses
``ledger backup [DIR]`` and ``ledger backup ls``.  Click would treat a group's
positional argument as the subcommand, so the first argument is interpreted
here (``ls`` lists instead of syncing; a real directory called ``ls`` can
always be passed as ``./ls``).
"""

from __future__ import annotations

import typer

from doltledger.cli.options import GlobalOptions, resolve_ledger
from doltledger.errors import UsageError
from doltledger.ops import remote as remotes

remote_app = typer.Typer(
    help="Configure remotes and push/pull the ledger's Dolt history.",
    no_args_is_help=True,
)


def _echo_result(result: remotes.Result) -> None:
    if result.dry_run:
        typer.echo(result.sql or result.message)
    elif result.message:
        typer.echo(result.message)


@remote_app.command("add")
def remote_add(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Remote name (e.g. origin)."),
    url: str = typer.Argument(..., help="Remote URL or local path."),
) -> None:
    """Add a remote (``DOLT_REMOTE``)."""
    ledger = resolve_ledger(ctx)
    options: GlobalOptions = ctx.obj
    _echo_result(remotes.add_remote(ledger, name, url, dry_run=options.dry_run))


@remote_app.command("ls")
def remote_ls(ctx: typer.Context) -> None:
    """List configured remotes."""
    ledger = resolve_ledger(ctx)
    entries = remotes.list_remotes(ledger)
    if not entries:
        typer.echo("no remotes configured")
        return
    for entry in entries:
        typer.echo(f"{entry.name}\t{entry.url}")


@remote_app.command("rm")
def remote_rm(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Remote to remove."),
) -> None:
    """Remove a configured remote."""
    ledger = resolve_ledger(ctx)
    options: GlobalOptions = ctx.obj
    _echo_result(remotes.remove_remote(ledger, name, dry_run=options.dry_run))


@remote_app.command("push")
def remote_push(
    ctx: typer.Context,
    name: str | None = typer.Argument(None, help="Remote name (default: the only/origin remote)."),
    branch: str | None = typer.Option(None, "--branch", "-b", help="Branch to push."),
) -> None:
    """Push the current branch to a remote."""
    ledger = resolve_ledger(ctx)
    options: GlobalOptions = ctx.obj
    _echo_result(
        remotes.push(ledger, name, branch=branch, dry_run=options.dry_run)
    )


@remote_app.command("pull")
def remote_pull(
    ctx: typer.Context,
    name: str | None = typer.Argument(None, help="Remote name (default: the only/origin remote)."),
    branch: str | None = typer.Option(None, "--branch", "-b", help="Branch to pull."),
) -> None:
    """Pull the current branch from a remote."""
    ledger = resolve_ledger(ctx)
    options: GlobalOptions = ctx.obj
    _echo_result(
        remotes.pull(ledger, name, branch=branch, dry_run=options.dry_run)
    )


def backup_command(
    ctx: typer.Context,
    path: str | None = typer.Argument(
        None,
        help="Backup directory, or 'ls' to list configured backups.",
    ),
    list_only: bool = typer.Option(
        False, "--ls", help="List configured backups instead of syncing."
    ),
) -> None:
    """Back up the ledger to a Dolt backup directory.

    The default is ``<data_dir>/backups/<ledger>``; use ``ledger backup ls`` to
    see the backups already configured for this ledger.
    """
    options: GlobalOptions = ctx.obj
    if list_only or (path is not None and path.strip().lower() == "ls"):
        ledger = resolve_ledger(ctx)
        entries = remotes.list_backups(ledger)
        if not entries:
            typer.echo("no backups configured")
            return
        for entry in entries:
            typer.echo(f"{entry.name}\t{entry.url}")
        return

    if path is not None and not path.strip():
        raise UsageError("a backup directory must not be empty")
    ledger = resolve_ledger(ctx)
    directory: str | None = path
    _echo_result(remotes.backup(ledger, directory, dry_run=options.dry_run))
