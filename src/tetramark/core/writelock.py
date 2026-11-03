"""The per-ledger writer lock (P11): one writer, readers never lock.

Why a lock of our own: the embedded driver runs one ``dolt sql`` subprocess per
statement, so a command's read (allocate an id, resolve an alias, validate a
transition) and its later write are separate Dolt sessions.  Dolt's own
noms/table locks only cover one transaction each and cannot close that
read-then-write race between two ``tm`` processes.  A single exclusive
``fcntl.flock`` on ``<ledger>/.dolt/tetramark.lock`` does.

Covered window
--------------
* **CLI write commands** take the lock after resolving the ledger directory but
  *before* the ledger is opened, so ``ledger.yaml``, the schema and
  ``ledger_meta`` are read inside the window too, along with every read,
  validation, the write session and the Dolt commit of the command.  This is
  the whole read -> validate -> write -> commit window.
* **Python API**: :meth:`tetramark.core.ledger.Ledger.write` /
  :class:`~tetramark.core.ledger.WriteSession` take it for the transaction and
  the commit.  A caller that reads before opening the session should wrap the
  whole step in ``with ledger.hold_write_lock():``; the lock is re-entrant per
  ``Ledger`` object, so the nested write session joins it.
* **Reads never lock** (find/show/list/status/check/history/export,
  ``tm backup list``, ...), and neither does ``--dry-run``: it writes nothing.
* ``tm init``/``tm adopt`` are not covered: they create/format the database
  directory and are exclusive by nature.

Waiting and the busy error
--------------------------
The first attempt is non-blocking; while another process holds the lock we
retry every :data:`RETRY_INTERVAL` seconds until the timeout, 10 s by default.
``TETRAMARK_LOCK_TIMEOUT`` (seconds, float) overrides it.  On timeout the
command raises :class:`tetramark.errors.LedgerBusyError` -- a
:class:`~tetramark.errors.UsageError`, exit code 2 -- with a message such as
``ledger busy: pid 4711 (tm stock add) is writing``.

The lock file
-------------
``<ledger>/.dolt/tetramark.lock`` is created when missing and left in place.
While the lock is held it carries a small JSON record -- ``pid``, ``command``,
``since`` (UTC) and ``host`` -- so a blocked command can name the holder and
``tm status`` can show it without waiting.  :func:`write_lock_holder` is that
read-only probe for ticket .9; it never creates, steals or waits for the lock.
"""

from __future__ import annotations

import errno
import json
import math
import os
import socket
import sys
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType
from typing import Any, Mapping, Sequence

import fcntl

from tetramark.errors import LedgerBusyError, LedgerError, UsageError

__all__ = [
    "DEFAULT_LOCK_TIMEOUT",
    "LOCK_FILENAME",
    "RETRY_INTERVAL",
    "TIMEOUT_ENV",
    "LockHolder",
    "WriteLock",
    "describe_command",
    "lock_path",
    "lock_timeout",
    "read_lock_holder",
    "write_lock_holder",
]

#: Name of the lock file inside the ledger's ``.dolt/`` directory.
LOCK_FILENAME = "tetramark.lock"

#: Seconds to wait for another writer before raising ``LedgerBusyError``.
DEFAULT_LOCK_TIMEOUT = 10.0

#: Environment variable that overrides :data:`DEFAULT_LOCK_TIMEOUT`.
TIMEOUT_ENV = "TETRAMARK_LOCK_TIMEOUT"

#: Seconds between two non-blocking lock attempts while waiting.
RETRY_INTERVAL = 0.05

#: ``flock`` reports contention with one of these (``EWOULDBLOCK == EAGAIN``).
_BUSY_ERRNOS = frozenset({errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK})


# ---------------------------------------------------------------------------
# The lock file
# ---------------------------------------------------------------------------


def lock_path(ledger_path: str | Path) -> Path:
    """``<ledger>/.dolt/tetramark.lock`` for a ledger directory."""
    return Path(ledger_path).expanduser() / ".dolt" / LOCK_FILENAME


