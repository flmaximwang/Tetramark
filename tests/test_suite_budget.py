"""The suite's own wall-clock guard is part of the test contract.

``tests/conftest.py`` fails a session that runs past its budget; these tests run
a tiny inner pytest session with an impossible budget and check the exit code
and the report.  They are Dolt-free and marked fast.

The inner session's clock is injected: the guard reads the module attribute
``tests/conftest.py::_suite_clock``, so the test can move that clock without
sleeping and without patching the global ``time`` -- the assertions are exact
and a loaded machine cannot turn them red.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
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


#: A plugin for the inner session: a 3 s fast-lane budget and an *injected*
#: clock.  The clock is a fake the plugin owns; it is installed on the inner
#: session's own ``tests/conftest.py`` module attribute (``_suite_clock``) and
#: nowhere else -- the global ``time.monotonic``/``time.sleep`` are never
#: patched, because pytest, xdist, PyMySQL and the Dolt supervision code read
#: real time.
#:
#: ``advance_late`` says where the fake clock jumps forward by 5 s:
#:
#: * ``False`` -- in a plain ``pytest_sessionfinish``.  A ``-p`` plugin is
#:   registered before ``tests/conftest.py``, so pluggy calls its hook *after*
#:   an unmarked conftest hook -- exactly like pytest's own tmp_path plugin,
#:   whose session-finish cleanup deletes old ``pytest-N`` directories full of
#:   Dolt ledgers.  Under load that cleanup took minutes while the guard had
#:   already stopped its clock, so fast-lane runs of 86 s and 158 s exited 0
#:   (GitHub issue #68).  The guard is ``trylast``, so it runs *after* this
#:   hook and sees the advanced clock.
#: * ``True`` -- in a ``hookwrapper`` ``pytest_sessionfinish``.  A wrapper's
#:   post-yield code runs after every non-wrapper implementation, so the clock
#:   moves after the conftest guard has already measured the session: the run
#:   must stay green.  That is the ordering negative control -- the test is not
#:   vacuously red.
def inner_clock_plugin(advance_late: bool = False) -> str:
    """The source of the inner plugin that fakes the session clock."""
    hook = (
        "@pytest.hookimpl(hookwrapper=True)\n"
        "def pytest_sessionfinish(session):\n"
        "    yield\n"
        "    _advance(session)\n"
        if advance_late
        else "def pytest_sessionfinish(session):\n    _advance(session)\n"
    )
    return f'''
import sys

import pytest

_CLOCK = None


class _FakeClock:
    """A clock the inner session owns; only the conftest attribute uses it."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def _advance(session):
    if hasattr(session.config, "workerinput"):
        return
    if _CLOCK is None:
        return
    _CLOCK.now += 5.0


def pytest_configure(config):
    global _CLOCK
    for name, module in list(sys.modules.items()):
        if name.endswith("conftest") and hasattr(module, "FAST_SUITE_BUDGET_SECONDS"):
            module.FAST_SUITE_BUDGET_SECONDS = 3.0
            clock = _FakeClock()
            module._suite_clock = clock
            # Re-stamp the start: conftest's own configure may have run
            # before or after this one, and the elapsed time must be the
            # fake clock's, never the real one.
            module._SUITE_STARTED = clock()
            _CLOCK = clock
            break


{hook}'''


def inner_budget_session(
    tmp_path: Path, plugin_name: str, plugin_source: str
) -> subprocess.CompletedProcess[str]:
    """Run the inner fast session with the given clock plugin installed."""
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    (plugins / f"{plugin_name}.py").write_text(plugin_source, encoding="utf-8")
    env = os.environ.copy()
    env.pop("TETRAMARK_SUITE_BUDGET_SECONDS", None)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(plugins), str(ROOT / "src")]
        + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
    )
    return subprocess.run(
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
            plugin_name,
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


def test_the_suite_clock_seam_defaults_to_the_real_clock() -> None:
    """The guard's clock is the real one unless a test replaces it."""
    import conftest

    assert conftest._suite_clock is time.monotonic


# No xdist: the plugin order is the same on a plain session (verified: the
# pre-fix conftest exits 0 either way), and an inner worker was the slowest
# test of a busy outer suite (54 s).  The session finish costs 5 fake seconds.
@pytest.mark.timeout(120)
def test_the_fast_lane_budget_counts_the_whole_session_finish(tmp_path: Path) -> None:
    """``-m fast`` over its budget fails even when the time is spent finishing."""
    result = inner_budget_session(
        tmp_path, "slow_finish", inner_clock_plugin(advance_late=False)
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "SUITE TIME BUDGET EXCEEDED: 5.0s > 3s" in output


@pytest.mark.timeout(120)
def test_a_clock_that_moves_after_the_guard_does_not_fail_the_run(
    tmp_path: Path,
) -> None:
    """The negative control: the same session stays green when the clock
    moves *after* the guard has read it.

    A regression test that cannot fail proves nothing, so the ordering that
    the test above relies on is pinned from the other side: the guard is
    ``trylast`` and therefore measures the session before a ``trylast``
    plugin hook can move the clock.
    """
    result = inner_budget_session(
        tmp_path, "late_finish", inner_clock_plugin(advance_late=True)
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "SUITE TIME BUDGET EXCEEDED" not in output

