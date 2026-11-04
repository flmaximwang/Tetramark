"""The suite's own wall-clock guard is part of the test contract.

``tests/conftest.py`` fails a session that runs past its budget; these tests run
a tiny inner pytest session with an impossible budget and check the exit code
and the report.  They are Dolt-free and marked fast.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.fast

ROOT = Path(__file__).resolve().parents[1]


def inner_pytest(tmp_path: Path, **overrides: str) -> subprocess.CompletedProcess[str]:
    """Run two fast tests in a fresh pytest session with a controlled budget."""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    env.update(overrides)
    # test_ids is Dolt-free and tiny: the point is the session guard, not the
    # tests.  No xdist: an inner pytest run under ten busy outer workers is the
    # flakiest thing in this suite, and the guard hooks are the same code on a
    # plain session.
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-n",
            "0",
            "-p",
            "no:cacheprovider",
            str(ROOT / "tests" / "test_ids.py"),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


# The inner pytest process can queue behind the outer suite's workers; 60 s is
# enough for it alone but not under load.
@pytest.mark.timeout(120)
def test_a_session_over_its_budget_fails(tmp_path: Path) -> None:
    """Exit non-zero when the wall clock passes the budget, and say why."""
    result = inner_pytest(tmp_path, TETRAMARK_SUITE_BUDGET_SECONDS="0.001")

    assert result.returncode != 0, result.stdout + result.stderr
    output = result.stdout + result.stderr
    assert "SUITE TIME BUDGET EXCEEDED" in output
    assert "TETRAMARK_SUITE_BUDGET_SECONDS" in output
    assert "slowest" in output.lower() or "s  tests/" in output


@pytest.mark.timeout(120)
def test_an_empty_budget_disables_the_guard(tmp_path: Path) -> None:
    """The documented escape hatch for profiling really disables the guard."""
    result = inner_pytest(tmp_path, TETRAMARK_SUITE_BUDGET_SECONDS="")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "SUITE TIME BUDGET EXCEEDED" not in result.stdout + result.stderr
