"""Opt-in server driver: one ``dolt sql-server`` per ledger, MySQL protocol.

The default backend is :class:`~tetramark.driver.embedded.EmbeddedDriver`; this
driver is selected only when the effective environment sets
``TETRAMARK_DRIVER=server`` (see :func:`tetramark.driver.driver_for`).  It speaks
the same :class:`~tetramark.driver.base.Driver` contract, but instead of paying
a ``dolt sql`` process per statement it starts one real ``dolt sql-server`` in
the ledger directory and runs every statement over PyMySQL.

Why it exists: on the reference machine a ``dolt sql`` process costs 60-100 ms
(0.2-0.4 s under load), while a server statement costs about 0.3 ms.  The full
real-Dolt test suite executes ~13,000 statements, so the server path is the only
way it fits in a five-minute lane.  The suite is still *real* Dolt: the server
runs the same binary against the same database files, and nothing here mocks
the engine.

Lifecycle
---------
* The server starts **lazily** on the first operation, on a free loopback port,
  and is polled until PyMySQL can connect (a captured stderr tail is reported
  when it does not become ready).
* One :class:`ServerDriver` owns one PyMySQL connection, so concurrent drivers
  on the same ledger -- e.g. a branch session and the checked-out session --
  do not share session state (``USE db/branch`` is per connection).
* ``driver.close()`` closes the connection but keeps the server process alive
  in a small module-level LRU keyed by the ledger's resolved path.  CLI
  subprocesses and doc tests open/close the same ledger many times; reusing the
  server is what makes those cheap.  The cache is capped
  (``$TETRAMARK_SERVER_CACHE``, default 6; ``0`` disables reuse and kills the
  server on close), evicts the least recently used idle server, and every owned
  server is killed at interpreter exit and from the pytest session-finish hook.
* A *foreign* server (one this process did not start, typically a parent test
  process's server) is discovered through Dolt's own
  ``.dolt/sql-server.info`` and borrowed read/write.  A borrowed server is never
  killed by this process.  This keeps subprocess CLI/migration runs working on
  a ledger whose server is owned by the parent process; a stale info file (a
  copied template ledger, a crashed server) fails the identity check and is
  ignored.

Semantics
---------
* Values are normalised to the shapes the embedded driver's ``-r json`` output
  produces: ``DECIMAL`` -> ``str``, ``DATE``/``DATETIME`` -> the same text,
  ``BLOB`` -> the same lossy UTF-8 text, ``NULL`` -> ``None`` with the column
  key present (PyMySQL returns all columns natively, so no second CSV run).
* Writes run with ``autocommit=1`` outside a transaction: one ``execute`` is
  committed exactly like one embedded ``dolt sql`` run.  :meth:`transaction`
  sends a real ``START TRANSACTION``; ``DDL`` keeps Dolt's implicit-commit
  behaviour (the driver re-enters the transaction afterwards so the DML that
  follows still rolls back with the session), and :meth:`commit` /
  :meth:`commit_pending` refuse to run inside one, like the embedded driver.
  A command still makes exactly one Dolt commit with the same message/author.
* Errors are mapped to :class:`~tetramark.errors.LedgerError` (or
  :class:`~tetramark.errors.UnreachableError` when the server cannot start or
  the connection drops) carrying Dolt's own message text.
"""

from __future__ import annotations

import atexit
import datetime
import hashlib
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from collections import OrderedDict
from collections.abc import Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any, BinaryIO

try:  # optional: only the opt-in server lane needs the MySQL client
    import pymysql
    from pymysql.cursors import DictCursor
except ImportError:  # pragma: no cover - exercised only without the extra
    pymysql = None  # type: ignore[assignment]
    DictCursor = None  # type: ignore[assignment]

from tetramark.driver.base import (
    Driver,
    Params,
    check_branch_name,
    commit_statements,
    is_ddl,
    is_nothing_to_commit,
    render_statement,
)
from tetramark.errors import LedgerError, UnreachableError, UsageError

