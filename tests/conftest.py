"""Shared pytest fixtures.

Tests run against the real ``dolt`` binary (never a mock). Every fixture that
shells out points ``DOLT_ROOT_PATH``, ``HOME``, ``XDG_CONFIG_HOME`` and the Git
config env vars at the pytest ``tmp_path`` so the developer's global Dolt/Git
configuration is never read or written.

Two lanes:

* ``uv run pytest -q -m fast`` — the Dolt-free unit tests only; seconds, no
  ``dolt`` binary needed. Run this while iterating.
* ``uv run pytest -q -n 4`` — the full suite, every integration test against a
  real database. Run this before merging or releasing.

The full suite is still real-Dolt end to end: ``ledger_factory``,
``embedded_ledger_factory`` and ``server_ledger_factory`` hand out layouts of a
*session template* (one real ``init_ledger``/``init_ledger_on_server`` per
standard shape, then copies) so a fresh ledger costs a directory copy instead of
a full ``tm init``. Anything that changes what ``init`` produces — a custom
preset, several presets, another ``id_prefix``/``author`` — keeps
the real init path; see :data:`TEMPLATE_SHAPES`.
"""

from __future__ import annotations

import itertools
import os
import shutil
import socket
import subprocess
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, suppress
from pathlib import Path

import pymysql
import pytest
import yaml

from tetramark.core.ledger import (
    LEDGER_FILE,
    Ledger,
    init_ledger,
    init_ledger_on_server,
    open as open_ledger,
)
from tetramark.driver.workspace import Workspace
from tetramark.errors import LedgerError
from tetramark.schema.loader import resolve_preset_spec

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
    dolt_env: dict[str, str],
    tmp_path: Path,
    template_ledgers: LedgerTemplates,
) -> Iterator[Callable[..., Ledger]]:
    """Create real ledgers in a ``tmp_path`` workspace (embedded driver).

    ``embedded_ledger_factory(presets=[...], id_prefix="77")`` returns the
    opened :class:`~tetramark.core.ledger.Ledger`.  The workspace is created
    once and reused; every call gets a fresh ledger name unless one is given.

    A standard shape with the default options (see :data:`TEMPLATE_SHAPES`,
    :func:`template_matches_options`) is copied from the session template; any
    other call runs the real ``init_ledger``.  ``real_init=True`` forces the
    latter for tests that exist to assert what init generates.
    """
    counter = itertools.count(1)
    created: list[Ledger] = []

    def factory(
        presets: list[str | Path] | None = None,
        *,
        name: str | None = None,
        id_prefix: str | None = "77",
        author: str | None = FACTORY_AUTHOR,
        real_init: bool = False,
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
        key = None
        if not real_init and template_matches_options(
            id_prefix=id_prefix, author=author
        ):
            key = template_key(sources, workspace)
        if key is None:
            ledger = init_ledger(
                workspace,
                ledger_name,
                sources,
                id_prefix=id_prefix,
                author=author,
            )
        else:
            ledger = template_ledgers.install(key, workspace, ledger_name)
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
    server_workspace: Workspace,
    _server_ledger_sequence: itertools.count,
    template_ledgers: LedgerTemplates,
) -> Iterator[Callable[..., Ledger]]:
    """Like :func:`embedded_ledger_factory`, but inside the worker's server.

    Each call gets its own database; teardown closes the driver and drops every
    database the fixture created.  A standard shape with the default options is
    restored from the session template's Dolt backup (the server cannot pick up
    a directory copied underneath it); anything else runs the real
    ``init_ledger_on_server``.  ``real_init=True`` forces the latter.
    """
    created: list[Ledger] = []

    def factory(
        presets: list[str | Path] | None = None,
        *,
        name: str | None = None,
        id_prefix: str | None = "77",
        author: str | None = FACTORY_AUTHOR,
        real_init: bool = False,
    ) -> Ledger:
        ledger_name = name or f"t_{worker_id()}_{next(_server_ledger_sequence)}"
        sources = list(presets) if presets else [PRESETS / "base"]
        key = None
        if not real_init and template_matches_options(
            id_prefix=id_prefix, author=author
        ):
            key = template_key(sources, server_workspace)
        if key is None:
            ledger = init_ledger_on_server(
                server_workspace,
                ledger_name,
                sources,
                id_prefix=id_prefix,
                author=author,
            )
        else:
            ledger = template_ledgers.restore_into_server(
                key, server_workspace, ledger_name
            )
        created.append(ledger)
        return ledger

    yield factory
    for ledger in reversed(created):
        try:
            ledger.close()
        finally:
            drop_database(server_workspace, ledger.name)


