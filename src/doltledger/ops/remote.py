"""Remotes, Dolt backups and author configuration (spec §6, §1.1).

Dolt keeps remotes and backups in its own configuration and operates them with
stored procedures, so everything here is one ``CALL`` through the driver:

* ``DOLT_REMOTE('add' | 'remove', name[, url])``
* ``DOLT_PUSH(remote, branch)`` / ``DOLT_PULL(remote, branch)``
* ``DOLT_BACKUP('add' | 'remove' | 'sync', name[, url])``

Local paths are accepted where a URL is expected and turned into absolute
``file://`` URLs, which is what Dolt wants.  Making a backup does not change
the ledger's data and therefore never creates a Dolt commit; ``push`` and
``pull`` move existing commits.

Every public function takes an opened :class:`~doltledger.core.ledger.Ledger`
and returns a :class:`Result` describing the statements it ran (or would run
with ``dry_run=True``).  The CLI turns that into human output; tests can assert
on the statements.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from doltledger.driver.base import literal
from doltledger.errors import LedgerError, UsageError

if TYPE_CHECKING:  # pragma: no cover - imported for type checkers only
    from doltledger.core.ledger import Ledger

__all__ = [
    "Backup",
    "Remote",
    "Result",
    "add_remote",
    "as_url",
    "backup",
    "current_branch",
    "default_backup_dir",
    "list_backups",
    "list_remotes",
    "pull",
    "push",
    "remove_remote",
    "resolve_remote",
]

#: A URL scheme Dolt understands (``file://``, ``https://``, ``aws://``, ...).
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Remote:
    """One row of ``dolt_remotes``."""

    name: str
    url: str


@dataclass(frozen=True)
class Backup:
    """One row of ``dolt_backups``."""

    name: str
    url: str


@dataclass(frozen=True)
class Result:
    """Outcome of one remote/backup command.

    ``statements`` holds the SQL that was executed, or that would be executed
    for a dry run.  ``message`` is Dolt's own message for push/pull, or a short
    description for the commands that do not return one.
    """

    statements: tuple[str, ...] = ()
    message: str = ""
    dry_run: bool = False

    @property
    def sql(self) -> str:
        """The collected statements as one semicolon-terminated script."""
        if not self.statements:
            return ""
        return ";\n".join(self.statements) + ";"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def as_url(value: str | Path) -> str:
    """Return a Dolt URL, converting a local path to an absolute ``file://`` URL."""
    text = str(value).strip()
    if not text:
        raise UsageError("a remote or backup URL/path must not be empty")
    if _SCHEME_RE.match(text) or text.startswith("file:"):
        return text
    return Path(text).expanduser().resolve().as_uri()


def _check_name(name: str, what: str = "remote") -> str:
    text = str(name).strip()
    if not text:
        raise UsageError(f"a {what} name must not be empty")
    return text


def _call(procedure: str, *args: Any) -> str:
    """Render ``CALL <procedure>(<literals>)``."""
    rendered = ", ".join(literal(arg) for arg in args)
    return f"CALL {procedure}({rendered})"


def _run(ledger: Ledger, statements: list[str], *, dry_run: bool) -> Result:
    if not dry_run:
        for statement in statements:
            ledger.driver.execute(statement)
    return Result(statements=tuple(statements), dry_run=dry_run)


def _call_rows(ledger: Ledger, statement: str) -> list[dict[str, Any]]:
    """Run a stored procedure that returns a result set (push/pull/backup)."""
    return ledger.driver.query(statement)


# ---------------------------------------------------------------------------
# Remotes
# ---------------------------------------------------------------------------


def list_remotes(ledger: Ledger) -> list[Remote]:
    """Remotes configured in the ledger, ordered by name."""
    rows = ledger.driver.query(
        "SELECT `name`, `url` FROM `dolt_remotes` ORDER BY `name`"
    )
    return [Remote(name=str(row["name"]), url=str(row["url"])) for row in rows]


def add_remote(
    ledger: Ledger, name: str, url: str | Path, *, dry_run: bool = False
) -> Result:
    """Add a remote (``DOLT_REMOTE('add', ...)``).

    Adding the same name with the same URL is a no-op, so scripts can be
    idempotent.  A different URL for an existing name is an error: removing a
    remote is an explicit operation (see :func:`remove_remote`).
    """
    remote_name = _check_name(name)
    remote_url = as_url(url)
    existing = {remote.name: remote for remote in list_remotes(ledger)}
    current = existing.get(remote_name)
    if current is not None:
        if current.url == remote_url:
            return Result(
                message=f"remote {remote_name!r} already points to {remote_url}",
                dry_run=dry_run,
            )
        raise LedgerError(
            f"remote {remote_name!r} already points to {current.url}; "
            f"remove it first to point it at {remote_url}"
        )

    statement = _call("DOLT_REMOTE", "add", remote_name, remote_url)
    result = _run(ledger, [statement], dry_run=dry_run)
    return Result(
        statements=result.statements,
        message=f"added remote {remote_name!r} -> {remote_url}",
        dry_run=dry_run,
    )


def remove_remote(ledger: Ledger, name: str, *, dry_run: bool = False) -> Result:
    """Remove one configured remote."""
    remote_name = _check_name(name)
    if not any(remote.name == remote_name for remote in list_remotes(ledger)):
        raise LedgerError(f"no remote named {remote_name!r} is configured")
    statement = _call("DOLT_REMOTE", "remove", remote_name)
    result = _run(ledger, [statement], dry_run=dry_run)
    return Result(
        statements=result.statements,
        message=f"removed remote {remote_name!r}",
        dry_run=dry_run,
    )