__all__ = [
    "ServerDriver",
    "cached_server_count",
    "discard_server",
    "server_start_count",
    "shutdown_all_servers",
]

#: How long a freshly started server may take to accept a connection.  The
#: measured startup is ~0.3 s; the timeout only exists so a broken binary
#: fails the test instead of hanging it.
_START_TIMEOUT_SECONDS = 30.0

#: Seconds between readiness probes (and after a failed connect).
_PROBE_INTERVAL_SECONDS = 0.02

#: Cache cap when ``$TETRAMARK_SERVER_CACHE`` is unset: six ledgers is enough
#: for the template copies a test worker cycles through and keeps the process
#: count small.
_DEFAULT_CACHE_SIZE = 6

#: ``.dolt/sql-server.info`` is Dolt's own discovery file: ``port:pid:uuid``.
_INFO_FILE = "sql-server.info"

#: Tetramark's own sidecar, written next to Dolt's file.  Unlike Dolt's file it
#: records the *resolved ledger path* and the directory's ``(st_dev, st_ino)``,
#: so a copied or replaced directory can never borrow a server that belongs to
#: another path (copying a template must not leak its server into the copy).
_SIDECAR_FILE = "tetramark-server.json"


class _Server:
    """One ``dolt sql-server`` process (or a borrowed server we do not own)."""

    def __init__(
        self,
        path: Path,
        port: int,
        *,
        process: subprocess.Popen[bytes] | None,
        stderr: BinaryIO | None,
        fingerprint: str | None,
        cache_cap: int,
        identity: tuple[int, int] | None = None,
    ) -> None:
        self.path = path
        self.port = port
        #: ``(st_dev, st_ino)`` of the ledger directory the server was started
        #: for; a different inode means the path now holds another database.
        self.identity = identity
        #: ``None`` for a borrowed server: another process owns its lifecycle.
        self.process = process
        self._stderr = stderr
        self.fingerprint = fingerprint
        self.cache_cap = cache_cap
        #: Open connections of this process; the cache only evicts at zero.
        self.refs = 0

    @property
    def owned(self) -> bool:
        return self.process is not None

    def alive(self) -> bool:
        return self.process is None or self.process.poll() is None

    def stop(self) -> None:
        """Terminate an owned server (borrowed ones are left alone)."""
        process = self.process
        self.process = None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover - stuck server
                process.kill()
                process.wait(timeout=5)
        if process is not None:
            _remove_sidecar(self.path)
            _remove_dolt_info(self.path, process.pid)
        self._close_stderr()

    def _close_stderr(self) -> None:
        stderr = self._stderr
        self._stderr = None
        if stderr is not None:
            try:
                stderr.close()
            except OSError:  # pragma: no cover - defensive
                pass

    def stderr_tail(self, limit: int = 2000) -> str:
        stderr = self._stderr
        if stderr is None:
            return ""
        try:
            stderr.flush()
            stderr.seek(0)
            data = stderr.read()
        except OSError:  # pragma: no cover - defensive
            return ""
        text = data.decode("utf-8", "replace") if isinstance(data, bytes) else str(data)
        return text[-limit:].strip()


#: Servers kept alive for reuse, least recently used first.
_SERVERS: "OrderedDict[str, _Server]" = OrderedDict()
#: Every server this process started, cached or not (for atexit cleanup only).
_OWNED: set[_Server] = set()
_LOCK = threading.RLock()
_ATEXIT_REGISTERED = False
#: Servers this process started (borrowed ones are not counted).
_STARTED = 0


def _ensure_atexit() -> None:
    global _ATEXIT_REGISTERED
    if not _ATEXIT_REGISTERED:
        atexit.register(shutdown_all_servers)
        _ATEXIT_REGISTERED = True


# ---------------------------------------------------------------------------
# Public test/CLI-visible helpers
# ---------------------------------------------------------------------------


def cached_server_count() -> int:
    """Number of idle/reusable servers currently held in the module cache."""
    with _LOCK:
        return len(_SERVERS)


