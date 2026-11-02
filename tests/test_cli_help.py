"""``-h`` is ``--help`` everywhere, and asking for help succeeds (Tetramark-03j).

The command tree is walked once from the real Typer root, so every command and
command group is covered as soon as it is registered -- a new command needs no
edit here.  Dolt-free: the whole module runs in the fast lane.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import typer.main
from typer.testing import CliRunner

from tetramark.cli.app import app
from tetramark.errors import EXIT_OK, EXIT_USAGE

pytestmark = pytest.mark.fast

runner = CliRunner()


def _tree() -> Iterator[tuple[tuple[str, ...], Any]]:
    """Every command path under the root, the root itself included."""
    stack: list[tuple[tuple[str, ...], Any]] = [((), typer.main.get_command(app))]
    found: list[tuple[tuple[str, ...], Any]] = []
    while stack:
        path, command = stack.pop()
        found.append((path, command))
        for name, child in getattr(command, "commands", {}).items():
            stack.append((path + (name,), child))
    yield from sorted(found, key=lambda item: item[0])


TREE = list(_tree())
PATHS = [path for path, _ in TREE]
GROUP_PATHS = [path for path, command in TREE if getattr(command, "commands", None)]


def _id(path: tuple[str, ...]) -> str:
    return " ".join(path) or "tm"


def test_the_walk_sees_groups_and_leaves() -> None:
    """Guard the parametrisation itself: a broken walk would test nothing."""
    assert () in PATHS
    assert len(GROUP_PATHS) > 1
    assert len(PATHS) > len(GROUP_PATHS)


@pytest.mark.parametrize("path", PATHS, ids=_id)
def test_dash_h_matches_dash_dash_help(path: tuple[str, ...]) -> None:
    short = runner.invoke(app, [*path, "-h"])
    long = runner.invoke(app, [*path, "--help"])
    assert short.exit_code == EXIT_OK, f"tm {_id(path)} -h exited {short.exit_code}"
    assert long.exit_code == EXIT_OK
    assert short.output == long.output
    assert "Usage:" in short.output


@pytest.mark.parametrize("path", GROUP_PATHS, ids=_id)
def test_bare_invocation_prints_help_and_succeeds(path: tuple[str, ...]) -> None:
    bare = runner.invoke(app, list(path))
    assert bare.exit_code == EXIT_OK, f"bare 'tm {_id(path)}' exited {bare.exit_code}"
    assert bare.output == runner.invoke(app, [*path, "-h"]).output


def test_unknown_option_is_still_a_usage_error() -> None:
    result = runner.invoke(app, ["--no-such-option"])
    assert result.exit_code == EXIT_USAGE


def test_unknown_command_is_still_a_usage_error() -> None:
    result = runner.invoke(app, ["no-such-command"])
    assert result.exit_code == EXIT_USAGE


def test_missing_argument_is_still_a_usage_error() -> None:
    result = runner.invoke(app, ["init"])
    assert result.exit_code == EXIT_USAGE


def test_version_still_wins_over_a_bare_invocation() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == EXIT_OK
    assert result.output.startswith("tetramark ")
