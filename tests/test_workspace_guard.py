"""The test lane must not build the removed workspace layer (P3/P4/P7).

0.5.0 removed ``workspace.yaml`` entirely: a ledger is one self-contained
directory.  The only tests allowed to **write** a ``workspace.yaml`` are the
migration-guard tests, which construct a 0.4 layout on purpose to prove the P4
error (``cd`` into the ledger) and P7 behaviour (a parent workspace file is
ignored).  Everything else would silently resurrect the old fixture layer.

Dolt-free: the whole module runs in the fast lane.  The scan is textual on the
test modules (``tests/*.py``); the fixture builders under ``tests/data/`` are
not collected and drive *pinned legacy checkouts* whose own API predates 0.5.0.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.fast

TESTS = Path(__file__).resolve().parent

#: The only modules allowed to write ``workspace.yaml``: they test the P4/P7
#: migration guards, not the removed workspace fixture layer.
MIGRATION_GUARD_TESTS = frozenset(
    {
        "test_ledger_resolution.py",  # P4: workspace.yaml before any ledger
        "test_cli_ledger_resolution.py",  # P4 through the CLI
        "test_migrate.py",  # P7: open_target ignores a parent workspace.yaml
    }
)

#: The only modules allowed to name ``$TETRAMARK_WORKSPACE``: the env guard in
#: conftest (no ledger may be selected by an env var, P3) and the resolver test
#: that proves a workspace env var is ignored.
WORKSPACE_ENV_TESTS = frozenset({"conftest.py", "test_ledger_resolution.py"})


def _test_modules() -> list[Path]:
    """Every test module except this guard (its own text names the patterns)."""
    return [
        path
        for path in sorted(TESTS.glob("*.py"))
        if path.name != Path(__file__).name
    ]


def test_no_test_builds_the_removed_workspace_class() -> None:
    """No test imports or constructs the deleted ``Workspace`` API."""
    offenders = [
        path.name
        for path in _test_modules()
        if "tetramark.driver.workspace" in path.read_text(encoding="utf-8")
        or "Workspace.create" in path.read_text(encoding="utf-8")
        or "Workspace.load" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_only_the_migration_guard_tests_write_workspace_yaml() -> None:
    """A 0.4 layout may only be built to test the P4/P7 guards."""
    writers = [
        path.name
        for path in _test_modules()
        if '"workspace.yaml").write_text' in path.read_text(encoding="utf-8")
        or "workspace.yaml').write_text" in path.read_text(encoding="utf-8")
        or "WORKSPACE_FILE).write_text" in path.read_text(encoding="utf-8")
    ]
    assert writers, "the P4/P7 guard tests disappeared; is the guard still tested?"
    assert set(writers) == MIGRATION_GUARD_TESTS, (
        "only the P4/P7 migration-guard tests may create workspace.yaml; "
        f"offenders: {sorted(set(writers) - MIGRATION_GUARD_TESTS)}"
    )


def test_no_test_runs_the_removed_workspace_command() -> None:
    """No test invokes ``tm workspace ...``: the command group is gone (P3)."""
    command = re.compile(r"['\"]workspace['\"]\s*,")
    offenders = [
        path.name
        for path in _test_modules()
        if command.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_only_the_documented_tests_mention_the_workspace_env_var() -> None:
    """``$TETRAMARK_WORKSPACE`` exists only as a proven-ignored legacy name."""
    mentions = {
        path.name
        for path in _test_modules()
        if "TETRAMARK_WORKSPACE" in path.read_text(encoding="utf-8")
    }
    assert mentions == WORKSPACE_ENV_TESTS


def test_conftest_has_no_workspace_fixture() -> None:
    """The shared fixtures build standalone ledgers only (P3)."""
    text = (TESTS / "conftest.py").read_text(encoding="utf-8")
    assert "Workspace" not in text
    assert "workspace.yaml" not in text
    assert not re.search(r"^def .*workspace", text, re.MULTILINE | re.IGNORECASE)