def server_start_count() -> int:
    """How many ``dolt sql-server`` processes this process started so far."""
    with _LOCK:
        return _STARTED


def shutdown_all_servers() -> None:
    """Kill every server this process started and clear the reuse cache.

    Borrowed servers are forgotten, never killed (another process owns them).
    Safe to call repeatedly; called at interpreter exit and by the pytest
    session-finish hook.
    """
    with _LOCK:
        servers = list(_SERVERS.values())
        _SERVERS.clear()
        owned = list(_OWNED)
        _OWNED.clear()
    seen: set[int] = set()
    for server in [*servers, *owned]:
        if id(server) in seen:
            continue
        seen.add(id(server))
        if server.owned:
            server.stop()
        else:
            server._close_stderr()


def discard_server(path: str | Path) -> None:
    """Forget and (when we own it) stop one ledger path's cached server.

    Used by ledger lifecycle paths that will not reuse the server: a failed
    operation, an adopt dry-run, or a template that is about to be copied.  A
    borrowed server is only forgotten; its owning process keeps it alive.
    """
    resolved = Path(path).expanduser().resolve()
    key = str(resolved)
    with _LOCK:
        server = _SERVERS.get(key)
        if server is not None and server.refs > 0:
            # Another open driver still uses it; a discard never breaks a
            # caller that is mid-command (e.g. a failed second open while the
            # first Ledger is still open in the same process).
            return
        _SERVERS.pop(key, None)
        if server is not None:
            _OWNED.discard(server)
    if server is None:
        return
    if server.owned:
        server.stop()
    else:
        server._close_stderr()


# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------