@dataclass(frozen=True)
class LockHolder:
    """The writer recorded in a ledger's lock file (all fields best effort)."""

    pid: int | None = None
    command: str | None = None
    since: str | None = None
    host: str | None = None

    @property
    def description(self) -> str:
        """Short human label, e.g. ``pid 4711 (tm stock add)``."""
        parts: list[str] = []
        if self.pid is not None:
            parts.append(f"pid {self.pid}")
        if self.command:
            parts.append(f"({self.command})")
        return " ".join(parts) if parts else "another process"

    def as_dict(self) -> dict[str, Any]:
        """The lock-file shape; ``None`` fields are omitted."""
        return {
            key: value
            for key, value in (
                ("pid", self.pid),
                ("command", self.command),
                ("since", self.since),
                ("host", self.host),
            )
            if value is not None
        }


def _ledger_path(target: str | Path | Any) -> Path:
    """Accept a ledger object (``.path``) or a path."""
    path = getattr(target, "path", target)
    return Path(path).expanduser()


def _parse_holder(text: str) -> LockHolder:
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return LockHolder()
    if not isinstance(data, dict):
        return LockHolder()

    pid = data.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int):
        pid = None
    command = data.get("command")
    command = command if isinstance(command, str) and command.strip() else None
    since = data.get("since")
    since = since if isinstance(since, str) and since.strip() else None
    host = data.get("host")
    host = host if isinstance(host, str) and host.strip() else None
    return LockHolder(pid=pid, command=command, since=since, host=host)


def read_lock_holder(target: str | Path | Any) -> LockHolder | None:
    """The lock file's recorded holder, or ``None`` when there is no lock file.

    This reads the file only: it neither locks nor checks whether the recorded
    writer is still alive (use :func:`write_lock_holder` for that).
    """
    path = lock_path(_ledger_path(target))
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    return _parse_holder(text)


def write_lock_holder(target: str | Path | Any) -> LockHolder | None:
    """Probe a ledger's write lock without waiting: the holder or ``None``.

    A non-blocking ``flock`` attempt decides whether the lock is currently
    held; the lock-file content then names the holder.  Nothing is created,
    stolen or waited for: a missing lock file means ``None`` (no file is
    made), and a free lock is released immediately after the probe.

    Ticket .9 uses this for ``tm status``'s write-lock line; ``target`` may be
    a :class:`~tetramark.core.ledger.Ledger` or a ledger directory.
    """
    path = lock_path(_ledger_path(target))
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno not in _BUSY_ERRNOS:
                return None
            return _parse_holder_file(path)
        with suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        return None
    finally:
        with suppress(OSError):
            os.close(fd)


def _parse_holder_file(path: Path) -> LockHolder:
    try:
        return _parse_holder(path.read_text(encoding="utf-8"))
    except OSError:  # pragma: no cover - unreadable but held lock
        return LockHolder()


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------


def lock_timeout(env: Mapping[str, str] | None = None) -> float:
    """The lock timeout in seconds: ``$TETRAMARK_LOCK_TIMEOUT`` or 10 s.

    An invalid or negative value is a :class:`UsageError`: silently waiting
    longer (or forever) would hide a typo in a script.
    """
    source = os.environ if env is None else env
    raw = source.get(TIMEOUT_ENV)
    if raw is None or not str(raw).strip():
        return DEFAULT_LOCK_TIMEOUT
    try:
        value = float(str(raw).strip())
    except ValueError:
        raise UsageError(
            f"invalid {TIMEOUT_ENV}={raw!r}: expected a number of seconds"
        ) from None
    if not math.isfinite(value) or value < 0:
        raise UsageError(
            f"invalid {TIMEOUT_ENV}={raw!r}: expected a finite, non-negative "
            "number of seconds"
        )
    return value


def _describe_default_command() -> str | None:
    """The process command line, best effort (informational only)."""
    return describe_command()