# ---------------------------------------------------------------------------
# Session templates: one real init per standard shape, then copies
# ---------------------------------------------------------------------------

#: ``id_prefix`` and author every template is built with.  A factory call that
#: asks for anything else keeps the real init path: a template must be exactly
#: the ledger ``init_ledger`` would have produced for those options.
TEMPLATE_ID_PREFIX = "77"
TEMPLATE_LEDGER_NAME = "template"

#: The standard ledger shapes: built once per pytest session (per xdist worker)
#: and copied afterwards.  Values are *resolved* preset files, so the key
#: follows what ``tm init --preset`` would load, not how a test spelled it.
#: Anything else (a custom preset directory, several presets, a shape not
#: listed here) keeps the real ``init_ledger`` path — see :func:`template_key`.
TEMPLATE_SHAPES: dict[str, tuple[str, ...]] = {
    "presets-base": (str((PRESETS / "base").resolve()),),
    "presets-full": (str((PRESETS / "full").resolve()),),
    "preset-base": (str((REPO_PRESETS / "base.yaml").resolve()),),
}


def template_key(
    presets: Sequence[str | Path], workspace: Workspace
) -> str | None:
    """The template covering this preset list, or ``None`` when init must run.

    Resolution goes through :func:`~tetramark.schema.loader.resolve_preset_spec`
    with the workspace's ``presets_dir``, exactly like init does.  A bad preset
    returns ``None`` (never raises) so the real ``init_ledger`` stays the one
    that reports the error.
    """
    resolved: list[str] = []
    for preset in presets:
        try:
            found = resolve_preset_spec(
                str(preset),
                presets_dir=workspace.presets_dir_path,
                base_dir=Path.cwd(),
            )
        except Exception:  # noqa: BLE001 - init must raise the real error
            return None
        resolved.append(str(found.path.resolve()))
    for key, shape in TEMPLATE_SHAPES.items():
        if tuple(resolved) == shape:
            return key
    return None


def template_matches_options(
    *, id_prefix: str | None, author: str | None
) -> bool:
    """Whether the requested options are the ones the templates were built with."""
    return id_prefix == TEMPLATE_ID_PREFIX and author == FACTORY_AUTHOR


def _rewrite_ledger_name(ledger_dir: Path, name: str) -> None:
    """Point a copied ``ledger.yaml`` at the caller's ledger name.

    A real init writes ``name: <ledger>``; a copy carries the template's, so
    exactly that field is replaced (formatting and every other setting stay
    what init produced).
    """
    path = ledger_dir / LEDGER_FILE
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    payload["name"] = name
    path.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )


class LedgerTemplates:
    """One real ``init_ledger`` per standard shape, then copies of it.

    A template is built lazily in its own isolated workspace and is never
    written to again: :meth:`install` copies the closed database directory into
    the test's workspace (the ledgers are otherwise identical to a fresh init —
    same schema, ``ledger_meta``, generated ``dolt_tests``, same two commits).

    The server path cannot copy a directory into a running ``dolt sql-server``
    (it only discovers databases at startup), so :meth:`restore_into_server`
    exports the template to a Dolt backup once and restores it inside the
    server — the same real restore ``dolt backup restore`` performs.
    """

    def __init__(self, root: Path, env: dict[str, str]) -> None:
        self._root = root
        self._env = env
        self._ledgers: dict[str, Path] = {}
        self._backup_urls: dict[str, str] = {}

    def ledger(self, key: str) -> Path:
        """The template ledger directory for ``key``, built on first use."""
        if key not in self._ledgers:
            workspace = Workspace.create(
                self._root / key, presets_dir=str(REPO_PRESETS), env=self._env
            )
            ledger = init_ledger(
                workspace,
                TEMPLATE_LEDGER_NAME,
                list(TEMPLATE_SHAPES[key]),
                id_prefix=TEMPLATE_ID_PREFIX,
                author=FACTORY_AUTHOR,
            )
            ledger.close()
            self._ledgers[key] = workspace.ledger_path(TEMPLATE_LEDGER_NAME)
        return self._ledgers[key]

    def install(self, key: str, workspace: Workspace, name: str) -> Ledger:
        """Copy the template into ``workspace`` as ``name`` and open it."""
        target = workspace.ledger_path(name)
        if target.exists():
            raise LedgerError(f"ledger {name!r} already exists at {target}")
        shutil.copytree(self.ledger(key), target)
        _rewrite_ledger_name(target, name)
        return open_ledger(workspace, name)

    def backup_url(self, key: str) -> str:
        """A Dolt backup of the template, exported once per session."""
        if key not in self._backup_urls:
            self.ledger(key)
            # ``dolt_backup('add')`` records the backup inside the *source*
            # repository, so sync from a throwaway copy: the template stays
            # exactly what init produced (no invented `dolt_backups` rows).
            source = self._root / f"{key}-backup-source"
            shutil.copytree(self._ledgers[key], source)
            backup_url = (self._root / f"{key}-backup").as_uri()
            self._dolt_sql(source, f"CALL dolt_backup('add', 'template', '{backup_url}')")
            self._dolt_sql(source, "CALL dolt_backup('sync', 'template')")
            self._backup_urls[key] = backup_url
        return self._backup_urls[key]

    def restore_into_server(
        self, key: str, workspace: Workspace, name: str
    ) -> Ledger:
        """Restore the template database into ``workspace``'s running server."""
        target = workspace.ledger_path(name)
        if target.exists():
            raise LedgerError(f"ledger {name!r} already exists at {target}")
        backup_url = self.backup_url(key).replace("'", "''")
        quoted_name = "'" + str(name).replace("'", "''") + "'"
        restored = False
        try:
            with admin_connection(workspace) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        f"CALL dolt_backup('restore', '{backup_url}', {quoted_name})"
                    )
            restored = True
            # The database carries the schema and history; the ledger directory
            # also needs the self-contained ``ledger.yaml`` + ``presets/`` that
            # init writes next to it.
            target.mkdir(parents=True, exist_ok=True)
            for item in self.ledger(key).iterdir():
                if item.name == ".dolt":
                    continue
                destination = target / item.name
                if item.is_dir():
                    shutil.copytree(item, destination, dirs_exist_ok=True)
                else:
                    shutil.copy2(item, destination)
            _rewrite_ledger_name(target, name)
            return open_ledger(workspace, name)
        except BaseException:
            if restored:
                with suppress(Exception):
                    drop_database(workspace, name)
            shutil.rmtree(target, ignore_errors=True)
            raise

    def _dolt_sql(self, cwd: Path, sql: str) -> None:
        binary = DOLT_BINARY or shutil.which("dolt")
        process = subprocess.run(
            [binary, "sql", "-q", sql],
            cwd=cwd,
            env=self._env,
            capture_output=True,
            text=True,
        )
        output = "\n".join(
            part for part in (process.stdout.strip(), process.stderr.strip()) if part
        )
        if process.returncode != 0 or "error" in output.lower():
            raise LedgerError(f"template backup failed in {cwd}:\n{output}")


@pytest.fixture(scope="session")
def template_ledgers(tmp_path_factory: pytest.TempPathFactory) -> LedgerTemplates:
    """The session's pre-built ledgers, one per standard shape (lazily built)."""
    root = tmp_path_factory.mktemp("ledger-templates")
    return LedgerTemplates(root, isolated_env(root / "dolt-root", root / "home"))


@pytest.fixture
def fast_ledger_factory(
    embedded_ledger_factory: Callable[..., Ledger],
) -> Callable[..., Ledger]:
    """Alias of :func:`embedded_ledger_factory` (template-backed by default)."""
    return embedded_ledger_factory