def _effective_env(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def _cache_cap(env: Mapping[str, str] | None) -> int:
    raw = _effective_env(env).get("TETRAMARK_SERVER_CACHE", str(_DEFAULT_CACHE_SIZE))
    try:
        cap = int(str(raw).strip())
    except (TypeError, ValueError):
        return _DEFAULT_CACHE_SIZE
    return max(0, cap)


def _fingerprint(env: Mapping[str, str] | None) -> str:
    """A stable digest of the environment the server was started with.

    The server reads Dolt's global config (identity, GC settings) from this
    environment, so a cached server is only reused for an identical one.  The
    comparison is skipped while an existing connection is open: one database
    directory cannot host a second ``dolt sql-server`` anyway, and killing the
    running one would break the caller that still uses it.
    """
    payload = json.dumps(
        sorted((str(key), str(value)) for key, value in _effective_env(env).items()),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8", "surrogatepass")).hexdigest()


# ---------------------------------------------------------------------------
# Process plumbing
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _connect(port: int, database: str, *, timeout: float = 10.0) -> Any:
    """Open one PyMySQL connection (the caller closes it)."""
    if pymysql is None:  # pragma: no cover - optional dependency
        raise UnreachableError(
            "TETRAMARK_DRIVER=server needs PyMySQL: install the 'pymysql' "
            "package (it is in the dev dependency group) or unset "
            "TETRAMARK_DRIVER to use the embedded driver"
        )
    return pymysql.connect(
        host="127.0.0.1",
        port=port,
        user="root",
        password="",
        database=database,
        charset="utf8mb4",
        autocommit=True,
        cursorclass=DictCursor,
        connect_timeout=timeout,
        read_timeout=60,
        write_timeout=60,
    )


def _check_serves(port: int, database: str) -> bool:
    """True when ``port`` speaks MySQL and serves ``database``."""
    if pymysql is None:  # pragma: no cover - optional dependency
        return False
    try:
        connection = _connect(port, database, timeout=1.0)
    except Exception:  # noqa: BLE001 - any failure means "not ours"
        return False
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT DATABASE()")
            row = cursor.fetchone()
    except Exception:  # noqa: BLE001 - connect worked, query did not
        return False
    finally:
        try:
            connection.close()
        except Exception:  # pragma: no cover - defensive
            pass
    return bool(row) and str(row.get("DATABASE()")) == database


def _dir_identity(path: Path) -> tuple[int, int] | None:
    """``(st_dev, st_ino)`` of the ledger directory, or ``None`` if it vanished."""
    try:
        status = os.stat(path)
    except OSError:
        return None
    return (status.st_dev, status.st_ino)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _sidecar_path(path: Path) -> Path:
    return path / ".dolt" / _SIDECAR_FILE


def _read_sidecar(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(_sidecar_path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_sidecar(server: _Server) -> None:
    payload = {
        "path": str(server.path),
        "pid": os.getpid(),
        "port": server.port,
        "identity": list(server.identity or ()),
    }
    try:
        _sidecar_path(server.path).write_text(
            json.dumps(payload, sort_keys=True), encoding="utf-8"
        )
    except OSError:  # pragma: no cover - read-only ledger directory
        pass


def _remove_dolt_info(path: Path, server_pid: int) -> None:
    """Remove Dolt's discovery file if it belongs to our stopped server.

    A graceful shutdown removes it itself; this covers a killed process, so a
    copied template never carries a stale port.  Never touches a file a newer
    server wrote.
    """
    info = path / ".dolt" / _INFO_FILE
    try:
        recorded = int(info.read_text(encoding="utf-8").split(":")[0])
    except (OSError, IndexError, ValueError):
        return
    if recorded != server_pid:
        return
    try:
        info.unlink()
    except OSError:  # pragma: no cover - defensive
        pass


def _remove_sidecar(path: Path) -> None:
    """Remove our sidecar after our server stopped; never a foreign one."""
    payload = _read_sidecar(path)
    if payload is None or payload.get("pid") != os.getpid():
        return
    try:
        _sidecar_path(path).unlink()
    except OSError:  # pragma: no cover - defensive
        pass


def _borrow_server(path: Path, database: str) -> _Server | None:
    """A live server another process runs on *this* ledger, if any.

    Only Tetramark's own sidecar is trusted: it records the resolved path and
    the directory inode, so a template copy can never borrow the template's
    server even when both databases share a basename.  A stale sidecar (dead
    process, replaced directory) is ignored and a fresh server is started.
    """
    payload = _read_sidecar(path)
    if payload is None:
        return None
    try:
        if str(payload.get("path")) != str(path):
            return None
        identity = tuple(payload.get("identity") or ())
        if identity != _dir_identity(path):
            return None
        port = int(payload.get("port"))
        pid = int(payload.get("pid"))
    except (TypeError, ValueError):
        return None
    if not 0 < port < 65536:
        return None
    if not _pid_alive(pid):
        return None
    if not _check_serves(port, database):
        return None
    return _Server(
        path,
        port,
        process=None,
        stderr=None,
        fingerprint=None,
        cache_cap=0,
        identity=identity,
    )


def _await_borrow_server(
    path: Path, database: str, *, timeout: float = 2.0
) -> _Server | None:
    """Borrow a server that a racing process is just finishing starting."""
    deadline = time.monotonic() + timeout
    while True:
        borrowed = _borrow_server(path, database)
        if borrowed is not None:
            return borrowed
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.05)


class _PortInUse(RuntimeError):
    """A parallel worker claimed the free port between probing and binding."""


def _is_start_race(output: str) -> bool:
    """Whether a failed start lost a race to another sql-server on the ledger."""
    lowered = output.lower()
    return (
        "already in use" in lowered
        or "locked by another dolt process" in lowered
    )


def _start_server(
    path: Path, binary: str, env: Mapping[str, str] | None, fingerprint: str
) -> _Server:
    """Start one ``dolt sql-server``, retrying a lost free-port race."""
    last_error: _PortInUse | None = None
    for attempt in range(3):
        try:
            return _spawn_server(path, binary, env, fingerprint)
        except _PortInUse as error:
            last_error = error
            time.sleep(0.05 * (attempt + 1))
    assert last_error is not None
    raise last_error


def _spawn_server(
    path: Path, binary: str, env: Mapping[str, str] | None, fingerprint: str
) -> _Server:
    if pymysql is None:  # pragma: no cover - optional dependency
        raise UnreachableError(
            "TETRAMARK_DRIVER=server needs PyMySQL: install the 'pymysql' "
            "package (it is in the dev dependency group) or unset "
            "TETRAMARK_DRIVER to use the embedded driver"
        )
    port = _free_port()
    argv = [
        binary,
        "sql-server",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--loglevel",
        "error",
    ]
    stderr = tempfile.TemporaryFile()
    server_env = dict(env) if env is not None else None
    try:
        process = subprocess.Popen(
            argv,
            cwd=path,
            env=server_env,
            stdin=subprocess.DEVNULL,
            stdout=stderr,
            stderr=stderr,
        )
    except OSError as error:
        stderr.close()
        raise UnreachableError(
            f"cannot start 'dolt sql-server' in {path}: {error}"
        ) from error

    global _STARTED
    with _LOCK:
        _STARTED += 1
    server = _Server(
        path,
        port,
        process=process,
        stderr=stderr,
        fingerprint=fingerprint,
        cache_cap=0,
        identity=_dir_identity(path),
    )
    deadline = time.monotonic() + _START_TIMEOUT_SECONDS
    while True:
        if process.poll() is not None:
            output = server.stderr_tail()
            server._close_stderr()
            message = (
                f"'dolt sql-server' exited with code {process.returncode} in "
                f"{path}" + (f":\n{output}" if output else "")
            )
            if _is_start_race(output):
                raise _PortInUse(message)
            raise UnreachableError(message)
        if _check_serves(port, path.name):
            _write_sidecar(server)
            return server
        if time.monotonic() > deadline:
            output = server.stderr_tail()
            server.stop()
            raise UnreachableError(
                f"'dolt sql-server' in {path} did not become ready within "
                f"{_START_TIMEOUT_SECONDS:.0f}s"
                + (f":\n{output}" if output else "")
            )
        time.sleep(_PROBE_INTERVAL_SECONDS)


def _drop(server: _Server, *, key: str | None = None) -> None:
    """Remove a server from the cache and stop it when we own it."""
    _SERVERS.pop(key if key is not None else str(server.path), None)
    _OWNED.discard(server)
    server.stop()


def _enforce_cache_cap(cap: int, keep: _Server) -> None:
    """Evict least-recently-used idle servers until the cache fits ``cap``."""
    if cap <= 0:
        return
    while len(_SERVERS) > cap:
        victim_key = None
        for key, server in _SERVERS.items():
            if server is not keep and server.refs == 0:
                victim_key = key
                break
        if victim_key is None:
            return  # every cached server is in use; allow a temporary overrun
        victim = _SERVERS[victim_key]
        _drop(victim, key=victim_key)


def _acquire(
    path: Path, database: str, env: Mapping[str, str] | None, binary: str
) -> _Server:
    """Return a running server for ``path``, reusing or starting one."""
    _ensure_atexit()
    fingerprint = _fingerprint(env)
    cap = _cache_cap(env)
    key = str(path)
    current_identity = _dir_identity(path)
    with _LOCK:
        server = _SERVERS.get(key)
        if server is not None and not server.alive():
            _drop(server, key=key)
            server = None
        if (
            server is not None
            and server.owned
            and server.refs == 0
            and server.identity != current_identity
        ):
            # The path now holds a different directory (a test replaced it, a
            # template copy, ...): the cached server serves the old inode.
            _drop(server, key=key)
            server = None
        if (
            server is not None
            and server.owned
            and server.refs == 0
            and server.fingerprint != fingerprint
            and cap > 0
        ):
            # No connection is using it, so the environment may be swapped
            # safely: one directory can host only one server, and the cached
            # one was started under a different DOLT_ROOT_PATH/HOME.
            _drop(server, key=key)
            server = None
        if server is not None and server.owned and server.refs == 0 and cap == 0:
            _drop(server, key=key)
            server = None
        if server is None:
            borrowed = _borrow_server(path, database)
            if borrowed is not None:
                server = borrowed
            else:
                try:
                    server = _start_server(path, binary, env, fingerprint)
                except _PortInUse as error:
                    # Another process won the race for this database (the
                    # same directory allows one sql-server): wait for its
                    # sidecar and borrow it instead of failing.
                    borrowed = _await_borrow_server(path, database)
                    if borrowed is None:
                        raise UnreachableError(str(error)) from error
                    server = borrowed
                else:
                    _OWNED.add(server)
            server.cache_cap = cap
            if cap > 0:
                _SERVERS[key] = server
        server.refs += 1
        if key in _SERVERS:
            _SERVERS.move_to_end(key)
        _enforce_cache_cap(cap, server)
        return server


def _release(server: _Server) -> None:
    """Drop one connection's reference; stop the server when it is not reused."""
    with _LOCK:
        server.refs = max(0, server.refs - 1)
        if server.refs:
            return
        key = str(server.path)
        cached = _SERVERS.get(key) is server
        if not server.owned:
            # A foreign process owns it: forget it, never signal it.
            if cached:
                _SERVERS.pop(key, None)
            server._close_stderr()
            return
        if not cached or server.cache_cap == 0:
            _drop(server, key=key)


# ---------------------------------------------------------------------------
# Value normalisation: match the embedded driver's ``-r json`` shapes
# ---------------------------------------------------------------------------


def _normalise(value: Any) -> Any:
    """Convert a PyMySQL value to the shape ``dolt sql -r json`` reports."""
    if value is None:
        return None
    if isinstance(value, bool):  # before int: bool is an int subclass
        return 1 if value else 0
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime.datetime):
        return str(value)
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, datetime.timedelta):
        return str(value)
    if isinstance(value, (bytes, bytearray)):
        # Dolt's JSON output decodes binary as UTF-8 (replacing bad bytes).
        return bytes(value).decode("utf-8", "replace")
    return value


def _normalise_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): _normalise(value) for key, value in row.items()}


