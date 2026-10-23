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
from pathlib import Path

import pytest

from doltledger.core.ledger import Ledger, init_ledger, open as open_ledger
from doltledger.driver.workspace import Workspace

DOLT_BINARY = shutil.which("dolt")

#: The presets from ticket 03's test data.
PRESETS = Path(__file__).parent / "data" / "presets"

#: Author used by :func:`ledger_factory` so tests never read a global identity.
FACTORY_AUTHOR = "Ledger Factory <factory@doltledger.invalid>"


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
    env.pop("DOLTLEDGER_WORKSPACE", None)
    return env


def free_port() -> int:
    """An unused TCP port on 127.0.0.1."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


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
    dolt_run("config", "--global", "--add", "user.name", "DoltLedger Tests")
    dolt_run("config", "--global", "--add", "user.email", "tests@doltledger.invalid")


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
def ledger_factory(
    dolt_env: dict[str, str], tmp_path: Path
) -> Iterator[Callable[..., Ledger]]:
    """Create real ledgers in a ``tmp_path`` workspace (embedded driver).

    ``ledger_factory(presets=[...], id_prefix="77")`` returns the opened
    :class:`~doltledger.core.ledger.Ledger`.  The workspace is created once and
    reused; every call gets a fresh ledger name unless one is given.
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
            workspace = Workspace.create(workspace_dir, env=dolt_env)
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
        workspace = Workspace.create(workspace_dir, env=env)
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
