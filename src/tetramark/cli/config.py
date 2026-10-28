"""``tm config get|set author|presets_dir`` (spec §6, §1.1, §8).

The effective author used by commits is resolved in this order: ``ledger.yaml``
→ ``workspace.yaml`` → Git ``user.name``/``user.email``.  ``config get author``
prints that effective value; ``config set author`` stores it in ``ledger.yaml``
and in the ``ledger_meta`` copy so the database stays self-describing.

``presets_dir`` is workspace configuration (``workspace.yaml``): the directory
that ``tm init --preset NAME`` resolves bare names from.  It is resolved
relative to ``workspace.yaml`` and is read/written without opening a ledger.
"""

from __future__ import annotations

import typer

from tetramark.cli.options import GlobalOptions, echo_dry_run_sql, resolve_ledger
from tetramark.cli.workspace import resolve_workspace
from tetramark.errors import UsageError

config_app = typer.Typer(
    help="Read and write the ledger configuration.",
    no_args_is_help=True,
)

#: The settings this command exposes.
_KEYS = ("author", "presets_dir")


def _check_key(key: str) -> str:
    name = str(key).strip().lower()
    if name not in _KEYS:
        raise UsageError(
            f"unknown configuration key {key!r} (supported: {', '.join(_KEYS)})"
        )
    return name


@config_app.command("get")
def config_get(
    ctx: typer.Context,
    key: str = typer.Argument(
        "author", help="Configuration key (author, presets_dir)."
    ),
) -> None:
    """Print an effective configuration value."""
    name = _check_key(key)
    if name == "presets_dir":
        workspace = resolve_workspace(ctx)
        presets_dir = workspace.presets_dir_path
        typer.echo(str(presets_dir) if presets_dir is not None else "")
        return
    ledger = resolve_ledger(ctx)
    typer.echo(ledger.author or "")


@config_app.command("set")
def config_set(
    ctx: typer.Context,
    key: str = typer.Argument(..., help="Configuration key (author, presets_dir)."),
    value: str = typer.Argument(
        ..., help='"Name <email>" for author; a directory for presets_dir.'
    ),
) -> None:
    """Set a configuration value (one Dolt commit when the value changes)."""
    name = _check_key(key)
    if name == "presets_dir":
        workspace = resolve_workspace(ctx)
        raw = value.strip()
        if not raw:
            raise UsageError("presets_dir must not be empty")
        workspace.config.presets_dir = raw
        workspace.save()
        typer.echo(f"presets_dir: {workspace.presets_dir_path}")
        typer.echo(f"  config : {workspace.config_path}")
        return
    ledger = resolve_ledger(ctx)
    options: GlobalOptions = ctx.obj
    if name == "author":
        session = ledger.set_author(value)
        if session.dry_run:
            echo_dry_run_sql(ledger, session.sql)
        else:
            typer.echo(f"author: {ledger.author}")
            if options.no_commit:
                typer.echo("(not committed; --no-commit)")
