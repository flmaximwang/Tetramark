"""Process exit codes and the exception hierarchy used across Tetramark.

The CLI maps ``LedgerError`` (and its subclasses) to a process exit code:

===== ==========================================================
code  meaning
===== ==========================================================
0     ok
1     business / validation error (``LedgerError``)
2     usage error (``UsageError``)
3     database unreachable (``UnreachableError``)
===== ==========================================================
"""

from __future__ import annotations

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_UNREACHABLE = 3

__all__ = [
    "EXIT_OK",
    "EXIT_ERROR",
    "EXIT_USAGE",
    "EXIT_UNREACHABLE",
    "LedgerBusyError",
    "LedgerError",
    "UsageError",
    "UnreachableError",
    "try_find_hint",
]


def try_find_hint(ref: str) -> str:
    """The ``; try tm find <ref>`` suffix of a single-object not-found error.

    Single-object resolution (show, history, lineage, ``--entity`` and
    ``tm alias add``) stays exact: a ``find.rewrites`` rule is never applied to
    it.  The error therefore points at ``tm find``, the one command that does
    apply the ledger's rewrite rules (Tetramark-53r.1).  An empty ref gets no
    hint.
    """
    text = str(ref).strip()
    return f"; try tm find {text}" if text else ""


class LedgerError(Exception):
    """A business or validation failure. Exit code 1."""

    exit_code = EXIT_ERROR


class UsageError(LedgerError):
    """Invalid invocation or configuration. Exit code 2."""

    exit_code = EXIT_USAGE


class LedgerBusyError(UsageError):
    """Another process holds the ledger's write lock (P11). Exit code 2.

    Raised when the per-ledger ``flock`` cannot be taken within
    ``$TETRAMARK_LOCK_TIMEOUT`` seconds (10 s by default); the message names
    the holder recorded in ``<ledger>/.dolt/tetramark.lock``.
    """


class UnreachableError(LedgerError):
    """The Dolt database (or the dolt binary) cannot be reached. Exit code 3."""

    exit_code = EXIT_UNREACHABLE