def _commit_hash(result_sets: Sequence[Sequence[Mapping[str, Any]]]) -> str | None:
    """The hash of the ``DOLT_COMMIT`` result in a commit script's output."""
    for rows in reversed(result_sets):
        for row in rows:
            if "hash" in row:
                return str(row["hash"])
    return None


# ---------------------------------------------------------------------------
# The driver
# ---------------------------------------------------------------------------


class ServerDriver(Driver):
    """Talk to one Dolt database through a managed ``dolt sql-server``.

    ``branch`` selects a session-scoped ``db/branch`` revision database via the
    connection's default database, exactly like the embedded driver's ``USE``.
    ``validate_branch=True`` (the default) checks it exists immediately; a
    caller that folds that read into its own batch passes ``False``.
    ``fill_columns`` is accepted and ignored: the MySQL protocol returns every
    column of every row, ``NULL`` included, without a second round trip.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        branch: str | None = None,
        validate_branch: bool = True,
        dolt_binary: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._path = Path(path).expanduser().resolve()
        binary = dolt_binary or shutil.which("dolt")
        if binary is None:
            raise UnreachableError("the 'dolt' binary was not found on PATH")
        self._dolt = binary
        self._env = dict(env) if env is not None else None
        if not (self._path / ".dolt").is_dir():
            raise UnreachableError(f"not a Dolt database: {self._path}")
        self._branch: str | None = None
        self._server: _Server | None = None
        self._conn: Any = None
        self._in_transaction = False
        self._closed = False
        if branch is not None:
            name = check_branch_name(branch)
            try:
                if validate_branch:
                    # The existence query runs on the checked-out database, so
                    # it must happen before the session selects ``db/branch``.
                    self._require_branch(name)
            except BaseException:
                self.close()
                raise
            self._branch = name
            if self._conn is not None:
                self._select_branch()

    # -- identity ---------------------------------------------------------
    @property
    def path(self) -> Path:
        """The ledger's data directory."""
        return self._path

    @property
    def database(self) -> str:
        """Dolt names a database after its directory."""
        return self._path.name

    @property
    def branch(self) -> str | None:
        """The branch this session reads and writes, or ``None`` when checked out."""
        return self._branch

    # -- connection -------------------------------------------------------
    def _require_branch(self, branch: str) -> str:
        """Fail with a usage error when ``branch`` does not exist (never create)."""
        name = check_branch_name(branch)
        rows = self.query(
            "SELECT `name` FROM dolt_branches WHERE `name` = %s",
            (name,),
            fill_columns=False,
        )
        if not rows:
            raise UsageError(
                f"branch {name!r} does not exist in database {self.database!r} "
                "(it is never created implicitly)"
            )
        return name

    def _ensure_connection(self) -> Any:
        if self._conn is not None:
            return self._conn
        if self._closed:
            raise LedgerError("this driver is closed")
        if self._server is None:
            self._server = _acquire(
                self._path, self.database, self._env, self._dolt
            )
        server = self._server
        try:
            connection = _connect(server.port, self.database)
        except Exception as error:  # noqa: BLE001 - mapped below
            self._server = None
            _release(server)
            raise UnreachableError(
                f"cannot connect to the dolt sql-server for {self._path}: {error}"
            ) from error
        self._conn = connection
        if self._branch is not None:
            try:
                self._select_branch()
            except BaseException:
                self._conn = None
                try:
                    connection.close()
                except Exception:  # pragma: no cover - defensive
                    pass
                self._server = None
                _release(server)
                raise
        return self._conn

    def _select_branch(self) -> None:
        assert self._conn is not None and self._branch is not None
        try:
            self._conn.select_db(f"{self.database}/{self._branch}")
        except Exception as error:  # noqa: BLE001 - mapped below
            raise _as_ledger_error(error) from error

    def close(self) -> None:
        """Close this driver's connection; the cached server may stay alive."""
        if self._closed:
            return
        self._closed = True
        connection, self._conn = self._conn, None
        if connection is not None:
            try:
                connection.close()
            except Exception:  # pragma: no cover - defensive
                pass
        server, self._server = self._server, None
        if server is not None:
            _release(server)

    def discard(self) -> None:
        """Close this driver and stop this ledger's cached server.

        For operations that will not reuse it (a failure, a dry run, a ledger
        about to be copied for a test template).  After this call no
        ``dolt sql-server`` serves ``self.path`` from this process, so a plain
        ``dolt sql`` there goes back to the embedded engine and keeps its typed
        JSON output.
        """
        path = self._path
        self.close()
        with _LOCK:
            cached = _SERVERS.get(str(path))
            if cached is not None and cached.refs > 0:
                return  # another open driver still uses this server
        discard_server(path)

    # -- error mapping ----------------------------------------------------
    def _execute_statement(self, sql: str, params: Params) -> list[dict[str, Any]]:
        connection = self._ensure_connection()
        try:
            with connection.cursor() as cursor:
                if params is None:
                    cursor.execute(sql)
                else:
                    cursor.execute(sql, params)
                if cursor.description is None:
                    return []
                return [_normalise_row(row) for row in cursor.fetchall()]
        except Exception as error:  # noqa: BLE001 - mapped below
            raise _as_ledger_error(error) from error

    # -- reads / writes ---------------------------------------------------
    def query(
        self, sql: str, params: Params = None, *, fill_columns: bool = True
    ) -> list[dict[str, Any]]:
        """Run one read; rows carry every column, ``NULL`` keys included."""
        del fill_columns  # the MySQL protocol already returns every column
        return self._execute_statement(sql, params)

    def query_many(
        self,
        statements: Sequence[tuple[str, Params]],
        *,
        fill_columns: bool = True,
    ) -> list[list[dict[str, Any]]]:
        """Run a whole read batch on one connection, one result per statement."""
        return [
            self.query(sql, params, fill_columns=fill_columns)
            for sql, params in statements
        ]

    def execute(self, sql: str, params: Params = None) -> None:
        self._execute_statement(sql, params)
        if self._in_transaction and is_ddl(sql):
            # Dolt commits DDL immediately, which ends the SQL transaction;
            # re-enter it so later DML still rolls back with the session.
            self._begin()

    def execute_many(self, statements: Sequence[str | tuple[str, Params]]) -> None:
        """Run a writing batch in order; one connection, no process per item."""
        ordered = list(statements)
        if not ordered:
            return
        self._ensure_connection()
        ddl = False
        total = len(ordered)
        for index, statement in enumerate(ordered, start=1):
            if isinstance(statement, tuple):
                sql, params = statement
            else:
                sql, params = statement, None
            try:
                self._execute_statement(sql, params)
            except LedgerError as error:
                rendered = " ".join(render_statement(statement).split())
                if len(rendered) > 160:
                    rendered = rendered[:157] + "..."
                message = f"statement {index} of {total} failed: {rendered}"
                raise LedgerError(f"{message}\n{error}") from error
            ddl = ddl or is_ddl(sql)
        if ddl and self._in_transaction:
            self._begin()

    def _begin(self) -> None:
        self._execute_statement("START TRANSACTION", None)

    @contextmanager
    def transaction(self) -> Iterator[ServerDriver]:
        """Send a real ``START TRANSACTION``; roll back when the body raises."""
        if self._in_transaction:
            raise LedgerError("nested transactions are not supported")
        self._ensure_connection()
        self._begin()
        self._in_transaction = True
        try:
            yield self
        except BaseException:
            self._in_transaction = False
            try:
                self._execute_statement("ROLLBACK", None)
            except LedgerError:  # pragma: no cover - connection already gone
                pass
            raise
        else:
            self._in_transaction = False
            self._execute_statement("COMMIT", None)

    # -- history ----------------------------------------------------------
    def commit(self, message: str, author: str | None = None) -> str | None:
        if self._in_transaction:
            raise LedgerError("cannot create a Dolt commit inside an open transaction")
        statements = commit_statements(message, author)
        try:
            results = [self._execute_statement(sql, None) for sql in statements]
        except LedgerError as error:
            if is_nothing_to_commit(str(error)):
                return None
            raise
        return _commit_hash(results)

    def commit_pending(
        self,
        transaction: AbstractContextManager[Driver],
        message: str,
        author: str | None = None,
    ) -> str | None:
        """End the SQL transaction and create the one Dolt commit.

        ``transaction`` is the entered context manager returned by
        :meth:`transaction`; it is closed exactly once.  ``DOLT_COMMIT`` must
        not run inside an open SQL transaction, so the order is ``COMMIT`` (the
        buffered DML is already sent by the write session) followed by
        ``DOLT_ADD``/``DOLT_COMMIT`` -- the same observable result as the
        embedded driver's one-process flush-plus-commit script, with no extra
        process.
        """
        if not self._in_transaction:
            return super().commit_pending(transaction, message, author)
        results: list[list[dict[str, Any]]] = []
        try:
            statements = list(commit_statements(message, author))
            transaction.__exit__(None, None, None)
            try:
                results = [self._execute_statement(sql, None) for sql in statements]
            except LedgerError as error:
                if is_nothing_to_commit(str(error)):
                    return None
                raise
        finally:
            if self._in_transaction:
                # ``commit_statements`` refused the message before the
                # transaction was closed: close it, like the embedded driver.
                transaction.__exit__(None, None, None)
        return _commit_hash(results)


def _as_ledger_error(error: BaseException) -> LedgerError:
    """Map a PyMySQL/Dolt error to a LedgerError with Dolt's message text."""
    if pymysql is not None and isinstance(error, pymysql.MySQLError):
        message = error.args[1] if len(error.args) > 1 else str(error)
        return LedgerError(str(message))
    if isinstance(error, LedgerError):
        return error
    return LedgerError(str(error))
