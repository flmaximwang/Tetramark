"""Workspaces: one ``data_dir`` holding ledgers, served by one ``dolt sql-server``.

A workspace is a directory containing ``workspace.yaml``; every direct
subdirectory with a ``.dolt`` directory is a ledger.  This module ports the
behaviour of ``dolt_start.sh`` / ``dolt_stop.sh`` (pid and log in the data_dir,
readiness probe, port-busy detection) and picks the right driver for a ledger.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import socket
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from tetramark.driver.base import Driver
from tetramark.driver.embedded import EmbeddedDriver
from tetramark.driver.server import ServerDriver
from tetramark.errors import LedgerError, UnreachableError, UsageError

__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "LOG_FILE",
    "PID_FILE",
    "WORKSPACE_FILE",
    "ServerStatus",
    "Workspace",
    "WorkspaceConfig",
]

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 13308
WORKSPACE_FILE = "workspace.yaml"
PID_FILE = "dolt.pid"
LOG_FILE = "dolt.log"

#: Seconds to wait for a starting server to accept connections / for a
#: stopping one to exit, matching the shell scripts.
READY_TIMEOUT = 10.0


class WorkspaceConfig(BaseModel):
    """Contents of ``workspace.yaml``."""

    model_config = ConfigDict(extra="allow")

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    default_ledger: str | None = None
    author: str | None = None
    #: Directory holding ``<name>.yaml`` presets for bare ``--preset NAME``.
    #: A relative path is resolved against ``workspace.yaml``.
    presets_dir: str | None = None


@dataclass
class ServerStatus:
    """Snapshot of the workspace's server, as printed by ``tm server status``."""

    data_dir: Path
    host: str
    port: int
    running: bool
    pid: int | None = None
    ledgers: list[str] = field(default_factory=list)
    default_ledger: str | None = None
    port_busy: bool = False
    pid_file: Path | None = None
    log_file: Path | None = None

    def endpoint(self, ledger: str | None = None) -> str:
        """A ``mysql://`` URL for humans and Workbench."""
        suffix = f"/{ledger}" if ledger else ""
        return f"mysql://root@{self.host}:{self.port}{suffix}"


