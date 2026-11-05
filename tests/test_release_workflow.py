"""The release workflow builds and attaches the distributions it promises.

A release is the only publishing path in this repository: there is no PyPI and
no CI, so the workflow file is the contract.  These tests read
``.github/workflows/release.yml`` as YAML and pin the pieces a release depends
on:

* the distributions are built from the tree being released, into the runner's
  temp dir (never into the checkout, where the next version check would see a
  dirty tree) and only when the release check said ``ready``;
* the build fails unless the artefacts carry the version being released, so a
  stale or renamed wheel can never be attached to the wrong tag;
* the upload runs after the release exists, is skipped for ``dry_run``, skips
  every asset the release already has and never passes ``--clobber`` -- a
  re-run or a ``workflow_dispatch`` backfill is idempotent.

The workflow itself is exercised by the full-lane ``test_installed_wheel.py``
(which builds a real wheel) and by the manual install check in the PR; these
tests are the fast, Dolt-free guard that the YAML still says all of that.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"

pytestmark = pytest.mark.fast

# Step names are the workflow's user-facing identifiers; the summary links to
# them, so renaming one is a release change in its own right.
RELEASE_STEP = "Tag the commit and publish the release"
BUILD_STEP = "Build the distributions"
ATTACH_STEP = "Attach the distributions to the release"


@pytest.fixture(scope="module")
def steps() -> list[dict[str, Any]]:
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return data["jobs"]["release"]["steps"]


def step_named(steps: list[dict[str, Any]], name: str) -> dict[str, Any]:
    matches = [step for step in steps if step.get("name") == name]
    assert len(matches) == 1, f"expected exactly one step named {name!r}, got {len(matches)}"
    return matches[0]


def step_index(steps: list[dict[str, Any]], name: str) -> int:
    names = [step.get("name") for step in steps]
    assert name in names, f"no step named {name!r}"
    return names.index(name)


def run_text(step: dict[str, Any]) -> str:
    return step.get("run", "") or ""


def commands(step: dict[str, Any]) -> str:
    """The step's ``run`` script without its comment lines."""
    return "\n".join(
        line for line in run_text(step).splitlines() if not line.lstrip().startswith("#")
    )


def test_the_build_writes_outside_the_checkout(steps: list[dict[str, Any]]) -> None:
    """A stray ``dist/`` in the checkout would make the next release check dirty."""
    text = run_text(step_named(steps, BUILD_STEP))
    assert "uv build" in text
    assert '--out-dir "$RUNNER_TEMP/dist"' in text
    assert "uv build --out-dir dist" not in text


def test_the_build_only_runs_when_the_release_check_passed(
    steps: list[dict[str, Any]],
) -> None:
    """A dry run still builds (it proves the tree builds); a failing gate does not."""
    assert "steps.check.outputs.ready == 'true'" in step_named(steps, BUILD_STEP)["if"]
    setup_uv = [
        step for step in steps if str(step.get("uses", "")).startswith("astral-sh/setup-uv")
    ]
    assert len(setup_uv) == 1
    assert setup_uv[0]["if"] == "steps.check.outputs.ready == 'true'"


def test_the_build_requires_the_versioned_filenames(steps: list[dict[str, Any]]) -> None:
    """A wheel whose name does not carry the release version must fail the build."""
    text = run_text(step_named(steps, BUILD_STEP))
    assert "tetramark-${VERSION}-py3-none-any.whl" in text
    assert "tetramark-${VERSION}.tar.gz" in text
    assert 'find "$out"' in text
    assert "exit 1" in text


def test_the_upload_comes_after_the_release(steps: list[dict[str, Any]]) -> None:
    """The release must exist before an asset can be attached to it."""
    release = step_index(steps, RELEASE_STEP)
    build = step_index(steps, BUILD_STEP)
    attach = step_index(steps, ATTACH_STEP)
    assert release < build < attach


def test_the_upload_is_guarded_by_release_state_and_dry_run(
    steps: list[dict[str, Any]],
) -> None:
    """Build on every ready tree; upload only for a real, version-moving release."""
    guard = step_named(steps, ATTACH_STEP)["if"]
    assert "steps.check.outputs.ready == 'true'" in guard
    assert "steps.check.outputs.changed == 'true'" in guard
    assert "inputs.dry_run != true" in guard


def test_no_step_clobbers_an_asset(steps: list[dict[str, Any]]) -> None:
    """Replacing a published asset silently would make the release lie."""
    for step in steps:
        assert "--clobber" not in commands(step), f"step {step.get('name')!r} clobbers"


def test_the_upload_skips_assets_that_already_exist(steps: list[dict[str, Any]]) -> None:
    """Read the assets back and only upload the names that are missing."""
    text = run_text(step_named(steps, ATTACH_STEP))
    assert 'gh release view "$TAG" --json assets' in text
    assert ".assets[].name" in text
    assert "grep -Fxq" in text
    assert "continue" in text
    assert 'gh release upload "$TAG"' in text


def test_the_summary_reports_the_assets(steps: list[dict[str, Any]]) -> None:
    """The run page names the two files the release now carries."""
    text = run_text(step_named(steps, "Summarise"))
    assert "| assets |" in text
    assert "py3-none-any.whl" in text
    assert ".tar.gz" in text
