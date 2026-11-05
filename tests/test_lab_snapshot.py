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

The second test walks that same ledger through the upgrade chain: the manual
spec 5 -> 6 step when the copy is still spec 5 (the helper from
``tests/test_fixed_status_model.py``), then ``migration/spec/6-to-7/migrate.py
--apply``, and asserts the spec-7 shape, the rename/fold and a green ``tm
check``.  The input must be the spec-5 copy: a spec-6 directory fails with a
clear message instead of silently skipping the manual half of the chain.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import warnings
from pathlib import Path

import pytest

from tetramark.core.ledger import open as open_ledger
from tetramark.schema.diff import apply, plan

#: The reference ledger's directory name (also its ``ledger.yaml`` identity).
SNAPSHOT_LEDGER = "zsqlab_10_gammaPFD-Fiber_2026.06.25"

#: The repository/worktree root (tests/ -> root).
ROOT = Path(__file__).resolve().parents[1]


def is_ledger(directory: Path) -> bool:
    """Whether ``directory`` is a ledger: a Dolt database plus ``ledger.yaml``."""
    return (directory / ".dolt").is_dir() and (directory / "ledger.yaml").is_file()


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
        if is_ledger(candidate):
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


#: The upgrade step and the counter its commit message names.
SPEC_6_TO_7 = ROOT / "migration" / "spec" / "6-to-7" / "migrate.py"


def manual_spec5_to_6(ledger, env: dict[str, str]):
    """The manual spec 5 -> 6 README, as ``test_fixed_status_model`` runs it.

    The helper there rewrites the stored preset copies in place (no status
    vocabulary), adds the fixed-status ``CHECK`` and lets ``tm schema apply``
    write the counter, the fingerprint and the regenerated ``dolt_tests`` in
    one commit.  The reference ledger only carries statuses of the fixed four,
    so the old-vocabulary remap table has nothing to do here; a row outside the
    four would need a human decision and is refused instead of guessed.
    """
    from test_fixed_status_model import MIGRATION_CHECK, rewrite_preset_to_fixed
    from tetramark.schema.model import FIXED_STATUSES, StatusModel

    leftover = [
        str(row["status"])
        for row in ledger.driver.query("SELECT DISTINCT `status` FROM `aliquot`")
        if str(row["status"]) not in FIXED_STATUSES
    ]
    assert leftover == [], (
        "the snapshot's old vocabulary needs the 5-to-6 remap table by hand: "
        f"{leftover}"
    )
    checks = {
        str(row["name"])
        for row in ledger.driver.query(
            "SELECT `constraint_name` AS `name` FROM "
            "`information_schema`.`table_constraints` WHERE `table_schema` = "
            "DATABASE() AND `table_name` = 'aliquot' AND "
            "`constraint_type` = 'CHECK'"
        )
    }
    if "ck_aliquot_status" not in checks:
        ledger.driver.execute(MIGRATION_CHECK)
        ledger.driver.commit("test: manual 5-to-6 CHECK")
    path = ledger.path
    ledger.close()

    # The helper rewrites the stored preset files, which are not Dolt content,
    # so it only needs the (closed) ledger's path.
    rewrite_preset_to_fixed(ledger)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        reopened = open_ledger(path, env=env)
    try:
        assert reopened.schema.status_model is StatusModel.fixed
        current = plan(reopened)
        assert current.steps == [], [step.action for step in current.steps]
        assert apply(reopened).applied is True
    finally:
        reopened.close()

    # ``apply`` runs with the *current* tool, so it writes the current
    # ``SPEC_VERSION`` (7).  The manual 5 -> 6 step was run by a 0.8.x spec-6
    # tool, so stamp the counter and fingerprint that tool would have written;
    # the 6 -> 7 step then sees exactly the spec-6 ledger the chain produces.
    import tetramark.schema.model as model

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        stamped = open_ledger(path, env=env)
    try:
        saved = model.SPEC_VERSION
        model.SPEC_VERSION = 6
        try:
            spec6 = stamped.schema.fingerprint()
        finally:
            model.SPEC_VERSION = saved
        stamped.driver.execute(
            "UPDATE `ledger_meta` SET `value` = '6' "
            "WHERE `key` = 'spec_version'"
        )
        stamped.driver.execute(
            "UPDATE `ledger_meta` SET `value` = '0.8.7' "
            "WHERE `key` = 'tetramark_version'"
        )
        stamped.driver.execute(
            "UPDATE `ledger_meta` SET `value` = %s WHERE `key` = 'fingerprint'",
            (spec6,),
        )
        stamped.driver.commit("test: stamp the spec-6 counter the 5-to-6 tool wrote")
    finally:
        stamped.close()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        return open_ledger(path, env=env)


