"""Process exit codes and the exception hierarchy used across DoltLedger.

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
    "LedgerError",
    "UsageError",
    "UnreachableError",
]


class LedgerError(Exception):
    """A business or validation failure. Exit code 1."""

    exit_code = EXIT_ERROR


class UsageError(LedgerError):
    """Invalid invocation or configuration. Exit code 2."""

    exit_code = EXIT_USAGE


class UnreachableError(LedgerError):
    """The Dolt database or workspace server cannot be reached. Exit code 3."""

    exit_code = EXIT_UNREACHABLE
