"""P6/P7: the commit author is ``ledger.yaml`` -> Git config; no workspace tier.

Fast lane: none of these paths opens a database.  ``init_ledger(dry_run=True)``
builds the plan (``ledger.yaml`` plus schema) and returns before the first
``dolt`` call, so the resolution order and the no-author failure are exercised
without Dolt.

The P7 side is the ``tm config`` surface: after the workspace layer was removed
the command exposes exactly one key, ``author``; the workspace-only settings
are rejected with the ordinary unknown-key error.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tetramark import init_ledger
from tetramark.cli.config import _KEYS, _check_key
from tetramark.core.ledger import InitPlan, resolve_author
from tetramark.errors import UsageError

from conftest import PRESETS

pytestmark = pytest.mark.fast

GIT = shutil.which("git")

LEDGER_AUTHOR = "Ledger Author <ledger@example.invalid>"
GIT_AUTHOR = "Git User <git@example.invalid>"


def git_env(home: Path) -> dict[str, str]:
    """An environment whose Git config is isolated under ``home``."""
    home.mkdir(parents=True, exist_ok=True)
    return {
        **os.environ,
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "GIT_CONFIG_GLOBAL": str(home / ".gitconfig"),
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }


def make_git_repo(
    path: Path, *, name: str | None = None, email: str | None = None
) -> dict[str, str]:
    """A Git repository with an optional local identity; its isolated env."""
    if GIT is None:  # pragma: no cover - git is a suite prerequisite
        pytest.skip("git is not installed")
    env = git_env(path.parent / f"{path.name}-home")
    path.mkdir(parents=True)
    subprocess.run([GIT, "init", "--quiet", str(path)], env=env, check=True)
    if name is not None:
        subprocess.run(
            [GIT, "config", "user.name", name], cwd=path, env=env, check=True
        )
    if email is not None:
        subprocess.run(
            [GIT, "config", "user.email", email], cwd=path, env=env, check=True
        )
    return env


# ---------------------------------------------------------------------------
# P6: ledger.yaml wins, Git config is the only fallback
# ---------------------------------------------------------------------------


def test_ledger_yaml_author_wins_over_git_config(tmp_path: Path) -> None:
    """A non-empty ``author:`` in ledger.yaml is used verbatim (stripped)."""
    repo = tmp_path / "repo"
    env = make_git_repo(repo, name="Git User", email="git@example.invalid")
    ledger_dir = repo / "ledger"

    plan = init_ledger(
        ledger_dir,
        presets=[PRESETS / "base"],
        author=f"  {LEDGER_AUTHOR}  ",
        dry_run=True,
        env=env,
    )

    assert isinstance(plan, InitPlan)
    assert plan.author == LEDGER_AUTHOR
    assert plan.config.author == LEDGER_AUTHOR
    assert f"author: {LEDGER_AUTHOR}" in plan.ledger_yaml
    assert not ledger_dir.exists(), "a dry-run plan writes nothing"


def test_blank_ledger_author_falls_back_to_git_config(tmp_path: Path) -> None:
    """An empty/whitespace ``author:`` is not an author: Git config fills it."""
    repo = tmp_path / "repo"
    env = make_git_repo(repo, name="Git User", email="git@example.invalid")

    assert resolve_author("   ", cwd=repo, env=env) == GIT_AUTHOR
    plan = init_ledger(
        repo / "ledger",
        presets=[PRESETS / "base"],
        author="   ",
        dry_run=True,
        env=env,
    )
    assert plan.author == GIT_AUTHOR


def test_missing_author_falls_back_to_git_config(tmp_path: Path) -> None:
    """With no ``author:`` at all, the repository's Git identity is the author."""
    repo = tmp_path / "repo"
    env = make_git_repo(repo, name="Git User", email="git@example.invalid")
    ledger_dir = repo / "ledger"

    plan = init_ledger(
        ledger_dir, presets=[PRESETS / "base"], dry_run=True, env=env
    )

    assert plan.author == GIT_AUTHOR
    assert f"author: {GIT_AUTHOR}" in plan.ledger_yaml


def test_partial_author_takes_the_missing_part_from_git_config(
    tmp_path: Path,
) -> None:
    """``Name`` alone is kept; the Git email completes it."""
    repo = tmp_path / "repo"
    env = make_git_repo(repo, name="Git User", email="git@example.invalid")

    plan = init_ledger(
        repo / "ledger",
        presets=[PRESETS / "base"],
        author="Only A Name",
        dry_run=True,
        env=env,
    )

    assert plan.author == "Only A Name <git@example.invalid>"


def test_no_author_anywhere_is_an_actionable_usage_error(tmp_path: Path) -> None:
    """The failure points at ``--author`` and git config, never workspace.yaml."""
    env = git_env(tmp_path / "home")  # isolated: name and email are both unset
    ledger_dir = tmp_path / "ledger"

    with pytest.raises(UsageError) as caught:
        init_ledger(
            ledger_dir, presets=[PRESETS / "base"], dry_run=True, env=env
        )

    message = str(caught.value)
    assert "--author" in message
    assert "git user.name/user.email" in message
    assert "workspace" not in message.lower()
    assert not ledger_dir.exists(), "the author check fails before any write"


# ---------------------------------------------------------------------------
# P7: ``tm config`` exposes author only
# ---------------------------------------------------------------------------


def test_tm_config_exposes_only_author() -> None:
    assert _KEYS == ("author",)


@pytest.mark.parametrize("removed", ["host", "port", "default_ledger", "presets_dir"])
def test_tm_config_rejects_removed_workspace_settings(removed: str) -> None:
    """The settings that only existed in workspace.yaml have no reader left."""
    with pytest.raises(UsageError, match="unknown configuration key"):
        _check_key(removed)


def test_tm_config_accepts_the_author_key() -> None:
    assert _check_key(" Author ") == "author"
