"""Opening the real 0.4.2 lab ledger read-only (P1, P12).

The epic's reference ledger is
``zsqlab_10_gammaPFD-Fiber_2026.06.25``: spec 5, preset ``molbio``, author
``wangfanlin1 <wangfanlin2022@sibcb.ac.cn>`` and two absolute ``file://``
backup URLs in ``.dolt/repo_state.json``.  P1 promises that a 0.4.2 ledger
opens **unchanged**; P12 says sharing it means copying the directory.  The test
therefore copies the read-only snapshot (never writing the snapshot itself) and
opens the copy with the embedded driver.

The snapshot is not part of the repository (it is lab data under
``.dsh/tm-l8g/lab-snapshot/``), so the test skips cleanly when a checkout does
not carry it.  ``TETRAMARK_LAB_SNAPSHOT`` can point at it explicitly.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from tetramark.core.ledger import open as open_ledger

#: The reference ledger's directory name (also its ``ledger.yaml`` identity).
SNAPSHOT_LEDGER = "zsqlab_10_gammaPFD-Fiber_2026.06.25"

#: The repository/worktree root (tests/ -> root).
ROOT = Path(__file__).resolve().parents[1]


def lab_snapshot() -> Path | None:
    """The read-only 0.4.2 ledger, from an env override or a checkout root.

    A ticket worktree lives under ``<repo>/.agents/worktrees/ticket/<name>``
    while the snapshot lives in the main checkout's ``.dsh/``, so walking the
    parents finds it from both places.
    """
    override = os.environ.get("TETRAMARK_LAB_SNAPSHOT", "").strip()
    candidates = [Path(override).expanduser()] if override else []
    candidates += [
        parent / ".dsh" / "tm-l8g" / "lab-snapshot" / SNAPSHOT_LEDGER
        for parent in (ROOT, *ROOT.parents)
    ]
    for candidate in candidates:
        if (candidate / ".dolt").is_dir() and (candidate / "ledger.yaml").is_file():
            return candidate
    return None


@pytest.mark.embedded
def test_the_real_0_4_2_lab_ledger_opens_and_checks(
    tmp_path: Path, dolt_env: dict[str, str]
) -> None:
    """P1: a 0.4.2 ledger opens as-is; P12: a copied ledger is complete."""
    snapshot = lab_snapshot()
    if snapshot is None:
        pytest.skip("the 0.4.2 lab-ledger snapshot is not available in this checkout")

    # Copy the whole directory: that is what sharing a ledger means (P12).  The
    # snapshot itself stays untouched.
    ledger_dir = tmp_path / SNAPSHOT_LEDGER
    shutil.copytree(snapshot, ledger_dir)

    ledger = open_ledger(ledger_dir, env=dolt_env)
    try:
        assert ledger.name == SNAPSHOT_LEDGER
        assert ledger.config.presets == ["molbio"]
        assert ledger.meta["spec_version"] == "5"
        assert ledger.author == "wangfanlin1 <wangfanlin2022@sibcb.ac.cn>"
        assert ledger.ids["stock"].prefix == "10"
        # The ledger's own preset copy travels with the directory.
        assert (ledger.path / "presets" / "molbio.yaml").is_file()
        assert (ledger.path / "kinds").is_dir()
        assert (ledger.path / "rules").is_dir()

        # A 0.4.2 ledger is immediately checkable.  The lab's absolute
        # ``file://`` backup URLs make .10's portability rule fire, but it is
        # informational and never fails ``tm check`` (P13).
        report = ledger.check()
        assert report.passed, [row.name for row in report.blocking_failures]
        assert report.count >= 39
        info_failures = {row.name for row in report.info_failures}
        assert "portability_absolute_file_urls" in info_failures
        assert all(row.is_info for row in report.failures)
    finally:
        ledger.close()
