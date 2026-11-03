"""``tm config get|set author`` (spec §6, §1.1, §8).

The effective author used by commits is resolved in this order: ``ledger.yaml``
→ Git ``user.name``/``user.email`` (P6; the workspace tier is gone in 0.5.0).
``config get author`` prints that effective value; ``config set author`` stores
it in ``ledger.yaml`` and in the ``ledger_meta`` copy so the database stays
self-describing.

Workspace-only settings were removed with the workspace layer (P7); presets are
resolved through ``$TETRAMARK_PRESETS_DIR`` or the bundled ``presets/``
directory (P8).
"""

from __future__ import annotations

import typer

from tetramark.cli.options import GlobalOptions, echo_dry_run_sql, group, resolve_ledger
from tetramark.errors import UsageError

config_app = group("Read and write the ledger configuration.")

#: The settings this command exposes.
_KEYS = ("author",)


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
    key: str = typer.Argument("author", help="Configuration key (author)."),
) -> None:
    """Print an effective configuration value."""
    _check_key(key)
    ledger = resolve_ledger(ctx)
    typer.echo(ledger.author or "")


@config_app.command("set")
def config_set(
    ctx: typer.Context,
    key: str = typer.Argument(..., help="Configuration key (author)."),
    value: str = typer.Argument(..., help='"Name <email>" for author.'),
) -> None:
    """Set a configuration value (one Dolt commit when the value changes)."""
    name = _check_key(key)
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
