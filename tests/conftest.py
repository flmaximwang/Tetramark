"""Shared pytest fixtures.

Tests run against the real ``dolt`` binary (never a mock). Every fixture that
shells out points ``DOLT_ROOT_PATH``, ``HOME``, ``XDG_CONFIG_HOME`` and the Git
config env vars at the pytest ``tmp_path`` so the developer's global Dolt/Git
configuration is never read or written.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

DOLT_BINARY = shutil.which("dolt")

#: ``subprocess.run`` signature use in fixtures/tests: run_dolt("sql", "-q", "SELECT 1")
RunDolt = Callable[..., subprocess.CompletedProcess[str]]


@pytest.fixture(scope="session")
def dolt_binary() -> str:
    """Path to the real ``dolt`` binary; skips the test if it is not installed."""
    if DOLT_BINARY is None:
        pytest.skip("the 'dolt' binary is not installed or not on PATH")
    return DOLT_BINARY


@pytest.fixture
def dolt_env(tmp_path: Path) -> dict[str, str]:
    """An environment whose global Dolt/Git config lives under ``tmp_path``."""
    root = tmp_path / "dolt-root"
    home = tmp_path / "home"
    root.mkdir()
    home.mkdir()
    env = os.environ.copy()
    env["DOLT_ROOT_PATH"] = str(root)
    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    env["GIT_CONFIG_GLOBAL"] = str(home / ".gitconfig")
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    env.pop("GIT_CONFIG_NOSYSTEM", None)
    return env


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
def dolt_repo(tmp_path: Path, dolt_run: RunDolt) -> Iterator[Path]:
    """An initialised Dolt data repository with an isolated global config.

    ``dolt config --global`` writes into ``$DOLT_ROOT_PATH`` (a tmp directory),
    so the user's real global config is untouched.
    """
    dolt_run("config", "--global", "--add", "user.name", "DoltLedger Tests")
    dolt_run("config", "--global", "--add", "user.email", "tests@doltledger.invalid")
    repo = tmp_path / "repo"
    repo.mkdir()
    dolt_run("init", cwd=repo)
    assert (repo / ".dolt").is_dir(), "dolt init did not create .dolt"
    yield repo
