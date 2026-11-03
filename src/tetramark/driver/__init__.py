"""Database drivers: the embedded default, the opt-in server backend, resolution.

The embedded driver (one ``dolt sql`` subprocess per batch) is the only
backend a normal invocation uses.  The ``dolt sql-server`` backend is opt-in:
:func:`driver_for` selects it only when the *effective* environment -- the
``env=`` mapping handed to ``init``/``adopt``/``open`` when there is one, else
``os.environ`` -- sets ``TETRAMARK_DRIVER=server``.  The server backend imports
PyMySQL lazily, so the default install never needs it.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from tetramark.driver.base import Driver
from tetramark.driver.embedded import EmbeddedDriver
from tetramark.driver.resolution import (
    LedgerRef,
    ledger_dir,
    refuse_nested,
    resolve_ledger,
)
from tetramark.errors import UsageError

__all__ = [
    "DRIVER_ENV",
    "EMBEDDED",
    "SERVER",
    "Driver",
    "EmbeddedDriver",
    "LedgerRef",
    "driver_for",
    "driver_mode",
    "ledger_dir",
    "refuse_nested",
    "resolve_ledger",
]

#: Environment variable selecting the backend for a ledger operation.
DRIVER_ENV = "TETRAMARK_DRIVER"
EMBEDDED = "embedded"
SERVER = "server"


def driver_mode(env: Mapping[str, str] | None = None) -> str:
    """The backend the effective environment selects: ``embedded`` or ``server``.

    ``env`` is the complete environment a caller passes to a ledger operation
    (tests/embedding do), not a delta: when it is ``None`` the process
    environment is used.  An unset/empty value means the default embedded
    driver, so nothing changes for existing users.
    """
    effective = os.environ if env is None else env
    raw = effective.get(DRIVER_ENV, "")
    value = str(raw).strip().lower() if raw is not None else ""
    if not value or value == EMBEDDED:
        return EMBEDDED
    if value == SERVER:
        return SERVER
    raise UsageError(
        f"invalid {DRIVER_ENV} value {raw!r}: use 'embedded' (default) or 'server'"
    )


def driver_for(
    path: str | Path,
    *,
    branch: str | None = None,
    validate_branch: bool = True,
    dolt_binary: str | None = None,
    env: Mapping[str, str] | None = None,
) -> Driver:
    """Build the driver the effective environment selects for one ledger."""
    if driver_mode(env) == SERVER:
        # Imported here so a normal embedded install never imports PyMySQL.
        from tetramark.driver.server import ServerDriver

        return ServerDriver(
            path,
            branch=branch,
            validate_branch=validate_branch,
            dolt_binary=dolt_binary,
            env=env,
        )
    return EmbeddedDriver(
        path,
        branch=branch,
        validate_branch=validate_branch,
        dolt_binary=dolt_binary,
        env=env,
    )
