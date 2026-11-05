"""The version lives in five files; a release is only as correct as the weakest.

`pyproject.toml`, `src/tetramark/__init__.py`, `uv.lock` and the frontmatter of
both shipped skills (`skills/tetramark/SKILL.md`,
`skills/tetramark-schema/SKILL.md`) must carry the same version. Bumping some of
them ships a wheel that calls itself 0.5.1 while `tm --version` says 0.5.0, or a
skill that still describes the previous release, and the release workflow then
refuses to tag.

These tests run the same script the workflow runs
(``scripts/release_check.py``) instead of re-implementing the comparison, so a
green fast lane means the release step will not be blocked by a mismatch.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
CHECK = ROOT / "scripts" / "release_check.py"


def report(*args: str) -> dict[str, Any]:
    # --offline keeps the fast lane off the network: the remote probe is a
    # `git ls-remote`, and four of them added seconds to a four-second lane.
    result = subprocess.run(
        [sys.executable, str(CHECK), "--json", "--offline", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode in (0, 1), result.stderr
    return json.loads(result.stdout)


@pytest.fixture(scope="module")
def current() -> dict[str, Any]:
    return report()


@pytest.mark.fast
def test_the_version_sources_agree(current: dict[str, Any]) -> None:
    assert current["problems"] == []
    assert current["ready"] is True


@pytest.mark.fast
def test_the_tag_name_follows_the_version(current: dict[str, Any]) -> None:
    assert current["version"]
    assert current["tag"] == f"v{current['version']}"
    assert set(current["sources"]) == {
        "pyproject",
        "init",
        "lock",
        "skill",
        "skill_schema",
    }


@pytest.mark.fast
def test_since_reports_an_unchanged_version(current: dict[str, Any]) -> None:
    """The workflow only tags a push that actually moved the version."""
    unchanged = report("--since", "HEAD")
    assert unchanged["version_before"] == current["version"]
    assert unchanged["version_changed"] is False


@pytest.mark.fast
def test_an_unknown_base_commit_does_not_silently_skip_a_release() -> None:
    """`None` means "cannot tell"; the workflow treats that as changed."""
    unknown = report("--since", "0" * 40)
    assert unknown["version_before"] is None
    assert unknown["version_changed"] is None
    assert unknown["ready"] is True