def resolve_remote(ledger: Ledger, name: str | None = None) -> Remote:
    """Pick the remote to push/pull from.

    An explicit ``name`` must exist.  Without one, a single configured remote
    is used; with several, ``origin`` wins; otherwise the caller must choose.
    """
    remotes = list_remotes(ledger)
    if name:
        wanted = str(name).strip()
        for remote in remotes:
            if remote.name == wanted:
                return remote
        raise LedgerError(
            f"no remote named {wanted!r} is configured"
            + (f" (have: {', '.join(r.name for r in remotes)})" if remotes else "")
        )
    if not remotes:
        raise UsageError(
            "no remote configured: add one with 'ledger remote add NAME URL'"
        )
    if len(remotes) == 1:
        return remotes[0]
    for remote in remotes:
        if remote.name == "origin":
            return remote
    raise UsageError(
        "several remotes are configured; choose one: "
        + ", ".join(remote.name for remote in remotes)
    )


def current_branch(ledger: Ledger) -> str:
    """The branch checked out in the ledger."""
    rows = ledger.driver.query("SELECT active_branch() AS `branch`")
    if not rows or not rows[0].get("branch"):
        raise LedgerError("cannot determine the active Dolt branch")
    return str(rows[0]["branch"])


def _sync(
    ledger: Ledger,
    remote: Remote,
    branch: str,
    *,
    operation: str,
    dry_run: bool,
) -> Result:
    statement = _call(f"DOLT_{operation.upper()}", remote.name, branch)
    if dry_run:
        return Result(statements=(statement,), dry_run=True)

    rows = _call_rows(ledger, statement)
    row = rows[-1] if rows else {}
    if operation == "pull" and int(row.get("conflicts") or 0) > 0:
        raise LedgerError(
            f"pull from {remote.name!r}/{branch} left "
            f"{row['conflicts']} conflict(s); resolve them before committing"
        )
    status = row.get("status")
    if status not in (None, 0, "0"):
        raise LedgerError(
            f"{operation} {remote.name!r}/{branch} failed: "
            f"{row.get('message') or status}"
        )
    message = str(row.get("message") or f"{operation} {remote.name!r}/{branch} complete")
    return Result(statements=(statement,), message=message, dry_run=dry_run)


def push(
    ledger: Ledger,
    name: str | None = None,
    *,
    branch: str | None = None,
    dry_run: bool = False,
) -> Result:
    """Push a branch to a remote (``DOLT_PUSH(remote, branch)``)."""
    remote = resolve_remote(ledger, name)
    target = str(branch).strip() if branch else current_branch(ledger)
    if not target:
        raise UsageError("a branch name must not be empty")
    return _sync(ledger, remote, target, operation="push", dry_run=dry_run)


def pull(
    ledger: Ledger,
    name: str | None = None,
    *,
    branch: str | None = None,
    dry_run: bool = False,
) -> Result:
    """Pull a branch from a remote (``DOLT_PULL(remote, branch)``)."""
    remote = resolve_remote(ledger, name)
    target = str(branch).strip() if branch else current_branch(ledger)
    if not target:
        raise UsageError("a branch name must not be empty")
    return _sync(ledger, remote, target, operation="pull", dry_run=dry_run)


# ---------------------------------------------------------------------------
# Backups
# ---------------------------------------------------------------------------


def default_backup_dir(ledger: Ledger) -> Path:
    """``<data_dir>/backups/<ledger>`` (spec: ``ledger backup`` default)."""
    return Path(ledger.workspace.data_dir) / "backups" / ledger.name


def list_backups(ledger: Ledger) -> list[Backup]:
    """Backups configured in the ledger, ordered by name."""
    rows = ledger.driver.query(
        "SELECT `name`, `url` FROM `dolt_backups` ORDER BY `name`"
    )
    return [Backup(name=str(row["name"]), url=str(row["url"])) for row in rows]


def backup(
    ledger: Ledger,
    directory: str | Path | None = None,
    *,
    name: str | None = None,
    dry_run: bool = False,
) -> Result:
    """Sync the ledger to a ``file://`` backup directory.

    A ledger has one backup destination per backup name (default: the ledger
    name), so running the command again with another directory replaces that
    destination before syncing.  The backup itself is a Dolt backup, not a
    filesystem copy: ``dolt backup restore`` can recreate the database.
    """
    url = as_url(default_backup_dir(ledger) if directory is None else directory)
    backup_name = _check_name(name or ledger.name, what="backup")
    existing = {item.name: item for item in list_backups(ledger)}

    statements: list[str] = []
    current = existing.get(backup_name)
    if current is None:
        statements.append(_call("DOLT_BACKUP", "add", backup_name, url))
    elif current.url != url:
        statements.append(_call("DOLT_BACKUP", "remove", backup_name))
        statements.append(_call("DOLT_BACKUP", "add", backup_name, url))
    statements.append(_call("DOLT_BACKUP", "sync", backup_name))

    result = _run(ledger, statements, dry_run=dry_run)
    return Result(
        statements=result.statements,
        message=f"backed up {ledger.name!r} to {url}",
        dry_run=dry_run,
    )
