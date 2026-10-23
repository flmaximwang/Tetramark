"""Values of the global CLI options, resolved once by the root callback."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class GlobalOptions:
    """Values of the global options, passed to subcommands via ``ctx.obj``."""

    workspace: Path | None = None
    ledger: str | None = None
