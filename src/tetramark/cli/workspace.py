"""``tm workspace`` and ``tm server`` commands."""

from __future__ import annotations

from pathlib import Path

import typer

from tetramark.cli.options import GlobalOptions
from tetramark.driver.workspace import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    ServerStatus,
    Workspace,
)
from tetramark.errors import UsageError

workspace_app = typer.Typer(
    help="Create and inspect a workspace (a data directory holding ledgers).",
    no_args_is_help=True,
)
server_app = typer.Typer(
    help="Start, stop and inspect the workspace's Dolt SQL server.",
    no_args_is_help=True,
)


def resolve_workspace(ctx: typer.Context) -> Workspace:
    """The workspace selected by ``--workspace`` or discovered from the cwd."""
    options: GlobalOptions = ctx.obj
    if options.workspace is not None:
        return Workspace.load(options.workspace)
    found = Workspace.discover()
    if found is None:
        raise UsageError(
            "no workspace found: pass --workspace PATH or run inside a directory "
            "containing workspace.yaml"
        )
    return found


@workspace_app.command("init")
def workspace_init(
    ctx: typer.Context,
    path: Path = typer.Argument(
        Path("."), help="Data directory to initialise (created if missing)."
    ),
    port: int = typer.Option(DEFAULT_PORT, "--port", help="Port for the Dolt SQL server."),
    host: str = typer.Option(DEFAULT_HOST, "--host", help="Host for the Dolt SQL server."),
    default_ledger: str | None = typer.Option(
        None, "--default-ledger", help="Ledger used when --ledger is omitted."
    ),
    author: str | None = typer.Option(None, "--author", help="Commit author for this workspace."),
    presets_dir: str | None = typer.Option(
        None,
        "--presets-dir",
        help=(
            "Directory holding <name>.yaml presets for 'tm init --preset NAME' "
            "(relative paths resolve against workspace.yaml), e.g. the "
            "Tetramark checkout's presets/."
        ),
    ),
) -> None:
    """Create ``workspace.yaml`` in PATH."""
    workspace = Workspace.create(
        path,
        host=host,
        port=port,
        default_ledger=default_ledger,
        author=author,
        presets_dir=presets_dir,
    )
    typer.echo(f"Created workspace {workspace.data_dir}")
    typer.echo(f"  config : {workspace.config_path}")
    typer.echo(f"  server : {workspace.config.host}:{workspace.config.port}")
    if workspace.presets_dir_path is not None:
        typer.echo(f"  presets: {workspace.presets_dir_path}")


@server_app.command("start")
def server_start(
    ctx: typer.Context,
    daemon: bool = typer.Option(
        False, "--daemon", "-d", help="Run in the background (log and pid in the data_dir)."
    ),
) -> None:
    """Start the workspace's ``dolt sql-server``."""
    workspace = resolve_workspace(ctx)
    if daemon:
        pid = workspace.start(daemon=True)
        typer.echo(
            f"Started Dolt server pid {pid} on "
            f"{workspace.config.host}:{workspace.config.port}"
        )
        _print_status(workspace.status())
    else:
        typer.echo(
            f"Starting Dolt server on {workspace.config.host}:{workspace.config.port} "
            "(Ctrl-C to stop) ..."
        )
        workspace.start(daemon=False)


@server_app.command("stop")
def server_stop(ctx: typer.Context) -> None:
    """Stop the workspace's Dolt SQL server."""
    workspace = resolve_workspace(ctx)
    if workspace.stop():
        typer.echo(f"Stopped the Dolt server (data_dir: {workspace.data_dir})")
    else:
        typer.echo(f"No Dolt server running (data_dir: {workspace.data_dir})")


@server_app.command("status")
def server_status(ctx: typer.Context) -> None:
    """Show whether the server runs, its endpoint, and the ledgers it serves."""
    workspace = resolve_workspace(ctx)
    _print_status(workspace.status())


def _print_status(status: ServerStatus) -> None:
    if status.running:
        typer.echo(f"running: pid {status.pid}")
        typer.echo(f"  data_dir : {status.data_dir}")
        typer.echo(f"  ledgers  : {' '.join(status.ledgers) or '(none)'}")
        typer.echo(f"  endpoint : {status.endpoint(status.default_ledger)}")
        typer.echo(f"  log      : {status.log_file}")
    else:
        typer.echo(f"not running (data_dir: {status.data_dir})")
        typer.echo(f"  ledgers  : {' '.join(status.ledgers) or '(none)'}")
        if status.port_busy:
            typer.echo(
                f"  note     : port {status.port} is used by another process"
            )
