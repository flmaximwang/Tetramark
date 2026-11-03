"""``tm workspace`` commands."""

from __future__ import annotations

from pathlib import Path

import typer

from tetramark.cli.options import GlobalOptions, group
from tetramark.driver.workspace import DEFAULT_HOST, DEFAULT_PORT, Workspace
from tetramark.errors import UsageError

workspace_app = group(
    "Create and inspect a workspace (a data directory holding ledgers)."
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