def describe_command(argv: Sequence[str] | None = None) -> str | None:
    """A short command label for the lock file, from ``argv`` (default ours).

    Keeps the program name plus the first few non-option arguments, e.g.
    ``tm stock add``; truncated so the lock file stays a small single line.
    """
    args = list(sys.argv if argv is None else argv)
    if not args:
        return None
    parts = [Path(args[0]).name]
    for argument in args[1:4]:
        if argument.startswith("-"):
            continue
        parts.append(argument)
    text = " ".join(part for part in parts if part)
    return text[:120] or None


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hostname() -> str | None:
    with suppress(OSError):
        return socket.gethostname() or None
    return None  # pragma: no cover - gethostname rarely fails


# ---------------------------------------------------------------------------
# The lock
# ---------------------------------------------------------------------------


class WriteLock:
    """An exclusive, non-blocking-with-retry ``flock`` on one ledger lock file.

    Use as a context manager (``acquire`` in ``__enter__``, ``release`` in
    ``__exit__``) or call :meth:`acquire`/:meth:`release` directly.  The same
    object is re-entrant only through :meth:`Ledger.hold_write_lock`; a second
    ``WriteLock`` on the same ledger (even in the same process) blocks like any
    other writer.
    """

    def __init__(
        self,
        ledger_path: str | Path,
        *,
        command: str | None = None,
    ) -> None:
        #: Ledger directory this lock belongs to.
        self.ledger_path = Path(ledger_path).expanduser()
        #: ``<ledger>/.dolt/tetramark.lock``.
        self.path = lock_path(self.ledger_path)
        #: Short command description written to the lock file.
        self.command = command
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        """True while this object owns the lock."""
        return self._fd is not None

    def acquire(self, *, timeout: float | None = None) -> LockHolder:
        """Take the lock, waiting up to ``timeout`` seconds (default rule).

        Raises :class:`~tetramark.errors.LedgerBusyError` when the wait times
        out.  The lock file is created if missing; its content is rewritten
        with this process's pid and command.  On any error no fd is leaked and
        the lock is not held.
        """
        if self._fd is not None:
            raise LedgerError("the ledger write lock is already held")
        wait = lock_timeout() if timeout is None else _check_timeout(timeout)

        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(
                self.path,
                os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0),
                0o644,
            )
        except OSError as error:
            raise LedgerError(
                f"cannot open the ledger write lock {self.path}: {error}"
            ) from error
        try:
            deadline = time.monotonic() + wait
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as error:
                    if error.errno not in _BUSY_ERRNOS:
                        raise LedgerError(
                            f"cannot take the ledger write lock {self.path}: {error}"
                        ) from error
                    if time.monotonic() >= deadline:
                        holder = _parse_holder_file(self.path)
                        raise LedgerBusyError(_busy_message(holder, wait)) from None
                    time.sleep(RETRY_INTERVAL)
                else:
                    break
            holder = LockHolder(
                pid=os.getpid(),
                command=self.command or _describe_default_command(),
                since=_utc_now(),
                host=_hostname(),
            )
            try:
                _write_holder(fd, holder)
            except OSError as error:
                raise LedgerError(
                    f"cannot record the writer in {self.path}: {error}"
                ) from error
        except BaseException:
            with suppress(OSError):
                os.close(fd)
            raise
        self._fd = fd
        return holder

    def release(self) -> None:
        """Release the lock (idempotent); the file itself stays in place."""
        fd = self._fd
        if fd is None:
            return
        self._fd = None
        with suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        with suppress(OSError):
            os.close(fd)

    def __enter__(self) -> LockHolder:
        return self.acquire()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        self.release()
        return False


def _check_timeout(value: float) -> float:
    if not math.isfinite(value) or value < 0:
        raise UsageError(
            f"invalid write-lock timeout {value!r}: expected a finite, "
            "non-negative number of seconds"
        )
    return float(value)


def _busy_message(holder: LockHolder, waited: float) -> str:
    return (
        f"ledger busy: {holder.description} is writing "
        f"(waited {waited:.1f}s; set {TIMEOUT_ENV}=<seconds> to wait longer)"
    )


def _write_holder(fd: int, holder: LockHolder) -> None:
    """Rewrite the lock file in place (the fd keeps the flock)."""
    payload = json.dumps(holder.as_dict(), sort_keys=True) + "\n"
    data = payload.encode("utf-8")
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]
