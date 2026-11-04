"""Tetramark — a version-controlled inventory ledger on Dolt.

A ledger is one self-contained directory (``.dolt/`` plus ``ledger.yaml``) that
lives next to its docs or repository.  :func:`open` takes its path or discovers
it upward from the current directory; a 0.5.0 ledger has no workspace layer.

The package is deliberately import-light: ``import tetramark`` must not import
the ledger machinery or its third-party dependencies (pydantic, typer, yaml).
``Ledger``, ``open``, ``init_ledger`` and ``adopt_ledger`` are resolved on
first access through PEP 562, so the console entry point can sanitize
``sys.path`` before any dependency is imported.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

__version__ = "0.8.17"

__all__ = ["Ledger", "__version__", "adopt_ledger", "init_ledger", "open"]

#: Names resolved from :mod:`tetramark.core.ledger` on first access.
_LAZY_EXPORTS = frozenset({"Ledger", "open", "init_ledger", "adopt_ledger"})

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tetramark.core.ledger import (
        Ledger,
        adopt_ledger,
        init_ledger,
        open,
    )


def __getattr__(name: str) -> Any:
    """Resolve the ledger API lazily, keeping ``import tetramark`` dependency-free."""
    if name not in _LAZY_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from tetramark.core import ledger

    value = getattr(ledger, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """List the public API without importing it, so ``dir(tetramark)`` stays cheap."""
    return sorted(set(globals()) | _LAZY_EXPORTS)
