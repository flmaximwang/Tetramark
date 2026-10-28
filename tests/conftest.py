"""Shared pytest fixtures.

Tests run against the real ``dolt`` binary (never a mock). Every fixture that
shells out points ``DOLT_ROOT_PATH``, ``HOME``, ``XDG_CONFIG_HOME`` and the Git
config env vars at the pytest ``tmp_path`` so the developer's global Dolt/Git
configuration is never read or written.
"""

from __future__ import annotations

import itertools
import os
import shutil
import socket
import subprocess
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pymysql
import pytest

from tetramark.core.ledger import (
    Ledger,
    init_ledger,
    init_ledger_on_server,
    open as open_ledger,
)
from tetramark.driver.workspace import Workspace

DOLT_BINARY = shutil.which("dolt")

#: The presets from ticket 03's test data (directory presets, path-style).
PRESETS = Path(__file__).parent / "data" / "presets"

#: The repository's own presets; workspaces point `presets_dir` at it so
#: ``tm init --preset NAME`` works (ticket 20: presets are not package data).
REPO_PRESETS = Path(__file__).resolve().parents[1] / "presets"

#: Author used by :func:`ledger_factory` so tests never read a global identity.
FACTORY_AUTHOR = "Ledger Factory <factory@tetramark.invalid>"


#: ``subprocess.run`` signature use in fixtures/tests: run_dolt("sql", "-q", "SELECT 1")
RunDolt = Callable[..., subprocess.CompletedProcess[str]]


def isolated_env(root: Path, home: Path) -> dict[str, str]:
    """An environment whose global Dolt/Git config lives under ``root``/``home``."""
    root.mkdir(parents=True, exist_ok=True)
    home.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["DOLT_ROOT_PATH"] = str(root)
    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    env["GIT_CONFIG_GLOBAL"] = str(home / ".gitconfig")
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    env.pop("GIT_CONFIG_NOSYSTEM", None)
    env.pop("TETRAMARK_WORKSPACE", None)
    return env


