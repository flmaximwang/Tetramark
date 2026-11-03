"""``tm remote ...`` and ``tm backup`` commands (spec §6).

``backup`` is deliberately a single command rather than a group: the spec uses
``tm backup [DIR]`` and ``tm backup list``.  Click would treat a group's
positional argument as the subcommand, so the first argument is interpreted
here (``list`` lists instead of syncing; a real directory called ``list`` can
always be passed as ``./list``).

``DIR`` is optional only when the ledger already has a configured backup: a
standalone ledger gets no default destination next to it, because that backup
would sit on the same disk and be lost with the ledger (epic P10).
"""

from __future__ import annotations

import typer

from tetramark.cli.options import (
    GlobalOptions,
    group,
    resolve_ledger,
    write_command,
)
from tetramark.errors import UsageError
from tetramark.ops import remote as remotes

remote_app = group("Configure remotes and push/pull the ledger's Dolt history.")


def _echo_result(result: remotes.Result) -> None:
    if result.dry_run:
        typer.echo(result.sql or result.message)
    elif result.message:
        typer.echo(result.message)


@remote_app.command("add")
@write_command
def remote_add(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Remote name (e.g. origin)."),
    url: str = typer.Argument(..., help="Remote URL or local path."),
) -> None:
    """Add a remote (``DOLT_REMOTE``)."""
    ledger = resolve_ledger(ctx)
    options: GlobalOptions = ctx.obj
    _echo_result(remotes.add_remote(ledger, name, url, dry_run=options.dry_run))


@remote_app.command("list")
def remote_list(ctx: typer.Context) -> None:
    """List configured remotes."""
    ledger = resolve_ledger(ctx)
    entries = remotes.list_remotes(ledger)
    if not entries:
        typer.echo("no remotes configured")
        return
    for entry in entries:
        typer.echo(f"{entry.name}\t{entry.url}")


@remote_app.command("push")
@write_command
def remote_push(
    ctx: typer.Context,
    name: str | None = typer.Argument(None, help="Remote name (default: the only/origin remote)."),
    branch: str | None = typer.Option(
        None,
        "--branch",
        "-b",
        help=(
            "Remote branch name to push (default: the session branch — the "
            "global -b, else the checked-out branch). Unlike the global "
            "'tm -b', this names a branch on the remote."
        ),
    ),
) -> None:
    """Push the current branch to a remote."""
    ledger = resolve_ledger(ctx)
    options: GlobalOptions = ctx.obj
    _echo_result(
        remotes.push(ledger, name, branch=branch, dry_run=options.dry_run)
    )


@remote_app.command("pull")
@write_command
def remote_pull(
    ctx: typer.Context,
    name: str | None = typer.Argument(None, help="Remote name (default: the only/origin remote)."),
    branch: str | None = typer.Option(
        None,
        "--branch",
        "-b",
        help=(
            "Remote branch name to pull (default: the session branch — the "
            "global -b, else the checked-out branch). Unlike the global "
            "'tm -b', this names a branch on the remote."
        ),
    ),
) -> None:
    """Pull the current branch from a remote."""
    ledger = resolve_ledger(ctx)
    options: GlobalOptions = ctx.obj
    _echo_result(
        remotes.pull(ledger, name, branch=branch, dry_run=options.dry_run)
    )


@remote_app.command("remove")
@write_command
def remote_remove(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Remote to remove."),
) -> None:
    """Remove a configured remote."""
    ledger = resolve_ledger(ctx)
    options: GlobalOptions = ctx.obj
    _echo_result(remotes.remove_remote(ledger, name, dry_run=options.dry_run))


def backup_command(
    ctx: typer.Context,
    path: str | None = typer.Argument(
        None,
        help="Backup directory, or 'list' to list configured backups.",
    ),
    list_only: bool = typer.Option(
        False, "--list", help="List configured backups instead of syncing."
    ),
    name: str | None = typer.Option(
        None,
        "--name",
        help=(
            "Backup name (default: the ledger name). Without DIR, selects the "
            "configured backup to sync."
        ),
    ),
) -> None:
    """Back up the ledger to a Dolt backup directory.

    DIR is required unless this ledger already has a configured backup: then
    that backup is synced.  There is no default directory, because a backup
    next to the ledger shares its disk and is lost with it.  Use
    ``tm backup list`` to see the backups already configured for this ledger.
    """
    options: GlobalOptions = ctx.obj
    if list_only or (path is not None and path.strip().lower() == "list"):
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
    # ``tm backup`` reads the ledger and writes a copy: one writer per ledger
    # (P11), but only for the sync -- ``tm backup list`` above is a read and the
    # dry run writes nothing.
    if options.dry_run:
        _echo_result(remotes.backup(ledger, directory, name=name, dry_run=True))
    else:
        with ledger.hold_write_lock(command="tm backup"):
            _echo_result(
                remotes.backup(ledger, directory, name=name, dry_run=False)
            )