class Workspace:
    """A Dolt data directory plus its ``workspace.yaml``."""

    def __init__(
        self,
        data_dir: str | Path,
        config: WorkspaceConfig | None = None,
        *,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.config = config or WorkspaceConfig()
        self._env = dict(env) if env is not None else None

    # -- construction -----------------------------------------------------
    @property
    def env(self) -> dict[str, str] | None:
        """The environment every ``dolt``/``git`` subprocess should inherit.

        Tests create workspaces with an isolated ``DOLT_ROOT_PATH``/``HOME``;
        production workspaces leave it ``None`` and inherit the process
        environment.  ``Ledger`` passes this to the embedded driver.
        """
        return self._env

    @property
    def config_path(self) -> Path:
        return self.data_dir / WORKSPACE_FILE

    @property
    def pid_file(self) -> Path:
        return self.data_dir / PID_FILE

    @property
    def presets_dir_path(self) -> Path | None:
        """The workspace's ``presets_dir`` as an absolute path (``None`` if unset).

        Relative values are resolved against ``workspace.yaml`` (the data_dir),
        never against the caller's cwd.
        """
        raw = self.config.presets_dir
        if raw is None or not str(raw).strip():
            return None
        path = Path(str(raw)).expanduser()
        if not path.is_absolute():
            path = self.data_dir / path
        return path

    @property
    def log_file(self) -> Path:
        return self.data_dir / LOG_FILE

    @classmethod
    def create(
        cls,
        path: str | Path,
        *,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        default_ledger: str | None = None,
        author: str | None = None,
        presets_dir: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> Workspace:
        """Create ``workspace.yaml`` in ``path`` (the directory is made if needed)."""
        data_dir = Path(path).expanduser().resolve()
        data_dir.mkdir(parents=True, exist_ok=True)
        workspace = cls(
            data_dir,
            WorkspaceConfig(
                host=host,
                port=port,
                default_ledger=default_ledger,
                author=author,
                presets_dir=presets_dir,
            ),
            env=env,
        )
        if workspace.config_path.exists():
            raise LedgerError(f"a workspace already exists at {workspace.config_path}")
        workspace.save()
        return workspace

    def save(self) -> None:
        """Write ``workspace.yaml`` (UTF-8, stable key order)."""
        payload = self.config.model_dump(exclude_none=True)
        self.config_path.write_text(
            yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: str | Path, *, env: Mapping[str, str] | None = None) -> Workspace:
        """Load a workspace from its directory or from the YAML file itself."""
        target = Path(path).expanduser()
        config_path = target / WORKSPACE_FILE if target.is_dir() else target
        if not config_path.is_file():
            raise UsageError(f"no {WORKSPACE_FILE} at {config_path}")
        try:
            payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as error:
            raise UsageError(f"invalid {config_path}: {error}") from error
        if not isinstance(payload, dict):
            raise UsageError(f"invalid {config_path}: expected a mapping")
        try:
            config = WorkspaceConfig(**payload)
        except ValidationError as error:
            raise UsageError(f"invalid {config_path}: {error}") from error
        return cls(config_path.parent, config, env=env)

    @classmethod
    def discover(
        cls,
        start: str | Path | None = None,
        *,
        env: Mapping[str, str] | None = None,
    ) -> Workspace | None:
        """Search upward from ``start`` (default cwd), then ``$TETRAMARK_WORKSPACE``."""
        environ = os.environ if env is None else env
        origin = Path(start) if start is not None else Path.cwd()
        origin = origin.expanduser().resolve()
        if origin.is_file():
            origin = origin.parent
        for candidate in (origin, *origin.parents):
            if (candidate / WORKSPACE_FILE).is_file():
                return cls.load(candidate, env=environ)
        fallback = environ.get("TETRAMARK_WORKSPACE")
        if fallback:
            return cls.load(fallback, env=environ)
        return None

    # -- ledgers ----------------------------------------------------------
    def list_ledgers(self) -> list[str]:
        """Names of direct subdirectories that are Dolt databases."""
        if not self.data_dir.is_dir():
            return []
        return sorted(
            entry.name
            for entry in self.data_dir.iterdir()
            if entry.is_dir()
            and not entry.name.startswith(".")
            and (entry / ".dolt").is_dir()
        )

    def ledger_path(self, ledger: str) -> Path:
        return self.data_dir / ledger

    # -- server -----------------------------------------------------------
    def start(self, daemon: bool = False) -> int | None:
        """Start ``dolt sql-server`` for this data directory.

        Returns the pid for a daemon start and ``None`` for a foreground start
        (which blocks until the server exits).
        """
        if not self.list_ledgers():
            raise LedgerError(
                f"no ledgers in {self.data_dir}: create a database first "
                "(a direct subdirectory containing .dolt)"
            )
        running = self.running_pid()
        if running is not None:
            raise LedgerError(f"the Dolt server is already running (pid {running})")
        if self.port_in_use():
            raise LedgerError(
                f"port {self.config.port} is already in use on {self.config.host}"
            )

        argv = self._server_argv()
        if not daemon:
            subprocess.run(argv, cwd=self.data_dir, env=self._env)
            return None

        self.log_file.touch(exist_ok=True)
        with self.log_file.open("a", encoding="utf-8") as log:
            process = subprocess.Popen(
                argv,
                cwd=self.data_dir,
                env=self._env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                text=True,
            )
        self.pid_file.write_text(str(process.pid), encoding="utf-8")
        if not self._wait_until_ready(process):
            self._terminate(process.pid)
            self.pid_file.unlink(missing_ok=True)
            raise LedgerError(
                f"the Dolt server did not become ready within {READY_TIMEOUT:g} s; "
                f"last log lines:\n{self.log_tail()}"
            )
        return process.pid

    def stop(self) -> bool:
        """Stop the server; return ``False`` when none was running."""
        pid = self.running_pid()
        if pid is None:
            self.pid_file.unlink(missing_ok=True)
            return False
        self._terminate(pid)
        self.pid_file.unlink(missing_ok=True)
        return True

    def running_pid(self) -> int | None:
        """Pid of the server for this data directory, if it is alive."""
        pid = self._read_pid_file()
        if pid is not None and _pid_alive(pid):
            return pid
        if pid is not None:  # stale pid file
            self.pid_file.unlink(missing_ok=True)
        return self._pgrep_pid()

    def status(self) -> ServerStatus:
        pid = self.running_pid()
        return ServerStatus(
            data_dir=self.data_dir,
            host=self.config.host,
            port=self.config.port,
            running=pid is not None,
            pid=pid,
            ledgers=self.list_ledgers(),
            default_ledger=self.config.default_ledger,
            port_busy=self.port_in_use(),
            pid_file=self.pid_file,
            log_file=self.log_file,
        )

    def port_in_use(self) -> bool:
        """True when something accepts TCP connections on the workspace port."""
        try:
            with socket.create_connection(
                (self.config.host, self.config.port), timeout=0.25
            ):
                return True
        except OSError:
            return False

    def log_tail(self, lines: int = 20) -> str:
        try:
            content = self.log_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return f"(no log at {self.log_file})"
        return "\n".join(content.splitlines()[-lines:])

    # -- drivers ----------------------------------------------------------
    def driver_for(self, ledger: str | None = None, *, branch: str | None = None) -> Driver:
        """A :class:`ServerDriver` when the server is up, else an :class:`EmbeddedDriver`.

        ``branch`` selects a session branch through a ``db/branch`` revision
        database; the repository's checked-out branch is left untouched.
        """
        name = ledger or self.config.default_ledger
        if not name:
            raise UsageError(
                "no ledger selected: pass --ledger or set default_ledger in workspace.yaml"
            )
        path = self.ledger_path(name)
        if self.running_pid() is not None and self.port_in_use():
            return ServerDriver(
                self.config.host, self.config.port, name, branch=branch
            )
        if not (path / ".dolt").is_dir():
            raise UnreachableError(f"ledger {name!r} not found in {self.data_dir}")
        return EmbeddedDriver(path, branch=branch, env=self._env)

    # -- internals --------------------------------------------------------
    def _server_argv(self) -> list[str]:
        binary = shutil.which("dolt")
        if binary is None:
            raise UnreachableError("the 'dolt' binary was not found on PATH")
        return [
            binary,
            "sql-server",
            "--data-dir",
            str(self.data_dir),
            "-H",
            self.config.host,
            "-P",
            str(self.config.port),
            "-l",
            "info",
        ]

    def _read_pid_file(self) -> int | None:
        try:
            raw = self.pid_file.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        try:
            return int(raw) if raw else None
        except ValueError:
            return None

    def _pgrep_pid(self) -> int | None:
        pattern = f"dolt sql-server.*--data-dir[= ]{re.escape(str(self.data_dir))}"
        try:
            result = subprocess.run(
                ["pgrep", "-f", pattern],
                capture_output=True,
                text=True,
                env=self._env,
                check=False,
            )
        except OSError:
            return None
        for line in result.stdout.split():
            try:
                pid = int(line)
            except ValueError:
                continue
            if _pid_alive(pid):
                return pid
        return None

    def _wait_until_ready(self, process: subprocess.Popen[str]) -> bool:
        deadline = time.monotonic() + READY_TIMEOUT
        while time.monotonic() < deadline:
            if process.poll() is not None:
                return False
            if self.port_in_use():
                return True
            time.sleep(0.1)
        return False

    def _terminate(self, pid: int) -> None:
        """SIGTERM, wait up to :data:`READY_TIMEOUT`, then SIGKILL (as dolt_stop.sh)."""
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + READY_TIMEOUT
        while time.monotonic() < deadline:
            _reap_child(pid)
            if not _pid_alive(pid):
                return
            time.sleep(0.1)
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            _reap_child(pid)
            if not _pid_alive(pid):
                return
            time.sleep(0.05)


def _reap_child(pid: int) -> None:
    """Reap a server this process started.

    ``Workspace.stop()`` is also called by the very process that called
    ``start()`` (tests, ``tm server start`` in a REPL).  Without ``waitpid`` the
    exited server stays a zombie, which ``os.kill(pid, 0)`` reports as alive, so
    the wait loop burns the whole ``READY_TIMEOUT`` (measured: 12 s per stop).
    """
    try:
        os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        pass


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