def free_port() -> int:
    """An unused TCP port on 127.0.0.1."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def worker_id() -> str:
    """The xdist worker name (``gw0`` when not running under xdist)."""
    return os.environ.get("PYTEST_XDIST_WORKER") or "gw0"


@contextmanager
def admin_connection(workspace: Workspace) -> Iterator[pymysql.connections.Connection]:
    """A short-lived admin connection (no database selected); never leaks."""
    connection = pymysql.connect(
        host=workspace.config.host,
        port=workspace.config.port,
        user="root",
        password="",
        autocommit=True,
        charset="utf8mb4",
        connect_timeout=5,
    )
    try:
        yield connection
    finally:
        connection.close()


def database_names(workspace: Workspace) -> list[str]:
    """Every database the workspace server currently knows about."""
    with admin_connection(workspace) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SHOW DATABASES")
            return sorted(str(row[0]) for row in cursor.fetchall())


def drop_database(workspace: Workspace, name: str, *, missing_ok: bool = True) -> None:
    """``DROP DATABASE ``name`` (idempotent by default)."""
    clause = "IF EXISTS " if missing_ok else ""
    quoted = "`" + name.replace("`", "``") + "`"
    with admin_connection(workspace) as connection:
        with connection.cursor() as cursor:
            cursor.execute(f"DROP DATABASE {clause}{quoted}")


def _configure_dolt_identity(dolt_binary: str, env: dict[str, str]) -> None:
    """Give the isolated global Dolt config a user name and email."""
    for key, value in (
        ("user.name", "Tetramark Tests"),
        ("user.email", "tests@tetramark.invalid"),
    ):
        subprocess.run(
            [dolt_binary, "config", "--global", "--add", key, value],
            env=env,
            capture_output=True,
            check=True,
        )


def _test_server_pids(under: str, orphans_only: bool) -> list[int]:
    """Pids of ``dolt sql-server`` processes whose data dir lies under ``under``."""
    out = subprocess.run(
        ["ps", "-eo", "pid=,ppid=,command="], capture_output=True, text=True
    ).stdout
    pids = []
    for line in out.splitlines():
        pid, ppid, command = line.split(None, 2)
        if "dolt sql-server" in command and f"--data-dir {under}" in command:
            if not orphans_only or ppid == "1":
                pids.append(int(pid))
    return pids


@pytest.fixture(scope="session", autouse=True)
def _reap_test_servers(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Never leak test ``dolt sql-server`` processes.

    Servers are daemonised (own session), so they outlive a killed pytest.
    At start, kill orphans (parent gone) left under the pytest temp root by
    earlier runs; during the session, a watchdog shell kills this session's
    servers as soon as this pytest process dies, however it dies.
    """
    basetemp = str(tmp_path_factory.getbasetemp().resolve())
    temp_root = str(Path(basetemp).parent.parent)  # .../pytest-of-<user>
    for pid in _test_server_pids(temp_root, orphans_only=True):
        try:
            os.kill(pid, 15)
        except ProcessLookupError:
            pass
    watchdog = subprocess.Popen(
        [
            "/bin/sh",
            "-c",
            'while kill -0 "$1" 2>/dev/null; do sleep 1; done; '
            'pkill -f "dolt sql-server --data-dir $2"',
            "reaper",
            str(os.getpid()),
            basetemp,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    yield
    for pid in _test_server_pids(basetemp, orphans_only=False):
        try:
            os.kill(pid, 15)
        except ProcessLookupError:
            pass
    watchdog.kill()


@pytest.fixture(scope="session")
def dolt_binary() -> str:
    """Path to the real ``dolt`` binary; skips the test if it is not installed."""
    if DOLT_BINARY is None:
        pytest.skip("the 'dolt' binary is not installed or not on PATH")
    return DOLT_BINARY


@pytest.fixture
def dolt_env(tmp_path: Path) -> dict[str, str]:
    """An environment whose global Dolt/Git config lives under ``tmp_path``."""
    return isolated_env(tmp_path / "dolt-root", tmp_path / "home")


@pytest.fixture
def dolt_run(dolt_binary: str, dolt_env: dict[str, str], tmp_path: Path) -> RunDolt:
    """Run a ``dolt`` command in ``tmp_path`` (or a given ``cwd``), capturing output."""

    def run(
        *args: str,
        cwd: Path | None = None,
        check: bool = True,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [dolt_binary, *args],
            cwd=cwd or tmp_path,
            env=env or dolt_env,
            capture_output=True,
            text=True,
            check=check,
        )

    return run


@pytest.fixture
def dolt_conf(dolt_run: RunDolt) -> None:
    """Give the isolated global Dolt config a user name and email."""
    dolt_run("config", "--global", "--add", "user.name", "Tetramark Tests")
    dolt_run("config", "--global", "--add", "user.email", "tests@tetramark.invalid")


@pytest.fixture
def dolt_repo(tmp_path: Path, dolt_run: RunDolt, dolt_conf: None) -> Iterator[Path]:
    """An initialised Dolt data repository with an isolated global config.

    ``dolt config --global`` writes into ``$DOLT_ROOT_PATH`` (a tmp directory),
    so the user's real global config is untouched.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    dolt_run("init", cwd=repo)
    assert (repo / ".dolt").is_dir(), "dolt init did not create .dolt"
    yield repo


@pytest.fixture
def make_ledger(dolt_run: RunDolt, dolt_conf: None) -> Callable[[Path], Path]:
    """Initialise a Dolt database in a directory and return it."""

    def make(directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        dolt_run("init", cwd=directory)
        assert (directory / ".dolt").is_dir(), "dolt init did not create .dolt"
        return directory

    return make


@pytest.fixture
def port() -> int:
    """A random free TCP port."""
    return free_port()


@pytest.fixture
def embedded_ledger_factory(
    dolt_env: dict[str, str], tmp_path: Path
) -> Iterator[Callable[..., Ledger]]:
    """Create real ledgers in a ``tmp_path`` workspace (embedded driver).

    ``embedded_ledger_factory(presets=[...], id_prefix="77")`` returns the
    opened :class:`~tetramark.core.ledger.Ledger`.  The workspace is created
    once and reused; every call gets a fresh ledger name unless one is given.
    """
    counter = itertools.count(1)
    created: list[Ledger] = []

    def factory(
        presets: list[str | Path] | None = None,
        *,
        name: str | None = None,
        id_prefix: str | None = "77",
        author: str | None = FACTORY_AUTHOR,
        statuses: list[str] | None = None,
    ) -> Ledger:
        workspace_dir = tmp_path / "workspace"
        if (workspace_dir / "workspace.yaml").is_file():
            workspace = Workspace.load(workspace_dir, env=dolt_env)
        else:
            workspace = Workspace.create(
                workspace_dir, presets_dir=str(REPO_PRESETS), env=dolt_env
            )
        ledger_name = name or f"ledger{next(counter)}"
        sources = list(presets) if presets else [PRESETS / "base"]
        ledger = init_ledger(
            workspace,
            ledger_name,
            sources,
            id_prefix=id_prefix,
            author=author,
            statuses=statuses,
        )
        assert isinstance(ledger, Ledger)
        created.append(ledger)
        return ledger

    yield factory
    for ledger in created:
        ledger.close()


@pytest.fixture
def ledger_factory(
    embedded_ledger_factory: Callable[..., Ledger],
) -> Callable[..., Ledger]:
    """Alias kept for existing tests: the embedded factory (unchanged behaviour)."""
    return embedded_ledger_factory


@pytest.fixture(scope="session")
def _server_ledger_sequence() -> itertools.count:
    """Worker-wide counter so default database names never repeat."""
    return itertools.count(1)


@pytest.fixture(scope="session")
def server_workspace(
    tmp_path_factory: pytest.TempPathFactory, dolt_binary: str
) -> Iterator[Workspace]:
    """One ``dolt sql-server`` per xdist worker, in an isolated environment.

    ``Workspace.start()`` refuses an empty data dir, so the workspace contains
    a bare bootstrap ledger that tests never use.  Database names are
    ``t_<worker>_<n>``; leftovers from a crashed run are swept at start.
    """
    worker = worker_id()
    base = tmp_path_factory.mktemp(f"server-ws-{worker}")
    env = isolated_env(base / "dolt-root", base / "home")
    _configure_dolt_identity(dolt_binary, env)

    data_dir = base / "data"
    bootstrap = data_dir / "bootstrap"
    bootstrap.mkdir(parents=True)
    subprocess.run(
        [dolt_binary, "init"],
        cwd=bootstrap,
        env=env,
        capture_output=True,
        check=True,
    )

    workspace = Workspace.create(
        data_dir, port=free_port(), presets_dir=str(REPO_PRESETS), env=env
    )
    try:
        workspace.start(daemon=True)
    except Exception as error:  # noqa: BLE001 - server flakiness must not fail the suite
        pytest.skip(f"dolt sql-server unavailable ({workspace.data_dir}): {error}")

    prefix = f"t_{worker}_"
    try:
        for stale in database_names(workspace):
            if stale.startswith(prefix):
                drop_database(workspace, stale)
        yield workspace
    finally:
        workspace.stop()
        assert workspace.running_pid() is None, (
            f"dolt sql-server for {workspace.data_dir} is still running"
        )


@pytest.fixture
def server_ledger_factory(
    server_workspace: Workspace, _server_ledger_sequence: itertools.count
) -> Iterator[Callable[..., Ledger]]:
    """Like :func:`embedded_ledger_factory`, but inside the worker's server.

    Each call gets its own database (``CREATE DATABASE``); teardown closes the
    driver and drops every database the fixture created.
    """
    created: list[Ledger] = []

    def factory(
        presets: list[str | Path] | None = None,
        *,
        name: str | None = None,
        id_prefix: str | None = "77",
        author: str | None = FACTORY_AUTHOR,
        statuses: list[str] | None = None,
    ) -> Ledger:
        ledger_name = name or f"t_{worker_id()}_{next(_server_ledger_sequence)}"
        sources = list(presets) if presets else [PRESETS / "base"]
        ledger = init_ledger_on_server(
            server_workspace,
            ledger_name,
            sources,
            id_prefix=id_prefix,
            author=author,
            statuses=statuses,
        )
        created.append(ledger)
        return ledger

    yield factory
    for ledger in reversed(created):
        try:
            ledger.close()
        finally:
            drop_database(server_workspace, ledger.name)


@pytest.fixture(scope="session")
def template_workspaces(tmp_path_factory) -> dict[str, Path]:
    """Two initialised workspaces (base and full) to copy per test.

    ``dolt init`` plus the generated DDL dominates the runtime of tests that
    only need a fresh empty ledger; copying a closed database directory is
    safe and much faster.
    """
    root = tmp_path_factory.mktemp("ledger-templates")
    env = isolated_env(root / "dolt-root", root / "home")
    templates: dict[str, Path] = {}
    for key, preset in (("base", PRESETS / "base"), ("full", PRESETS / "full")):
        workspace_dir = root / key
        workspace = Workspace.create(
            workspace_dir, presets_dir=str(REPO_PRESETS), env=env
        )
        ledger = init_ledger(
            workspace,
            "template",
            [preset],
            id_prefix="77",
            author=FACTORY_AUTHOR,
        )
        ledger.close()
        templates[key] = workspace_dir
    return templates


@pytest.fixture
def fast_ledger_factory(
    tmp_path: Path,
    dolt_env: dict[str, str],
    template_workspaces: dict[str, Path],
) -> Iterator[Callable[..., Ledger]]:
    """Like :func:`ledger_factory`, but copies a pre-initialised template."""
    counter = itertools.count(1)
    created: list[Ledger] = []

    def factory(
        presets: list[str | Path] | None = None,
        *,
        name: str | None = None,
        id_prefix: str | None = "77",
        author: str | None = FACTORY_AUTHOR,
        statuses: list[str] | None = None,
    ) -> Ledger:
        del name, id_prefix, author, statuses  # fixed in the template
        sources = list(presets) if presets else [PRESETS / "base"]
        names = {Path(str(source)).name for source in sources}
        key = "base" if names == {"base"} else "full"
        target = tmp_path / f"workspace{next(counter)}"
        shutil.copytree(template_workspaces[key], target)
        workspace = Workspace.load(target, env=dolt_env)
        ledger = open_ledger(workspace, "template")
        created.append(ledger)
        return ledger

    yield factory
    for ledger in created:
        ledger.close()
