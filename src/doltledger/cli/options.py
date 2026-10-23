"""Values of the global CLI options, resolved once by the root callback."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import typer

from doltledger.core.ledger import Ledger, open as open_ledger


@dataclass
class GlobalOptions:
    """Values of the global options, passed to subcommands via ``ctx.obj``."""

    workspace: Path | None = None
    ledger: str | None = None
    dry_run: bool = False
    no_commit: bool = False


def resolve_ledger(ctx: typer.Context) -> Ledger:
    """Open the ledger selected by the global options."""
    options = ctx.obj
    if not isinstance(options, GlobalOptions):
        raise RuntimeError("the CLI root callback did not set GlobalOptions")
    return open_ledger(
        options.workspace,
        options.ledger,
        dry_run=options.dry_run,
        no_commit=options.no_commit,
    )
