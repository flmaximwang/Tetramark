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


#: A plugin for the inner session: a 3 s fast-lane budget, and a controller
#: ``sessionfinish`` that takes 5 s.  A ``-p`` plugin is registered before
#: ``tests/conftest.py``, so pluggy calls its hook *after* an unmarked conftest
#: hook -- exactly like pytest's own tmp_path plugin, whose session-finish
#: cleanup deletes old ``pytest-N`` directories full of Dolt ledgers.  Under
#: load that cleanup took minutes while the guard had already stopped its
#: clock, so fast-lane runs of 86 s and 158 s exited 0 (GitHub issue #68).
SLOW_FINISH_PLUGIN = '''
import sys, time

def pytest_configure(config):
    for name, module in list(sys.modules.items()):
        if name.endswith("conftest") and hasattr(module, "FAST_SUITE_BUDGET_SECONDS"):
            module.FAST_SUITE_BUDGET_SECONDS = 3.0

def pytest_sessionfinish(session):
    if not hasattr(session.config, "workerinput"):
        time.sleep(5)
'''


# No xdist: the plugin order is the same on a plain session (verified: the
# pre-fix conftest exits 0 either way), and an inner worker was the slowest
# test of a busy outer suite (54 s).  5 s of session finish.
@pytest.mark.timeout(120)
def test_the_fast_lane_budget_counts_the_whole_session_finish(tmp_path: Path) -> None:
    """``-m fast`` over its budget fails even when the time is spent finishing."""
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    (plugins / "slow_finish.py").write_text(SLOW_FINISH_PLUGIN, encoding="utf-8")
    env = os.environ.copy()
    env.pop("TETRAMARK_SUITE_BUDGET_SECONDS", None)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(plugins), str(ROOT / "src")]
        + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-m",
            "fast",
            "-n",
            "0",
            "-p",
            "slow_finish",
            "-p",
            "no:cacheprovider",
            str(ROOT / "tests" / "test_ids.py"),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=110,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "SUITE TIME BUDGET EXCEEDED" in output
    assert "> 3s" in output