@pytest.mark.embedded
def test_the_real_lab_ledger_upgrades_to_spec_7(
    tmp_path: Path, dolt_env: dict[str, str]
) -> None:
    """The real spec-5 ledger runs the manual 5 -> 6 step, then 6 -> 7."""
    snapshot = lab_snapshot()
    if snapshot is None:
        pytest.skip("the 0.4.2 lab-ledger snapshot is not available in this checkout")

    ledger_dir = tmp_path / SNAPSHOT_LEDGER
    shutil.copytree(snapshot, ledger_dir)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        ledger = open_ledger(ledger_dir, env=dolt_env)
    try:
        # The bead's test plan: copy the 0.4.2 (spec 5) snapshot, run the
        # manual 5 -> 6 steps, then the scripted 6 -> 7 step.  A spec-6
        # directory is the wrong input; failing with this message is better
        # than silently skipping the first half of the chain.
        stored = int(ledger.meta["spec_version"])
        assert stored == 5, (
            "TETRAMARK_LAB_SNAPSHOT must point at the 0.4.2/spec-5 ledger "
            f"directory (got spec {stored})"
        )
        ledger = manual_spec5_to_6(ledger, dolt_env)
        assert ledger.meta["spec_version"] == "6"
        assert "purpose" in {
            str(row["name"])
            for row in ledger.driver.query(
                "SELECT `column_name` AS `name` FROM `information_schema`.`columns` "
                "WHERE `table_schema` = DATABASE() "
                "AND `table_name` = 'aliquot_event'"
            )
        }

        # The event text the spec-6 ledger holds, and the shape the step must
        # preserve: no note/purpose row may lose its text in the fold.
        notes = [
            str(row["message"])
            for row in ledger.driver.query(
                "SELECT `note` AS `message` FROM `aliquot_event` "
                "WHERE `note` IS NOT NULL AND TRIM(`note`) <> '' ORDER BY `id`"
            )
        ]
        event_count = int(
            ledger.driver.query("SELECT COUNT(*) AS `n` FROM `aliquot_event`")[0]["n"]
        )
        head_before = str(
            ledger.driver.query("SELECT DOLT_HASHOF('HEAD') AS `hash`")[0]["hash"]
        )
        commits_before = int(
            ledger.driver.query("SELECT COUNT(*) AS `n` FROM `dolt_log`")[0]["n"]
        )
    finally:
        ledger.close()

    result = subprocess.run(
        [
            sys.executable,
            str(SPEC_6_TO_7),
            "--ledger",
            str(ledger_dir),
            "--apply",
        ],
        cwd=str(ROOT),
        env={**os.environ, **dolt_env},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "purpose folded into message:" in result.stdout
    assert "migrate(ledger): spec 6 -> 7" in result.stdout

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        migrated = open_ledger(ledger_dir, env=dolt_env)
    try:
        assert migrated.meta["spec_version"] == "7"
        assert migrated.meta["fingerprint"] == migrated.schema.fingerprint()
        columns = {
            str(row["name"])
            for row in migrated.driver.query(
                "SELECT `column_name` AS `name` FROM `information_schema`.`columns` "
                "WHERE `table_schema` = DATABASE() "
                "AND `table_name` = 'aliquot_event'"
            )
        }
        assert "message" in columns
        assert "note" not in columns and "purpose" not in columns

        # The rename kept every event and its text; the fold only adds the
        # non-empty purposes (none in this ledger).
        assert (
            int(
                migrated.driver.query(
                    "SELECT COUNT(*) AS `n` FROM `aliquot_event`"
                )[0]["n"]
            )
            == event_count
        )
        messages = [
            str(row["message"])
            for row in migrated.driver.query(
                "SELECT `message` FROM `aliquot_event` "
                "WHERE `message` IS NOT NULL AND TRIM(`message`) <> '' ORDER BY `id`"
            )
        ]
        assert messages == notes

        # Exactly one migration commit on top of the spec-6 head, and the tag.
        head_after = str(
            migrated.driver.query("SELECT DOLT_HASHOF('HEAD') AS `hash`")[0]["hash"]
        )
        assert head_after != head_before
        assert (
            int(migrated.driver.query("SELECT COUNT(*) AS `n` FROM `dolt_log`")[0]["n"])
            == commits_before + 1
        )
        assert str(
            migrated.driver.query(
                "SELECT `message` FROM `dolt_log` WHERE `commit_hash` = %s",
                (head_after,),
            )[0]["message"]
        ).strip() == "migrate(ledger): spec 6 -> 7"
        tags = {
            str(row["tag_name"]): str(row["tag_hash"])
            for row in migrated.driver.query(
                "SELECT `tag_name`, `tag_hash` FROM `dolt_tags`"
            )
        }
        assert tags.get("pre-migrate-spec-6") == head_before

        report = migrated.check()
        assert report.passed, [row.name for row in report.blocking_failures]
        assert not report.blocking_failures
    finally:
        migrated.close()
