"""``migration/`` steps and the shared runner (tickets 21-22, spec §5).

Every test uses a **real** Dolt database (no mocks): the spec/1-to-2 and
presets/molbio/1-to-2 steps, the rename-map hook and the runner's
target-schema / post-commit-file hooks are exercised end to end through the
embedded driver (``dolt sql`` in the ledger directory).

The spec-1 fixture is built from the current tool (init, then downgrade the
generated views to their pre-``861faae`` shape and stamp ``spec_version=1``
with the fingerprint a spec-1 tool would have written), which the ticket allows
as an alternative to checking in a historical DDL dump.  The molbio-1 fixture
is a **real checked-in Dolt database** (``tests/data/ledgers/molbio-v1``) built
by ``tests/data/ledgers/build_fixtures.py`` from the wave-9-era tool; the tests
copy it into ``tmp_path`` and seed their rows, so no hand-written v1 DDL is
needed any more.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import warnings
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable, Iterator

import pytest
import yaml
from typer.testing import CliRunner

from tetramark import __version__
from tetramark.cli.app import app
from tetramark.core.ledger import Ledger, build_id_strategies, open as open_ledger
from tetramark.errors import LedgerError, UsageError
from tetramark.migrate import (
    SPEC4_ONLY_TESTS,
    MigrationStep,
    counter,
    load_renames,
    open_target,
    run,
)
from tetramark.schema.ddl import (
    STOCK_VIEW_COLUMNS,
    kind_statements,
    table_name,
    view_statements,
)
from tetramark.schema.diff import RenameMap, apply, plan
from tetramark.schema.tests_gen import generate_tests, run_check
from tetramark.schema.loader import load_ledger_schema, load_schema

import tetramark.schema.model as model

from conftest import PRESETS, REPO_PRESETS, isolated_env

ROOT = Path(__file__).resolve().parents[1]
MIGRATE_SCRIPT = ROOT / "migration" / "spec" / "1-to-2" / "migrate.py"
MOLBIO_STEP = ROOT / "migration" / "presets" / "molbio" / "1-to-2" / "migrate.py"

#: The frozen v2 preset content the historical steps install.  The repository's
#: ``presets/`` only holds the current version (base 4, molbio 3), so the
#: 1 -> 2 steps must ship what they upgrade to next to their ``migrate.py``.
BASE_V2 = ROOT / "migration" / "presets" / "base" / "1-to-2" / "base-2.yaml"
MOLBIO_V2 = ROOT / "migration" / "presets" / "molbio" / "1-to-2" / "molbio-2.yaml"
#: The current repository molbio copy (v3 is a bundle of parts) and the frozen
#: base v3 the wave-10 steps install: the repository copy is base 4 with the
#: ``grid96`` -> ``grid8x12`` rename since spec 5, so every step that upgrades
#: the closure to base 3 ships ``base-3.yaml`` next to its ``migrate.py``.
MOLBIO_V3 = REPO_PRESETS / "molbio.yaml"
BASE_V3 = ROOT / "migration" / "presets" / "base" / "3-to-4" / "base-3.yaml"
#: The last legacy base (v4: ``status_sets`` tube/cryo/animal and the per-kind
#: ``statuses:`` keys).  Tetramark-epn.4 made the repository copy the spec-6
#: fixed model, so the pre-spec-4 fixtures start from this frozen copy - the
#: content the repository shipped before that bump.
LEGACY_BASE_V4 = ROOT / "migration" / "presets" / "base" / "3-to-4" / "base-4.yaml"
#: Tetramark-500: base 6 drops the slotless ``capacity`` of shelf/freezer/drawer.
#: The baseline a base-5 ledger holds is frozen next to its manual step, and the
#: v6 target is frozen the same way so this historical step keeps working after
#: a later bump.
BASE_5 = ROOT / "migration" / "presets" / "base" / "4-to-5" / "base-5.yaml"
BASE_6 = ROOT / "migration" / "presets" / "base" / "5-to-6" / "base-6.yaml"
BASE_5_TO_6_README = ROOT / "migration" / "presets" / "base" / "5-to-6" / "README.md"
#: The frozen bacteria v1 and library v2 the old preset steps keep installing
#: after Tetramark-epn.4 bumped those parts (the repository copies are v2/v3).
FROZEN_BACTERIA_1 = (
    ROOT / "migration" / "presets" / "molbio" / "2-to-3" / "bacteria-1.yaml"
)
FROZEN_LIBRARY_2 = (
    ROOT / "migration" / "presets" / "library" / "1-to-2" / "library-2.yaml"
)
FROZEN_CELL_CULTURE_2 = (
    ROOT / "migration" / "presets" / "cell_culture" / "1-to-2" / "cell_culture-2.yaml"
)
PARTS_V3 = ("proteins", "nucleic_acids", "bacteria", "cell_culture")


def base_v2_schema():
    """The schema the frozen base v2 preset loads (for expected view counts)."""
    return load_schema(
        model.LedgerConfig(name="base-v2", presets=[str(BASE_V2)])
    )


def base_args(ledger: Ledger) -> list[str]:
    """CLI arguments selecting a standalone ledger (P3/P15: a path, no workspace)."""
    return ["--ledger", str(ledger.path)]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def script_env(dolt_env: dict[str, str]) -> dict[str, str]:
    """The environment a ``migration/…/migrate.py`` subprocess runs with."""
    return {**os.environ, **dolt_env}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def run_python(
    script: Path, *args: str, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script), *args],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
    )


def head(ledger: Ledger) -> str:
    rows = ledger.driver.query("SELECT DOLT_HASHOF('HEAD') AS `hash`")
    assert rows and rows[0]["hash"]
    return str(rows[0]["hash"])


def commit_count(ledger: Ledger) -> int:
    rows = ledger.driver.query("SELECT COUNT(*) AS `n` FROM `dolt_log`")
    return int(rows[0]["n"])


def commit_message(ledger: Ledger, commit_hash: str) -> str:
    rows = ledger.driver.query(
        "SELECT `message` FROM `dolt_log` WHERE `commit_hash` = %s", (commit_hash,)
    )
    assert rows, commit_hash
    return str(rows[0]["message"]).strip()


def tags(ledger: Ledger) -> dict[str, str]:
    rows = ledger.driver.query("SELECT `tag_name`, `tag_hash` FROM `dolt_tags`")
    return {str(row["tag_name"]): str(row["tag_hash"]) for row in rows}


def column_names(ledger: Ledger, table: str) -> set[str]:
    rows = ledger.driver.query(
        "SELECT `column_name` AS `name` FROM `information_schema`.`columns` "
        "WHERE `table_schema` = DATABASE() AND `table_name` = %s",
        (table,),
    )
    return {str(row["name"]) for row in rows}


def table_names(ledger: Ledger) -> set[str]:
    rows = ledger.driver.query(
        "SELECT `table_name` AS `name` FROM `information_schema`.`tables` "
        "WHERE `table_schema` = DATABASE()"
    )
    return {str(row["name"]) for row in rows}


def row_count(ledger: Ledger, table: str) -> int:
    rows = ledger.driver.query(f"SELECT COUNT(*) AS `n` FROM `{table}`")
    return int(rows[0]["n"])


def columns_by_table(ledger: Ledger, tables: Iterable[str]) -> dict[str, set[str]]:
    """Column names for many tables from one batched read.

    A migration test checks many tables/views at once; asking Dolt once per
    table is one process per check, which dominates the test cost.
    """
    names = list(tables)
    results = ledger.driver.query_many(
        [
            (
                "SELECT `column_name` AS `name` FROM `information_schema`.`columns` "
                "WHERE `table_schema` = DATABASE() AND `table_name` = %s",
                (table,),
            )
            for table in names
        ]
    )
    return {
        table: {str(row["name"]) for row in rows}
        for table, rows in zip(names, results)
    }


def row_counts(ledger: Ledger, tables: Iterable[str]) -> dict[str, int]:
    """``COUNT(*)`` for many tables from one batched read."""
    names = list(tables)
    results = ledger.driver.query_many(
        [(f"SELECT COUNT(*) AS `n` FROM `{table}`", None) for table in names]
    )
    return {table: int(rows[0]["n"]) for table, rows in zip(names, results)}


def v1_view_sql(statement_sql: str) -> str:
    """The spec-1 definition of a generated view: spec 2 minus its alias column.

    The alias column is the final select item of every generated view (spec
    §3), so dropping it reproduces the old shape without a second DDL copy.
    """
    select, separator, tail = statement_sql.rpartition("\nFROM ")
    assert separator, statement_sql
    last_item = select.rstrip()
    assert last_item.endswith(("`alias_list`", "`occupant_alias_list`")), (
        last_item[-80:]
    )
    comma = last_item.rfind(",\n")
    assert comma > 0
    return last_item[:comma] + separator + tail


def spec1_fingerprint(ledger: Ledger) -> str:
    """The fingerprint a spec-1 tool would have written (SPEC_VERSION=1)."""
    saved = model.SPEC_VERSION
    model.SPEC_VERSION = 1
    try:
        return ledger.schema.fingerprint()
    finally:
        model.SPEC_VERSION = saved


def downgrade_to_spec1(ledger: Ledger) -> None:
    """Turn a fresh ledger into a genuine spec-1 one and commit the change."""
    statements: list[str | tuple[str, Any]] = []
    for statement in view_statements(ledger.schema):
        statements.append(f"DROP VIEW `{statement.name}`")
        statements.append(v1_view_sql(statement.sql))
    fingerprint = spec1_fingerprint(ledger)
    statements.extend(
        [
            "UPDATE `ledger_meta` SET `value` = '1' WHERE `key` = 'spec_version'",
            "UPDATE `ledger_meta` SET `value` = '0.1.1' "
            "WHERE `key` = 'tetramark_version'",
            (
                "UPDATE `ledger_meta` SET `value` = %s WHERE `key` = 'fingerprint'",
                (fingerprint,),
            ),
        ]
    )
    # One dolt process for the whole downgrade instead of one per view DDL.
    ledger.driver.execute_many(statements)
    ledger.driver.commit("test: simulate a spec-1 ledger")
    ledger.read_meta()


def seed(ledger: Ledger) -> tuple[dict[str, Any], dict[str, Any]]:
    """One entity with two aliases and one stock with two aliquots."""
    entity = ledger.entities.add("item", name="alpha", values={"category": "control"})
    ledger.aliases.add("entity", entity["id"], "ZED-ALIAS")
    ledger.aliases.add("entity", entity["id"], "ALPHA-AC")
    stock = ledger.stocks.add(
        "lot",
        entity=entity["id"],
        values={"supplier": "acme"},
        aliquots=2,
        quantity=5,
        unit="ml",
    )
    return entity, stock


def tm_check(ledger: Ledger):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        return CliRunner().invoke(app, [*base_args(ledger), "check"])


def expected_installed_tests(ledger: Ledger) -> int:
    """How many generated tests a step may install at this ledger's spec.

    The runner omits the spec-4-only rules on a ledger that stays below spec 4
    (they read ``aliquot_event.forced``, which migration/spec/3-to-4 adds).
    """
    spec = int(str(ledger.meta.get("spec_version") or 0))
    tests = generate_tests(ledger.schema)
    if spec < 4:
        tests = [test for test in tests if test.name not in SPEC4_ONLY_TESTS]
    return len(tests)


def assert_fingerprint_refreshed(ledger: Ledger) -> None:
    """A migration step writes the current fingerprint, nothing stale.

    The ledger may still be on an older *format* (a preset step keeps
    ``spec_version``), so the manual-upgrade hint is expected; no fingerprint
    warning is.
    """
    assert not any("fingerprint does not match" in text for text in ledger.warnings)
    spec = int(str(ledger.meta.get("spec_version") or 0))
    if spec < model.SPEC_VERSION:
        hint = f"ledger is spec {spec}, tm is spec {model.SPEC_VERSION}"
        assert any(hint in text for text in ledger.warnings), ledger.warnings


def tm_check_open(ledger: Ledger):
    """``tm check`` on an already-open ledger, without reopening it.

    A migrated-but-still-spec-3 ledger keeps the spec-4-removed ``statuses:``
    key in ``ledger.yaml`` until ``migration/spec/3-to-4``, so the spec-4 CLI
    refuses to reopen it.  Migration tests assert the generated tests on the
    open ledger instead, which is the same ``run_check`` the CLI calls.
    """
    report = run_check(ledger.driver, ledger.schema)
    lines = [
        f"{'FAIL' if result.failed else 'OK:'} {result.name}"
        for result in report.results
    ]
    return SimpleNamespace(
        exit_code=0 if report.passed else 1, output="\n".join(lines)
    )


def write_step(
    directory: Path,
    *,
    kind: str,
    from_version: int,
    statements: list[str],
    preset: str | None = None,
    hook_source: str = "",
    step_args: str = "",
) -> Path:
    """A throwaway ``migrate.py`` importing the shared runner.

    ``hook_source`` may define ``after(ledger, result)`` (and use ``MARKER``, a
    path in ``directory``) and ``step_args`` appends MigrationStep keywords.
    """
    script = directory / "step.py"
    script.write_text(
        "from pathlib import Path\n"
        "from tetramark.migrate import MigrationStep, main\n"
        "\n"
        f"MARKER = Path({str(directory / 'marker.txt')!r})\n"
        "\n"
        "def plan(ledger):\n"
        f"    return {statements!r}\n"
        "\n"
        f"{hook_source}"
        "\n"
        f"step = MigrationStep(kind={kind!r}, from_version={from_version!r}, "
        f"preset={preset!r}, plan=plan{step_args})\n"
        "\n"
        'if __name__ == "__main__":\n'
        "    main(step)\n",
        encoding="utf-8",
    )
    return script


# ---------------------------------------------------------------------------
# spec/1-to-2: the real step
# ---------------------------------------------------------------------------


def test_spec_1_to_2_migrates_views_meta_tag_in_one_commit(
    ledger_factory, script_env: dict[str, str]
) -> None:
    """Verify bullet 1: alias_list, spec 2, one commit, tag, check, refuse."""
    ledger = ledger_factory([PRESETS / "base"])
    seed(ledger)
    downgrade_to_spec1(ledger)
    view_count = len(view_statements(ledger.schema))
    assert "alias_list" not in column_names(ledger, "v_entity_item")

    head_before = head(ledger)
    commits_before = commit_count(ledger)
    # The generated dolt_tests do not inspect view columns, so check is clean
    # before the step as well (documented in the step README).
    pre_check = tm_check(ledger)
    assert pre_check.exit_code == 0, pre_check.output
    assert "FAIL" not in pre_check.output

    result = run_python(
        MIGRATE_SCRIPT, "--ledger", str(ledger.path), env=script_env
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "migrate(ledger): spec 1 -> 2" in result.stdout
    assert "pre-migrate-spec-1" in result.stdout

    # -- the views now carry the alias columns --------------------------
    views = list(view_statements(ledger.schema))
    columns = columns_by_table(ledger, [statement.name for statement in views])
    for statement in views:
        expected = (
            "occupant_alias_list"
            if statement.name == "v_box_slots"
            else "alias_list"
        )
        assert expected in columns[statement.name], statement.name
    rows = ledger.driver.query("SELECT `alias_list` FROM `v_entity_item`")
    assert [row["alias_list"] for row in rows] == ["ALPHA-AC;ZED-ALIAS"]

    # -- metadata bumped in the same commit ----------------------------
    ledger.read_meta()
    assert ledger.meta["spec_version"] == "2"
    assert ledger.meta["tetramark_version"] == __version__
    assert ledger.meta["fingerprint"] == ledger.schema.fingerprint()

    # -- exactly one commit, with the contract message -----------------
    assert commit_count(ledger) == commits_before + 1
    head_after = head(ledger)
    assert head_after != head_before
    assert commit_message(ledger, head_after) == "migrate(ledger): spec 1 -> 2"

    # -- the tag points at the pre-migration commit --------------------
    assert tags(ledger) == {"pre-migrate-spec-1": head_before}

    # -- tm check is clean ---------------------------------------------
    checked = tm_check(ledger)
    assert checked.exit_code == 0, checked.output
    assert "FAIL" not in checked.output
    assert "OK:" in checked.output

    # -- a second run refuses (counter is 2, not 1) --------------------
    again = run_python(
        MIGRATE_SCRIPT, "--ledger", str(ledger.path), env=script_env
    )
    assert again.returncode == 1, again.stdout + again.stderr
    assert "refusing to run migration spec 1 -> 2" in again.stdout + again.stderr
    assert "spec_version 2, expected 1" in again.stdout + again.stderr
    assert head(ledger) == head_after


def test_spec_1_to_2_dry_run_prints_sql_and_writes_nothing(
    embedded_ledger_factory, script_env: dict[str, str]
) -> None:
    """Verify bullet 3: ``--dry-run`` writes nothing (Dolt HEAD unchanged)."""
    ledger = embedded_ledger_factory([PRESETS / "base"])
    seed(ledger)
    downgrade_to_spec1(ledger)
    head_before = head(ledger)
    commits_before = commit_count(ledger)
    view_count = len(view_statements(ledger.schema))

    result = run_python(
        MIGRATE_SCRIPT,
        "--ledger",
        str(ledger.path),
        "--dry-run",
        env=script_env,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "dry run:" in result.stdout
    assert result.stdout.count("CREATE VIEW") == view_count
    assert result.stdout.count("DROP VIEW IF EXISTS") == view_count
    assert "CALL DOLT_TAG('pre-migrate-spec-1'" in result.stdout
    assert "'spec_version', '2'" in result.stdout

    # Nothing was written.
    assert head(ledger) == head_before
    assert commit_count(ledger) == commits_before
    assert tags(ledger) == {}
    ledger.read_meta()
    assert ledger.meta["spec_version"] == "1"
    assert "alias_list" not in column_names(ledger, "v_entity_item")


def test_failed_step_prints_last_good_commit_and_restore_command(
    ledger_factory, script_env: dict[str, str], tmp_path: Path
) -> None:
    """Verify contract 5: DDL is not transactional, no commit, report printed."""
    ledger = ledger_factory([PRESETS / "base"])
    downgrade_to_spec1(ledger)
    head_before = head(ledger)
    commits_before = commit_count(ledger)
    script = write_step(
        tmp_path,
        kind="spec",
        from_version=1,
        statements=[
            "CREATE TABLE t21_partial (i INT)",
            "THIS IS NOT SQL",
        ],
    )

    result = run_python(script, "--ledger", str(ledger.path), env=script_env)
    assert result.returncode == 1, result.stdout + result.stderr
    output = result.stdout + result.stderr
    assert "failed" in output
    assert f"last good commit: {head_before}" in output
    assert f"dolt reset --hard {head_before}" in output
    assert "restore-point tag (unchanged): pre-migrate-spec-1" in output

    # No commit was made, the tag is the restore point, but the first DDL
    # statement did run (Dolt cannot roll DDL back).
    assert head(ledger) == head_before
    assert commit_count(ledger) == commits_before
    assert tags(ledger) == {"pre-migrate-spec-1": head_before}
    ledger.read_meta()
    assert ledger.meta["spec_version"] == "1"
    assert "t21_partial" in table_names(ledger)


def test_preset_step_bumps_preset_versions_counter(
    ledger_factory, script_env: dict[str, str], tmp_path: Path
) -> None:
    """The helper's preset branch (ticket 22 will use it): counter + message."""
    ledger = ledger_factory([PRESETS / "base"])
    ledger.driver.execute(
        "UPDATE `ledger_meta` SET `value` = '{\"base\": 1}' "
        "WHERE `key` = 'preset_versions'"
    )
    ledger.driver.commit("test: preset version 1")
    ledger.read_meta()
    head_before = head(ledger)
    commits_before = commit_count(ledger)
    script = write_step(
        tmp_path,
        kind="preset",
        preset="base",
        from_version=1,
        statements=["UPDATE `alias` SET `alias` = `alias` WHERE 1 = 0"],
    )

    result = run_python(script, "--ledger", str(ledger.path), env=script_env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "migrate(ledger): base 1 -> 2" in result.stdout

    ledger.read_meta()
    assert json.loads(ledger.meta["preset_versions"]) == {"base": 2}
    assert ledger.meta["tetramark_version"] == __version__
    assert commit_count(ledger) == commits_before + 1
    head_after = head(ledger)
    assert commit_message(ledger, head_after) == "migrate(ledger): base 1 -> 2"
    assert tags(ledger) == {"pre-migrate-base-1": head_before}

    again = run_python(script, "--ledger", str(ledger.path), env=script_env)
    assert again.returncode == 1, again.stdout + again.stderr
    assert "preset_versions[base] 2, expected 1" in again.stdout + again.stderr


def test_migration_can_run_on_a_branch(
    ledger_factory, script_env: dict[str, str]
) -> None:
    """``--branch``: the commit lands on the branch, the checkout stays put."""
    ledger = ledger_factory([PRESETS / "base"])
    downgrade_to_spec1(ledger)
    ledger.driver.execute("CALL DOLT_BRANCH(%s)", ("upgrade",))
    main_head = head(ledger)

    result = run_python(
        MIGRATE_SCRIPT,
        "--ledger",
        str(ledger.path),
        "--branch",
        "upgrade",
        env=script_env,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "branch 'upgrade'" in result.stdout

    # The checked-out branch (`main`) did not move.
    assert head(ledger) == main_head
    ledger.read_meta()
    assert ledger.meta["spec_version"] == "1"

    branch = open_ledger(ledger.path, branch="upgrade")
    try:
        branch.read_meta()
        assert branch.meta["spec_version"] == "2"
        branch_head = head(branch)
        assert branch_head != main_head
        assert commit_message(branch, branch_head) == "migrate(ledger): spec 1 -> 2"
        assert tags(branch) == {"pre-migrate-spec-1": main_head}
    finally:
        branch.close()


def test_current_tm_opens_a_spec_1_ledger_with_a_fingerprint_warning(
    ledger_factory,
) -> None:
    """No refusal: a stale fingerprint, an upgrade hint, entity writes allowed."""
    ledger = ledger_factory([PRESETS / "base"])
    downgrade_to_spec1(ledger)
    spec1_head = head(ledger)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        reopened = open_ledger(ledger.path)
    try:
        messages = [str(item.message) for item in caught]
        assert any("fingerprint does not match" in text for text in messages), messages
        assert any(
            f"ledger is spec 1, tm is spec {model.SPEC_VERSION}: "
            "follow migration/spec/1-to-2" in text
            for text in messages
        ), messages
        assert reopened.warnings  # collected on the ledger too
        assert reopened.check_fingerprint(warn=False) is False
        # Opening does not rewrite, refuse or upgrade anything.
        assert reopened.meta["spec_version"] == "1"
        assert head(reopened) == spec1_head
        # Notices only: neither warning refuses or upgrades anything.
        current = plan(reopened)
        assert current.destructive == []
        assert current.meta_stale is True
        actions = {(step.action, step.name) for step in current.steps}
        assert ("drop_view", "v_entity_item") in actions
        assert ("create_view", "v_entity_item") in actions
        # ... and a write is not refused either (the guard does not exist yet).
        created = reopened.entities.add("item", name="written-at-spec-1")
        assert created["id"]
        assert head(reopened) != spec1_head
    finally:
        reopened.close()


# ---------------------------------------------------------------------------
# Renames hook: explicit map vs ``renamed_from``
# ---------------------------------------------------------------------------

OLD_PRESET: dict[str, Any] = {
    "preset": "rn",
    "version": 1,
    "kinds": {
        "construct": {
            "object": "entity",
            "fields": {
                "name": {"type": "string"},
                "sequence": {"type": "sequence"},
            },
        },
        "bacterial": {
            "object": "stock",
            "entity_kinds": ["construct"],
            "fields": {"strain": {"type": "string"}},
        },
        "box96": {
            "object": "box",
            "geometry": {"type": "grid", "rows": "A-H", "cols": "1-12"},
        },
    },
}

NEW_KINDS: dict[str, Any] = {
    "protein_construct": {
        "object": "entity",
        "fields": {
            "name": {"type": "string"},
            "dna_sequence": {"type": "sequence"},
        },
    },
    "bacterial_glycerol": {
        "object": "stock",
        "entity_kinds": ["protein_construct"],
        "fields": {"strain_name": {"type": "string"}},
    },
    "box96": {
        "object": "box",
        "geometry": {"type": "grid", "rows": "A-H", "cols": "1-12"},
    },
}

RENAME_MAP = {
    "entity": {"construct": "protein_construct"},
    "stock": {"bacterial": "bacterial_glycerol"},
    "fields": {
        "entity.protein_construct": {"sequence": "dna_sequence"},
        "stock.bacterial_glycerol": {"strain": "strain_name"},
    },
}


#: The status set the throwaway ``rn`` presets use (spec 4).
PRESET_STATUS_SETS = {
    "tube": {
        "pending": {"in_stock": False, "to": ["stored", "missing", "consumed"]},
        "stored": {"in_stock": True, "to": ["missing", "consumed"]},
        "missing": {"in_stock": False, "to": ["stored", "consumed"]},
        "consumed": {"in_stock": False},
    }
}


def _with_status_sets(kinds: dict[str, Any]) -> dict[str, Any]:
    """Give every stock kind of a throwaway preset the ``tube`` set (spec 4)."""
    payload = json.loads(json.dumps(kinds))
    for kind in payload.values():
        if kind.get("object") == "stock" and "statuses" not in kind:
            kind["statuses"] = "tube"
    return payload


def write_preset(
    directory: Path,
    kinds: dict[str, Any],
    *,
    version: int,
    ledger: Ledger | None = None,
) -> Path:
    """Write a ``rn`` preset, and with ``ledger`` its own copy as well.

    Ticket 20: a ledger reads presets only from ``<ledger>/presets/``, so
    changing the source directory of an existing ledger has no effect. Tests
    that evolve a live ledger's schema must write the ledger's copy too.
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "preset.yaml"
    text = yaml.safe_dump(
        {
            "preset": "rn",
            "version": version,
            "status_sets": PRESET_STATUS_SETS,
            "kinds": _with_status_sets(kinds),
        },
        sort_keys=False,
    )
    path.write_text(text, encoding="utf-8")
    if ledger is not None:
        copy = ledger.path / "presets" / "rn" / "preset.yaml"
        assert copy.is_file(), f"ledger has no own preset copy at {copy}"
        copy.write_text(text, encoding="utf-8")
    return path


def reload_schema(ledger: Ledger) -> None:
    """Re-read ledger.yaml + the preset, as a fresh ``open`` would."""
    config, schema = load_ledger_schema(
        ledger.path, allow_legacy_statuses=True
    )
    ledger.config = config
    ledger.schema = schema
    ledger.ids = build_id_strategies(config)
    ledger.read_meta()


def seed_rename_data(ledger: Ledger) -> None:
    """Rows in the old shape: entity+sequence, stock, aliquot, extension, alias."""
    ledger.driver.execute(
        "INSERT INTO `entity` (`id`, `kind`) VALUES ('E1', 'construct')"
    )
    ledger.driver.execute(
        "INSERT INTO `entity_construct` (`id`, `name`, `sequence`) "
        "VALUES ('E1', 'p1', 'ATGC')"
    )
    ledger.driver.execute(
        "INSERT INTO `stock` (`id`, `kind`, `entity_id`) VALUES ('S1', 'bacterial', 'E1')"
    )
    ledger.driver.execute(
        "INSERT INTO `stock_bacterial` (`id`, `kind`, `strain`) "
        "VALUES ('S1', 'bacterial', 'DH5a')"
    )
    ledger.driver.execute(
        "INSERT INTO `aliquot` (`id`, `stock_id`, `seq`, `status`) "
        "VALUES ('S1.1', 'S1', 1, 'stored')"
    )
    ledger.driver.execute(
        "INSERT INTO `alias` (`object_type`, `object_id`, `alias`) "
        "VALUES ('entity', 'E1', 'OLD-CONSTRUCT')"
    )
    ledger.driver.commit("test: seed old-shape rows")


def renamed_from_kinds() -> dict[str, Any]:
    kinds = json.loads(json.dumps(NEW_KINDS))
    kinds["protein_construct"]["renamed_from"] = "construct"
    kinds["protein_construct"]["fields"]["dna_sequence"]["renamed_from"] = "sequence"
    kinds["bacterial_glycerol"]["renamed_from"] = "bacterial"
    kinds["bacterial_glycerol"]["fields"]["strain_name"]["renamed_from"] = "strain"
    return kinds


def test_rename_map_renames_kind_tables_and_kind_values_without_data_loss(
    ledger_factory, tmp_path: Path
) -> None:
    """Verify bullet 2: map-driven rename == ``renamed_from``, rows preserved."""
    preset_map = tmp_path / "preset_map"
    preset_rf = tmp_path / "preset_renamed_from"
    write_preset(preset_map, OLD_PRESET["kinds"], version=1)
    write_preset(preset_rf, OLD_PRESET["kinds"], version=1)
    ledger_map = ledger_factory([preset_map], name="rename-map")
    ledger_rf = ledger_factory([preset_rf], name="rename-renamed-from")
    for ledger in (ledger_map, ledger_rf):
        seed_rename_data(ledger)

    write_preset(preset_map, NEW_KINDS, version=2, ledger=ledger_map)
    write_preset(preset_rf, renamed_from_kinds(), version=2, ledger=ledger_rf)
    reload_schema(ledger_map)
    reload_schema(ledger_rf)

    plan_map = plan(ledger_map, renames=RenameMap.from_dict(RENAME_MAP))
    plan_rf = plan(ledger_rf)
    assert plan_map.to_dict()["steps"] == plan_rf.to_dict()["steps"]
    assert plan_map.destructive == []

    actions = {(step.action, step.name) for step in plan_map.steps}
    for expected in (
        ("rename_table", "entity_protein_construct"),
        ("rename_table", "stock_bacterial_glycerol"),
        ("rename_column", "entity_protein_construct.dna_sequence"),
        ("rename_column", "stock_bacterial_glycerol.strain_name"),
        ("update_data", "entity"),
        ("update_data", "stock"),
    ):
        assert expected in actions, expected
    # The map reuses the planner: no hand-written SQL for the renames.
    for step in plan_map.steps:
        if step.action == "rename_table":
            assert step.sql.startswith("ALTER TABLE `")
            assert "RENAME TO" in step.sql

    applied = apply(ledger_map, schema_plan=plan_map)
    assert applied.applied is True
    applied_rf = apply(ledger_rf, schema_plan=plan_rf)
    assert applied_rf.applied is True

    for ledger in (ledger_map, ledger_rf):
        tables = table_names(ledger)
        assert "entity_protein_construct" in tables
        assert "stock_bacterial_glycerol" in tables
        for gone in (
            "entity_construct",
            "stock_bacterial",
        ):
            assert gone not in tables

        # No data loss: every row is still there, under its new kind.
        assert row_count(ledger, "entity") == 1
        assert row_count(ledger, "stock") == 1
        assert row_count(ledger, "aliquot") == 1
        assert ledger.driver.query(
            "SELECT `id`, `kind` FROM `entity`"
        ) == [{"id": "E1", "kind": "protein_construct"}]
        assert ledger.driver.query(
            "SELECT `id`, `kind` FROM `stock`"
        ) == [{"id": "S1", "kind": "bacterial_glycerol"}]
        assert ledger.driver.query(
            "SELECT `id`, `kind`, `strain_name` FROM `stock_bacterial_glycerol`"
        ) == [
            {"id": "S1", "kind": "bacterial_glycerol", "strain_name": "DH5a"}
        ]
        # The renamed sequence column keeps its value and its generated hash.
        assert ledger.driver.query(
            "SELECT `id`, `dna_sequence`, `dna_sequence_sha256` IS NOT NULL AS `hashed` "
            "FROM `entity_protein_construct`"
        ) == [{"id": "E1", "dna_sequence": "ATGC", "hashed": 1}]
        # Aliases (and with them the old handles) survive the rename.
        assert ledger.driver.query("SELECT `alias` FROM `alias`") == [
            {"alias": "OLD-CONSTRUCT"}
        ]
        assert "v_entity_protein_construct" in table_names(ledger)
        assert "alias_list" in column_names(ledger, "v_entity_protein_construct")


def test_rename_map_validation() -> None:
    """A malformed map fails loudly instead of renaming nothing."""
    with pytest.raises(UsageError):
        RenameMap.from_dict({"kinds": {"construct": "protein_construct"}})
    with pytest.raises(UsageError):
        RenameMap.from_dict({"entity": {"Construct": "protein_construct"}})
    with pytest.raises(UsageError):
        RenameMap.from_dict({"entity": {"construct": "construct"}})
    with pytest.raises(UsageError):
        RenameMap.from_dict({"fields": {"construct": {"a": "b"}}})
    with pytest.raises(UsageError):
        RenameMap.from_dict({"fields": {"stock.bacterial": {"Bad": "worse"}}})

    parsed = RenameMap.from_dict(RENAME_MAP)
    assert parsed.entity == {"construct": "protein_construct"}
    assert parsed.stock == {"bacterial": "bacterial_glycerol"}
    assert parsed.fields["stock.bacterial_glycerol"] == {"strain": "strain_name"}


def test_load_renames_reads_the_step_file(tmp_path: Path) -> None:
    path = tmp_path / "renames.yaml"
    path.write_text(
        "entity: {construct: protein_construct}\n"
        "stock:  {bacterial: bacterial_glycerol}\n"
        "fields: {stock.bacterial_glycerol: {strain: strain_name}}\n",
        encoding="utf-8",
    )
    renames = load_renames(path)
    assert isinstance(renames, RenameMap)
    assert renames.entity == {"construct": "protein_construct"}
    assert renames.fields["stock.bacterial_glycerol"] == {"strain": "strain_name"}

    with pytest.raises(UsageError):
        load_renames(tmp_path / "missing.yaml")
    broken = tmp_path / "broken.yaml"
    broken.write_text("entity: [not: a mapping\n", encoding="utf-8")
    with pytest.raises(UsageError):
        load_renames(broken)


# ---------------------------------------------------------------------------
# Runner API details
# ---------------------------------------------------------------------------


def test_open_target_rejects_a_workspace_directory(tmp_path: Path) -> None:
    """A 0.4 workspace directory is not a ledger: the path is opened directly."""
    workspace = tmp_path / "Dolt"
    workspace.mkdir()
    (workspace / "workspace.yaml").write_text("name: Dolt\n", encoding="utf-8")
    with pytest.raises(UsageError, match="not a Tetramark ledger"):
        open_target(workspace)


def test_open_target_ignores_a_parent_workspace_yaml(
    ledger_factory,
) -> None:
    """No parent/``workspace.yaml`` guessing: the ledger path is enough (P7)."""
    ledger = ledger_factory([PRESETS / "base"])
    (ledger.path.parent / "workspace.yaml").write_text(
        "name: Dolt\n", encoding="utf-8"
    )
    reopened = open_target(ledger.path)
    try:
        assert reopened.path == ledger.path
        assert reopened.name == ledger.name
    finally:
        reopened.close()


def test_run_refuses_a_dirty_working_set(
    ledger_factory, script_env: dict[str, str]
) -> None:
    """A migration commit must be the only change in its commit."""
    ledger = ledger_factory([PRESETS / "base"])
    downgrade_to_spec1(ledger)
    head_before = head(ledger)
    ledger.driver.execute("CREATE TABLE t21_dirty (i INT)")
    try:
        result = run_python(
            MIGRATE_SCRIPT, "--ledger", str(ledger.path), env=script_env
        )
        assert result.returncode == 1, result.stdout + result.stderr
        output = result.stdout + result.stderr
        assert "working set has uncommitted changes" in output
        assert "t21_dirty" in output
        assert head(ledger) == head_before
        assert tags(ledger) == {}
    finally:
        ledger.driver.execute("DROP TABLE `t21_dirty`")


def test_counter_reads_spec_and_preset_counters(
    ledger_factory,
) -> None:
    ledger = ledger_factory([PRESETS / "base"])
    spec_step = MigrationStep(
        kind="spec", from_version=1, plan=lambda opened: ["SELECT 1"]
    )
    preset_step = MigrationStep(
        kind="preset", preset="base", from_version=1, plan=lambda opened: ["SELECT 1"]
    )
    assert counter(spec_step, ledger) == model.SPEC_VERSION
    assert counter(preset_step, ledger) == 2

    ledger.driver.execute(
        "UPDATE `ledger_meta` SET `value` = '{\"base\": 1}' "
        "WHERE `key` = 'preset_versions'"
    )
    ledger.read_meta()
    assert counter(preset_step, ledger) == 1

    # ``run`` returns the report (the API ticket 24 may call).
    ledger.driver.commit("test: preset version 1")
    sink = io.StringIO()
    result = run(preset_step, ledger_path=ledger.path, out=sink)
    assert result.applied is True
    assert result.tag_created is True
    assert result.commit_hash
    ledger.read_meta()
    assert json.loads(ledger.meta["preset_versions"]) == {"base": 2}


# ---------------------------------------------------------------------------
# presets/molbio/1-to-2: the real step (ticket 22)
# ---------------------------------------------------------------------------

#: The checked-in molbio-1 fixture, built by
#: ``tests/data/ledgers/build_fixtures.py`` from the wave-9 checkout
#: (``3ec57d5``, tetramark 0.1.2, the last tool that accepts the v1 preset):
#: a real Dolt database with the four v1 kind tables, the ``aliquot_bacterial``
#: extension table, ``spec_version=2`` and
#: ``preset_versions={"base": 1, "molbio": 1}``.  Its own v1 preset copy had the
#: removed ``aliquot_fields`` key stripped after the build, so the current
#: (spec 3) loader can open the ledger; the **database shape** is untouched.
MOLBIO_V1_FIXTURE = ROOT / "tests" / "data" / "ledgers" / "molbio-v1"
BUILD_FIXTURES = ROOT / "tests" / "data" / "ledgers" / "build_fixtures.py"


def copy_molbio_v1(destination: Path) -> Path:
    """Copy the checked-in molbio-1 ledger directory to ``destination``."""
    assert (MOLBIO_V1_FIXTURE / ".dolt").is_dir(), (
        f"the checked-in molbio-1 fixture is missing at {MOLBIO_V1_FIXTURE}; "
        f"rebuild it with: uv run python {BUILD_FIXTURES.relative_to(ROOT)}"
    )
    shutil.copytree(MOLBIO_V1_FIXTURE, destination)
    return destination


def open_copied_molbio_v1(
    path: Path, *, env: dict[str, str] | None = None
) -> Ledger:
    """Open a copied fixture directory, tolerating its documented legacy shape.

    A genuine v1 ledger records the fingerprint the spec-2 tool computed and
    still carries the spec-4-removed ``statuses:`` key in ``ledger.yaml``; the
    current loader computes a different fingerprint and warns, and the spec-4
    read path refuses the legacy key.  Migration steps are the supported reader
    of that shape (``allow_legacy_statuses``), so the helper opens it the same
    way instead of polluting the suite's warning summary.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        ledger = open_ledger(path, allow_legacy_statuses=True, env=env)
    assert ledger.warnings, "a genuine v1 ledger must warn about its stale fingerprint"
    assert any("fingerprint does not match" in text for text in ledger.warnings)
    return ledger


@pytest.fixture
def molbio_v1_ledger(
    tmp_path: Path, dolt_env: dict[str, str]
) -> Iterator[Ledger]:
    """A fresh copy of the checked-in molbio-1 fixture (embedded driver)."""
    ledger_path = tmp_path / "molbio-v1"
    copy_molbio_v1(ledger_path)
    ledger = open_copied_molbio_v1(ledger_path, env=dolt_env)
    try:
        yield ledger
    finally:
        ledger.close()


#: Every old molbio kind, a bacterial stock with two agreeing aliquots (S1), a
#: second bacterial stock with a different value (S2), and one row of each other
#: stock kind.  S1/S2 differ from each other on purpose: the value is per stock.
def seed_molbio_v1(ledger: Ledger) -> None:
    execute = ledger.driver.execute
    execute(
        "INSERT INTO `entity` (`id`, `kind`) VALUES "
        "('E1','construct'),('E2','plasmid'),('E3','strain')"
    )
    execute(
        "INSERT INTO `entity_construct` (`id`, `name`, `sequence`, `molecular_weight_da`) "
        "VALUES ('E1','c01','ATGC', 12345.67)"
    )
    execute(
        "INSERT INTO `entity_plasmid` (`id`, `name`, `sequence`, `construct`) "
        "VALUES ('E2','p01','ATGG','E1')"
    )
    execute(
        "INSERT INTO `entity_strain` (`id`, `name`, `genotype`) "
        "VALUES ('E3','DH5a','F- endA1')"
    )
    execute(
        "INSERT INTO `alias` (`object_type`, `object_id`, `alias`) "
        "VALUES ('entity','E1','OLD-C1')"
    )
    execute(
        "INSERT INTO `stock` (`id`, `kind`, `entity_id`) VALUES "
        "('S1','bacterial','E3'),('S2','bacterial','E2'),"
        "('S3','plasmid_prep','E2'),('S4','protein_prep','E1'),"
        "('S5','pellet','E2')"
    )
    execute(
        "INSERT INTO `stock_bacterial` (`id`, `strain`, `medium`) VALUES "
        "('S1','DH5a','LB'),('S2','BL21(DE3)','TB')"
    )
    execute(
        "INSERT INTO `stock_plasmid_prep` (`id`, `concentration_ng_ul`) "
        "VALUES ('S3', 120.5)"
    )
    execute(
        "INSERT INTO `stock_protein_prep` (`id`, `concentration_mg_ml`) "
        "VALUES ('S4', 1.5)"
    )
    execute(
        "INSERT INTO `stock_pellet` (`id`, `culture_volume_ml`, `od600`) "
        "VALUES ('S5', 500, 2.4)"
    )
    # Spec 5: an in-stock aliquot has a position, and this v1 fixture records
    # none; the tubes are unplaced, so they are seeded ``pending``.  Placement
    # is established later by migration/spec/4-to-5, not by molbio 1 -> 2.
    execute(
        "INSERT INTO `aliquot` (`id`, `stock_id`, `seq`, `status`, `quantity`, `unit`) "
        "VALUES ('S1.1','S1',1,'pending',500,'ul'),('S1.2','S1',2,'pending',500,'ul'),"
        "('S2.1','S2',1,'pending',500,'ul'),('S3.1','S3',1,'pending',50,'ul'),"
        "('S4.1','S4',1,'pending',100,'ul'),('S5.1','S5',1,'pending',250,'ml'),"
        "('S5.2','S5',2,'pending',250,'ml')"
    )
    execute(
        "INSERT INTO `aliquot_bacterial` (`id`, `glycerol_pct`) VALUES "
        "('S1.1',25),('S1.2',25),('S2.1',15)"
    )
    ledger.driver.commit("test: seed molbio v1 rows")
    ledger.read_meta()


def test_molbio_1_to_2_migrates_renames_moves_glycerol_and_vendors_the_preset(
    molbio_v1_ledger, script_env: dict[str, str]
) -> None:
    """Verify bullet 2, both drivers: renames, data move, metadata, files, check."""
    ledger = molbio_v1_ledger
    seed_molbio_v1(ledger)
    preset_copy = ledger.path / "presets" / "molbio.yaml"
    preset_before = preset_copy.read_bytes()
    assert yaml.safe_load(preset_before)["version"] == 1
    assert ledger.schema.preset_versions == {"base": 1, "molbio": 1}
    assert ledger.meta["tetramark_version"] == "0.1.2"  # built by the legacy tool
    assert ledger.meta["spec_version"] == "2"
    assert "aliquot_bacterial" in table_names(ledger)

    head_before = head(ledger)
    commits_before = commit_count(ledger)

    # -- dry run: full plan, the data move spliced at its safe point, no writes
    dry = run_python(
        MOLBIO_STEP, "--ledger", str(ledger.path), "--dry-run", env=script_env
    )
    assert dry.returncode == 0, dry.stdout + dry.stderr
    out = dry.stdout
    assert "migration: molbio 1 -> 2" in out
    assert "CALL DOLT_TAG('pre-migrate-molbio-1'" in out
    assert "dolt tests: replace with" in out
    assert "after commit:" in out
    add_at = out.index("ADD COLUMN `glycerol_pct`")
    copy_at = out.index("UPDATE `stock_bacterial_glycerol`")
    drop_at = out.index("DROP TABLE IF EXISTS `aliquot_bacterial`")
    assert add_at < copy_at < drop_at, out
    # The extension rows are keyed by aliquot id: the join goes through aliquot.
    assert "JOIN `aliquot` AS `a` ON `a`.`stock_id` = `s`.`id`" in out
    assert "JOIN `aliquot_bacterial` AS `e` ON `e`.`id` = `a`.`id`" in out
    assert "SET `s`.`glycerol_pct` = `e`.`glycerol_pct`" in out

    # Nothing was written: DDL, tag, meta and the ledger's preset copy.
    assert head(ledger) == head_before
    assert commit_count(ledger) == commits_before
    assert tags(ledger) == {}
    assert preset_copy.read_bytes() == preset_before
    ledger.read_meta()
    assert json.loads(ledger.meta["preset_versions"]) == {"base": 1, "molbio": 1}
    assert "aliquot_bacterial" in table_names(ledger)

    # -- apply -------------------------------------------------------------
    applied = run_python(MOLBIO_STEP, "--ledger", str(ledger.path), env=script_env)
    assert applied.returncode == 0, applied.stdout + applied.stderr
    assert "committed" in applied.stdout
    assert "migrate(ledger): molbio 1 -> 2" in applied.stdout
    assert "post-commit: replaced molbio.yaml" in applied.stdout

    # The ledger is still spec 3 (its ledger.yaml keeps statuses: until
    # migration/spec/3-to-4), so reopen it through the migration path.
    reopened = open_ledger(
        ledger.path, allow_legacy_statuses=True
    )
    try:
        # The vendored v2 copy reopens without a fingerprint warning.
        assert_fingerprint_refreshed(reopened)
        assert reopened.check_fingerprint(warn=False) is True
        assert reopened.schema.preset_versions == {"base": 2, "molbio": 2}
        assert reopened.meta["fingerprint"] == reopened.schema.fingerprint()
        # The step bumps only its own preset: base's counter is bumped by
        # migration/presets/base/1-to-2, which is a separate (idempotent) step.
        assert json.loads(reopened.meta["preset_versions"]) == {
            "base": 1,
            "molbio": 2,
        }

        # Renamed tables exist, old ones and the extension table are gone.
        tables = table_names(reopened)
        for renamed in (
            "entity_protein_construct",
            "entity_bacterial_strain",
            "entity_cell_line",
            "stock_bacterial_glycerol",
            "stock_bacterial_pellet",
            "stock_cell_vial",
            "stock_cell_pellet",
        ):
            assert renamed in tables, renamed
            assert f"v_{renamed}" in tables, f"v_{renamed}"
        for gone in (
            "entity_construct",
            "entity_strain",
            "stock_bacterial",
            "stock_pellet",
            "aliquot_bacterial",
        ):
            assert gone not in tables, gone
        # No per-stock extension table is left; aliquot/_event are core.
        assert {name for name in tables if name.startswith("aliquot_")} == {
            "aliquot_event"
        }

        # Every row is kept, under its new name.
        assert row_count(reopened, "entity") == 3
        assert row_count(reopened, "stock") == 5
        assert row_count(reopened, "aliquot") == 7
        assert reopened.driver.query(
            "SELECT `id`, `kind` FROM `entity` ORDER BY `id`"
        ) == [
            {"id": "E1", "kind": "protein_construct"},
            {"id": "E2", "kind": "plasmid"},
            {"id": "E3", "kind": "bacterial_strain"},
        ]
        assert {
            row["id"]: row["kind"]
            for row in reopened.driver.query("SELECT `id`, `kind` FROM `stock`")
        } == {
            "S1": "bacterial_glycerol",
            "S2": "bacterial_glycerol",
            "S3": "plasmid_prep",
            "S4": "protein_prep",
            "S5": "bacterial_pellet",
        }
        # glycerol_pct now lives on the stock, per stock (not per tube).
        assert {
            row["id"]: str(row["glycerol_pct"])
            for row in reopened.driver.query(
                "SELECT `id`, `glycerol_pct` FROM `stock_bacterial_glycerol` "
                "ORDER BY `id`"
            )
        } == {"S1": "25.00", "S2": "15.00"}
        assert reopened.driver.query(
            "SELECT `strain`, `medium` FROM `stock_bacterial_glycerol` "
            "WHERE `id` = 'S2'"
        ) == [{"strain": "BL21(DE3)", "medium": "TB"}]
        # Aliases and the renamed entity fields survive.
        assert reopened.driver.query("SELECT `alias` FROM `alias`") == [
            {"alias": "OLD-C1"}
        ]
        assert reopened.driver.query(
            "SELECT `name`, `sequence` FROM `entity_protein_construct`"
        ) == [{"name": "c01", "sequence": "ATGC"}]

        # The generated dolt_tests were regenerated from v2 in the same commit:
        # none still references a table/kind that the commit renamed or dropped.
        stale = reopened.driver.query(
            "SELECT COUNT(*) AS `n` FROM `dolt_tests` WHERE "
            "`test_query` LIKE '%entity_construct%' "
            "OR `test_query` LIKE '%stock_bacterial`%' "
            "OR `test_query` LIKE '%aliquot_bacterial%'"
        )
        assert stale == [{"n": 0}]
        assert row_count(reopened, "dolt_tests") == len(
            reopened.check().results
        )

        # One commit, contract message, tag on the pre-migration commit.
        assert commit_count(reopened) == commits_before + 1
        head_after = head(reopened)
        assert head_after != head_before
        assert commit_message(reopened, head_after) == "migrate(ledger): molbio 1 -> 2"
        assert tags(reopened) == {"pre-migrate-molbio-1": head_before}

        # Ticket 20: the ledger's own copy is the repository v2 preset, byte
        # for byte, and base.yaml is unchanged.
        assert (reopened.path / "presets" / "molbio.yaml").read_bytes() == (
            MOLBIO_V2.read_bytes()
        )
        assert yaml.safe_load(
            (reopened.path / "presets" / "molbio.yaml").read_text(encoding="utf-8")
        )["version"] == 2
        # base is frozen v2 here: the repository copy is v3 nowadays, and this
        # step must still install the version its counter reaches.
        assert (reopened.path / "presets" / "base.yaml").read_bytes() == (
            BASE_V2.read_bytes()
        )

        # tm check runs the regenerated tests: no FAIL, no stale-table error.
        checked = tm_check_open(reopened)
        assert checked.exit_code == 0, checked.output
        assert "FAIL" not in checked.output
        assert "OK:" in checked.output
    finally:
        reopened.close()

    # -- a second run refuses (the counter is 2, not 1) --------------------
    again = run_python(MOLBIO_STEP, "--ledger", str(ledger.path), env=script_env)
    assert again.returncode == 1, again.stdout + again.stderr
    assert "refusing to run migration molbio 1 -> 2" in again.stdout + again.stderr
    assert "preset_versions[molbio] 2, expected 1" in again.stdout + again.stderr
    assert head(ledger) == head_after


def test_molbio_1_to_2_refuses_conflicting_glycerol_and_writes_nothing(
    molbio_v1_ledger, script_env: dict[str, str]
) -> None:
    """Verify bullet 3: per-stock disagreement refuses, listing the stock."""
    ledger = molbio_v1_ledger
    seed_molbio_v1(ledger)
    ledger.driver.execute(
        "UPDATE `aliquot_bacterial` SET `glycerol_pct` = 30 WHERE `id` = 'S1.2'"
    )
    ledger.driver.commit("test: aliquots of S1 disagree on glycerol_pct")
    ledger.read_meta()

    preset_before = (ledger.path / "presets" / "molbio.yaml").read_bytes()
    head_before = head(ledger)
    commits_before = commit_count(ledger)

    for flags in (["--dry-run"], []):
        result = run_python(
            MOLBIO_STEP, "--ledger", str(ledger.path), *flags, env=script_env
        )
        assert result.returncode == 1, result.stdout + result.stderr
        output = result.stdout + result.stderr
        assert "refusing to migrate molbio 1 -> 2" in output
        assert "disagree on glycerol_pct" in output
        # The offending stock is listed with both values.
        assert "S1" in output
        assert "25.00" in output and "30.00" in output
        # Nothing was written: no commit, no tag, no DDL, no file change.
        assert head(ledger) == head_before
        assert commit_count(ledger) == commits_before
        assert tags(ledger) == {}
        assert (ledger.path / "presets" / "molbio.yaml").read_bytes() == preset_before
        ledger.read_meta()
        assert json.loads(ledger.meta["preset_versions"]) == {"base": 1, "molbio": 1}
        assert "entity_construct" in table_names(ledger)
        assert "aliquot_bacterial" in table_names(ledger)


def test_molbio_1_to_2_refuses_a_destructive_plan_and_writes_nothing(
    molbio_v1_ledger, script_env: dict[str, str]
) -> None:
    """F1a: a plan with a destructive step is refused, never executed.

    ``tm schema apply`` refuses this same plan without ``--allow-destructive``;
    the step must not execute it silently.  A hand-added column that the v2
    schema does not know is planned as ``[destructive] drop_column``.
    """
    ledger = molbio_v1_ledger
    seed_molbio_v1(ledger)
    ledger.driver.execute(
        "ALTER TABLE `entity_construct` ADD COLUMN `legacy_note` VARCHAR(64)"
    )
    ledger.driver.execute(
        "UPDATE `entity_construct` SET `legacy_note` = 'keep me' WHERE `id` = 'E1'"
    )
    ledger.driver.commit("test: hand-added column on a v1 kind table")
    ledger.read_meta()

    preset_before = (ledger.path / "presets" / "molbio.yaml").read_bytes()
    head_before = head(ledger)
    commits_before = commit_count(ledger)

    for flags in (["--dry-run"], []):
        result = run_python(
            MOLBIO_STEP, "--ledger", str(ledger.path), *flags, env=script_env
        )
        assert result.returncode == 1, result.stdout + result.stderr
        output = result.stdout + result.stderr
        assert "refusing to migrate molbio 1 -> 2" in output
        assert "destructive step(s)" in output
        assert "drop_column" in output
        assert "legacy_note" in output
        assert "DROP COLUMN `legacy_note`" in output
        assert "tm schema apply --allow-destructive" in output
        # Refused before writing: no commit, no tag, no DDL, no file change.
        assert head(ledger) == head_before
        assert commit_count(ledger) == commits_before
        assert tags(ledger) == {}
        assert (ledger.path / "presets" / "molbio.yaml").read_bytes() == preset_before
        ledger.read_meta()
        assert json.loads(ledger.meta["preset_versions"]) == {"base": 1, "molbio": 1}
        assert "legacy_note" in column_names(ledger, "entity_construct")
        assert ledger.driver.query(
            "SELECT `legacy_note` FROM `entity_construct` WHERE `id` = 'E1'"
        ) == [{"legacy_note": "keep me"}]


def test_molbio_1_to_2_refuses_a_directory_preset_copy(
    molbio_v1_ledger, script_env: dict[str, str]
) -> None:
    """Vendoring writes ``presets/<name>.yaml``; a directory copy would win on
    reopen (``<name>/`` is the first candidate), so the step refuses first."""
    ledger = molbio_v1_ledger
    copies = ledger.path / "presets"
    document = (copies / "molbio.yaml").read_bytes()
    (copies / "molbio.yaml").unlink()
    (copies / "molbio").mkdir()
    (copies / "molbio" / "preset.yaml").write_bytes(document)
    reopenable = open_copied_molbio_v1(ledger.path)
    reopenable.close()

    head_before = head(ledger)
    result = run_python(
        MOLBIO_STEP, "--ledger", str(ledger.path), "--dry-run", env=script_env
    )
    assert result.returncode == 1, result.stdout + result.stderr
    output = result.stdout + result.stderr
    assert "refusing to migrate molbio 1 -> 2" in output
    assert "is a directory" in output
    assert "molbio" in output
    assert head(ledger) == head_before
    assert tags(ledger) == {}


def _write_widgets_preset(directory: Path) -> Path:
    """An unrelated preset with one entity kind (it does not include base)."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "widgets-src.yaml"
    path.write_text(
        "preset: widgets\n"
        "version: 1\n"
        "description: unrelated entity kind for the multi-preset migration test\n"
        "kinds:\n"
        "  widget:\n"
        "    object: entity\n"
        "    fields:\n"
        "      size: {type: int}\n",
        encoding="utf-8",
    )
    return path


def _create_widgets_kind(ledger: Ledger) -> None:
    """Create the extra preset's table/view in the copied v1 ledger.

    The checked-in fixture predates the ``widgets`` preset, so after adding that
    preset to the ledger's config the live database needs its kind's DDL, which
    a fresh ``tm init`` would have created.  Only this kind's generated
    statements run; the v1 molbio tables stay exactly as the legacy tool built
    them.
    """
    statements = [
        statement
        for statement in kind_statements(ledger.schema)
        if "entity_widget" in {statement.name, statement.table}
    ]
    statements += [
        statement
        for statement in view_statements(ledger.schema)
        if statement.name == "v_entity_widget"
    ]
    assert statements, "the widgets preset produced no entity_widget DDL"
    ledger.driver.execute_many([statement.sql for statement in statements])
    ledger.driver.commit("test: create the widgets kind")


def _load_molbio_step():
    spec = importlib.util.spec_from_file_location("molbio_1_to_2_f1", MOLBIO_STEP)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_molbio_1_to_2_keeps_other_presets_in_the_target_schema_and_ledger(
    molbio_v1_ledger, script_env: dict[str, str], tmp_path: Path
) -> None:
    """F1b: a ledger loading another preset keeps its kind table and version.

    ``target_schema`` builds its preset list from the ledger's own list: molbio
    and base come from the repository v2/v1 files, the ``widgets`` preset (and
    its include closure) from ``<ledger>/presets/``.  The plan is additive, the
    ``entity_widget`` table (and its row) survives, and ``preset_versions``
    still records ``widgets``.
    """
    extra = _write_widgets_preset(tmp_path / "presets-src")
    ledger = molbio_v1_ledger
    # Give the copied fixture a second preset the way `tm init -p molbio -p
    # widgets` would have: its own copy under presets/ plus a ledger.yaml entry.
    widgets_copy = ledger.path / "presets" / "widgets.yaml"
    widgets_copy.write_bytes(extra.read_bytes())
    config_path = ledger.path / "ledger.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["presets"] = [*config["presets"], "widgets"]
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    reload_schema(ledger)
    _create_widgets_kind(ledger)
    # The legacy build recorded only its original presets; record the extra
    # preset's version so the ledger describes the presets it actually loads.
    ledger.driver.execute(
        "UPDATE `ledger_meta` SET `value` = %s WHERE `key` = 'preset_versions'",
        (json.dumps({"base": 1, "molbio": 1, "widgets": 1}),),
    )
    ledger.driver.commit("test: record the widgets preset version")
    ledger.read_meta()
    seed_molbio_v1(ledger)
    widget = ledger.entities.add("widget", name="W1", values={"size": 3})
    widgets_before = widgets_copy.read_bytes()
    assert widgets_before == extra.read_bytes()

    module = _load_molbio_step()
    target = module.target_schema(ledger)
    assert target.preset_versions == {"base": 2, "molbio": 2, "widgets": 1}
    assert "widget" in target.kinds
    schema_plan = plan(ledger, target, renames=module.RENAMES)
    assert schema_plan.destructive == []
    assert ("drop_table", "entity_widget") not in {
        (step.action, step.name) for step in schema_plan.steps
    }

    dry = run_python(
        MOLBIO_STEP, "--ledger", str(ledger.path), "--dry-run", env=script_env
    )
    assert dry.returncode == 0, dry.stdout + dry.stderr
    assert "DROP TABLE `entity_widget`" not in dry.stdout
    assert "entity_widget" not in dry.stdout  # not even as a planned rename

    head_before = head(ledger)
    applied = run_python(MOLBIO_STEP, "--ledger", str(ledger.path), env=script_env)
    assert applied.returncode == 0, applied.stdout + applied.stderr
    assert "migrate(ledger): molbio 1 -> 2" in applied.stdout
    assert head(ledger) != head_before
    # The step vendors molbio/base only; the other preset copy is untouched.
    assert widgets_copy.read_bytes() == widgets_before

    reopened = open_ledger(
        ledger.path, allow_legacy_statuses=True
    )
    try:
        assert "widget" in reopened.schema.kinds
        assert reopened.schema.preset_versions == {
            "base": 2,
            "molbio": 2,
            "widgets": 1,
        }
        # Only molbio's counter is bumped by this step.
        assert json.loads(reopened.meta["preset_versions"]) == {
            "base": 1,
            "molbio": 2,
            "widgets": 1,
        }
        assert "entity_widget" in table_names(reopened)
        assert reopened.driver.query(
            "SELECT `id`, `kind` FROM `entity` WHERE `kind` = 'widget'"
        ) == [{"id": widget["id"], "kind": "widget"}]
        assert reopened.driver.query("SELECT `size` FROM `entity_widget`") == [
            {"size": 3}
        ]
        assert_fingerprint_refreshed(reopened)
        assert reopened.check_fingerprint(warn=False) is True
        checked = tm_check_open(reopened)
        assert checked.exit_code == 0, checked.output
        assert "FAIL" not in checked.output
    finally:
        reopened.close()


# ---------------------------------------------------------------------------
# The checked-in fixture: drift guard
# ---------------------------------------------------------------------------


def fixture_shape(dolt: str, ledger: Path) -> dict[str, Any]:
    """The logical shape of a ledger: fingerprint, table/view list, test count."""

    def rows(sql: str) -> list[dict[str, Any]]:
        result = subprocess.run(
            [dolt, "sql", "--result-format", "json", "-q", sql],
            cwd=str(ledger),
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return json.loads(result.stdout).get("rows", [])

    meta = {
        str(row["key"]): str(row["value"])
        for row in rows("SELECT `key`, `value` FROM `ledger_meta`")
    }
    objects = rows(
        "SELECT `table_name` AS `name`, `table_type` AS `kind` "
        "FROM `information_schema`.`tables` WHERE `table_schema` = DATABASE()"
    )
    (generated,) = rows("SELECT COUNT(*) AS `n` FROM `dolt_tests`")
    return {
        "fingerprint": meta.get("fingerprint"),
        "tables": sorted(
            str(row["name"])
            for row in objects
            if str(row["kind"]).upper() == "BASE TABLE"
        ),
        "views": sorted(
            str(row["name"]) for row in objects if str(row["kind"]).upper() == "VIEW"
        ),
        "dolt_tests": int(generated["n"]),
    }


def _load_build_fixtures():
    spec = importlib.util.spec_from_file_location(
        "tetramark_build_fixtures", BUILD_FIXTURES
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def rebuilt_molbio_v1(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Rebuild the molbio-1 fixture once per module; skip when impossible.

    The build needs ``git``, ``uv`` and ``dolt``, the legacy commit in this
    clone, and either network or a warm uv cache.  A machine that cannot
    materialise the legacy checkout skips the drift guard instead of failing.
    """
    builder = _load_build_fixtures()
    missing = [name for name in ("git", "uv", "dolt") if shutil.which(name) is None]
    if missing:
        pytest.skip(f"cannot rebuild the molbio-1 fixture without: {', '.join(missing)}")
    probe = subprocess.run(
        [
            "git",
            "-C",
            str(ROOT),
            "cat-file",
            "-e",
            f"{builder.LEGACY_COMMIT}^{{commit}}",
        ],
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        pytest.skip(
            f"the legacy commit {builder.LEGACY_COMMIT[:12]} is not in this clone"
        )
    target = tmp_path_factory.mktemp("molbio-v1-rebuilt") / "molbio-v1"
    result = subprocess.run(
        [sys.executable, str(BUILD_FIXTURES), "--out", str(target)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        output = (result.stdout + result.stderr).strip()
        pytest.skip(f"cannot rebuild the molbio-1 fixture here:\n{output[-1200:]}")
    return target


def test_molbio_v1_fixture_matches_its_builder(
    rebuilt_molbio_v1: Path, dolt_binary: str, tmp_path: Path
) -> None:
    """The checked-in ledger still has the shape the builder produces.

    Compares the **logical** shape — schema fingerprint, table/view list and
    ``dolt_tests`` count — plus the checked-in preset files, never the ``.dolt``
    bytes (Dolt timestamps commit hashes).  The checked-in copy is queried from
    a temporary copy so the repository's fixture directory is never written to.
    """
    checked_in = copy_molbio_v1(tmp_path / "checked-in")
    checked = fixture_shape(dolt_binary, checked_in)
    fresh = fixture_shape(dolt_binary, rebuilt_molbio_v1)

    differences = [
        f"{key}: checked-in={checked[key]!r}, builder={fresh[key]!r}"
        for key in ("fingerprint", "tables", "views", "dolt_tests")
        if checked[key] != fresh[key]
    ]
    for name in ("ledger.yaml", "presets/base.yaml", "presets/molbio.yaml"):
        checked_file = MOLBIO_V1_FIXTURE / name
        if not checked_file.is_file():
            differences.append(f"{name}: missing from the checked-in fixture")
        elif checked_file.read_bytes() != (rebuilt_molbio_v1 / name).read_bytes():
            differences.append(f"{name}: differs from the builder's file byte for byte")

    assert not differences, (
        "the checked-in molbio-1 fixture no longer matches what "
        f"{BUILD_FIXTURES.relative_to(ROOT)} produces from the legacy checkout:\n"
        "  - "
        + "\n  - ".join(differences)
        + f"\nRebuild it with: uv run python "
        f"{BUILD_FIXTURES.relative_to(ROOT)} --force"
    )
    # ... and it is still the v1 shape the step needs, not an empty shell.
    assert checked["fingerprint"]
    assert {
        "entity_construct",
        "entity_strain",
        "stock_bacterial",
        "stock_pellet",
        "aliquot_bacterial",
    } <= set(checked["tables"])
    assert checked["dolt_tests"] > 0


# ---------------------------------------------------------------------------
# Runner hooks a preset step needs: target schema + post-commit files
# ---------------------------------------------------------------------------


def _make_base_v1_ledger(factory) -> Ledger:
    """A base-preset ledger whose ``preset_versions`` says base 1."""
    ledger = factory([PRESETS / "base"])
    ledger.driver.execute(
        "UPDATE `ledger_meta` SET `value` = '{\"base\": 1}' "
        "WHERE `key` = 'preset_versions'"
    )
    ledger.driver.commit("test: preset version 1")
    ledger.read_meta()
    return ledger


def test_after_commit_hook_runs_only_on_a_real_apply(
    embedded_ledger_factory, script_env: dict[str, str], tmp_path: Path
) -> None:
    """A preset-file hook is never part of a dry run, and reports its note."""
    ledger = _make_base_v1_ledger(embedded_ledger_factory)
    head_before = head(ledger)
    script = write_step(
        tmp_path,
        kind="preset",
        preset="base",
        from_version=1,
        statements=["UPDATE `alias` SET `alias` = `alias` WHERE 1 = 0"],
        hook_source=(
            "def after(ledger, result):\n"
            "    MARKER.write_text(str(result.commit_hash))\n"
            "    return 'wrote the marker file'\n"
        ),
        step_args=", after_commit=after, after_commit_note='write the marker file'",
    )

    dry = run_python(script, "--ledger", str(ledger.path), "--dry-run", env=script_env)
    assert dry.returncode == 0, dry.stdout + dry.stderr
    assert "after commit: write the marker file" in dry.stdout
    assert not (tmp_path / "marker.txt").exists()
    assert head(ledger) == head_before

    applied = run_python(script, "--ledger", str(ledger.path), env=script_env)
    assert applied.returncode == 0, applied.stdout + applied.stderr
    assert "post-commit: wrote the marker file" in applied.stdout
    assert (tmp_path / "marker.txt").read_text(encoding="utf-8") == head(ledger)


def test_failed_after_commit_hook_reports_that_the_commit_exists(
    embedded_ledger_factory, script_env: dict[str, str], tmp_path: Path
) -> None:
    """The commit cannot be undone by a file error, so the report must say so."""
    ledger = _make_base_v1_ledger(embedded_ledger_factory)
    head_before = head(ledger)
    commits_before = commit_count(ledger)
    script = write_step(
        tmp_path,
        kind="preset",
        preset="base",
        from_version=1,
        statements=["UPDATE `alias` SET `alias` = `alias` WHERE 1 = 0"],
        hook_source=(
            "def after(ledger, result):\n"
            "    raise RuntimeError('boom in the file step')\n"
        ),
        step_args=", after_commit=after",
    )

    result = run_python(script, "--ledger", str(ledger.path), env=script_env)
    assert result.returncode == 1, result.stdout + result.stderr
    output = result.stdout + result.stderr
    head_after = head(ledger)
    assert head_after != head_before
    assert commit_count(ledger) == commits_before + 1
    assert (
        f"commit {head_after} was made but the post-commit preset copy "
        "update failed" in output
    )
    assert "boom in the file step" in output
    assert "post-commit" in output
    # The Dolt side of the commit is complete: the counter was bumped.
    ledger.read_meta()
    assert json.loads(ledger.meta["preset_versions"]) == {"base": 2}


def test_post_commit_verification_failure_is_reported_on_stderr(
    embedded_ledger_factory, script_env: dict[str, str], tmp_path: Path
) -> None:
    """F5: a failure after the commit must name itself, not exit 1 silently.

    ``reopened_schema`` re-reads the ledger from its own preset copy; here the
    post-commit hook deliberately corrupts that copy, so the fingerprint the
    commit recorded no longer matches what the ledger opens with.
    """
    ledger = _make_base_v1_ledger(embedded_ledger_factory)
    head_before = head(ledger)
    commits_before = commit_count(ledger)
    script = write_step(
        tmp_path,
        kind="preset",
        preset="base",
        from_version=1,
        statements=["UPDATE `alias` SET `alias` = `alias` WHERE 1 = 0"],
        hook_source=(
            "from tetramark.schema.loader import load_ledger_schema, load_schema\n"
            "\n"
            "def reopened(ledger):\n"
            "    return load_ledger_schema(ledger.path)[1]\n"
            "\n"
            "def after(ledger, result):\n"
            "    kinds = ledger.path / 'presets' / 'base' / 'kinds'\n"
            "    kinds.mkdir(parents=True, exist_ok=True)\n"
            "    (kinds / 'late.yaml').write_text(\n"
            "        'kinds:\\n  late_entity:\\n    object: entity\\n'\n"
            "    )\n"
            "    return 'corrupted the preset copy on purpose'\n"
        ),
        step_args=", after_commit=after, reopened_schema=reopened",
    )

    result = run_python(script, "--ledger", str(ledger.path), env=script_env)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "committed" in result.stdout
    assert "post-commit: corrupted the preset copy on purpose" in result.stdout
    assert (
        "error: migration base 1 -> 2: the commit was made, but the ledger now "
        "opens with fingerprint" in result.stderr
    ), result.stderr
    # The commit exists (the report must not pretend otherwise).
    assert head(ledger) != head_before
    assert commit_count(ledger) == commits_before + 1
    ledger.read_meta()
    assert json.loads(ledger.meta["preset_versions"]) == {"base": 2}


def test_molbio_step_refuses_a_rename_map_that_names_a_missing_old_table(
    embedded_ledger_factory,
) -> None:
    """A typo in ``renames.yaml`` fails loudly before the planner runs.

    The step pre-checks every old kind table its rename map names (derived from
    the map, so a typo is what is checked), instead of letting the planner skip
    a rename whose old table does not exist.
    """
    ledger = embedded_ledger_factory([PRESETS / "base"])
    spec = importlib.util.spec_from_file_location("molbio_1_to_2", MOLBIO_STEP)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # "constructs" is the typo: the live table is entity_construct.
    module.RENAMES = RenameMap.from_dict(
        {
            "entity": {
                "constructs": "protein_construct",
                "strain": "bacterial_strain",
            },
            "stock": {
                "bacterial": "bacterial_glycerol",
                "pellet": "bacterial_pellet",
            },
        }
    )
    assert "entity_constructs" in {
        table_name(object_name, old_kind)
        for object_name, mapping in module.RENAMES.kind_maps()
        for old_kind in mapping
    }
    with pytest.raises(LedgerError, match="entity_constructs"):
        module.plan(ledger)


# ---------------------------------------------------------------------------
# spec/2-to-3: drop the extension tables, recreate views, regenerate tests
# ---------------------------------------------------------------------------

SPEC_2_TO_3 = ROOT / "migration" / "spec" / "2-to-3" / "migrate.py"

#: A stock view in its spec-2 shape: cored row + kind fields + alias_list only
#: (no entity/aliquot/quantity/status columns).  The step must recreate it.
SPEC2_STOCK_VIEW = (
    "CREATE VIEW `v_stock_lot` AS\n"
    "SELECT c.`id` AS `id`, c.`kind` AS `kind`, c.`entity_id` AS `entity_id`, "
    "k.`supplier` AS `supplier`, "
    "(SELECT GROUP_CONCAT(`alias` ORDER BY `alias` SEPARATOR ';') FROM `alias` "
    "WHERE `object_type` = 'stock' AND `object_id` = c.`id`) AS `alias_list`\n"
    "FROM `stock_lot` k JOIN `stock` c ON c.`id` = k.`id`"
)


def spec2_ledger(factory, *, populated: bool):
    """A real ledger downgraded to the spec-2 shape, with one extension table.

    The extension feature is gone, so no preset can create ``aliquot_lot``: the
    fixture is a normal init followed by hand SQL, as the ticket allows.
    ``dolt_tests`` is emptied as well, so the step's regenerated tests are
    visibly restored.
    """
    ledger = factory([LEGACY_BASE_V4], name="spec2", id_prefix="77")
    item = ledger.entities.add("item", name="alpha")
    stock = ledger.stocks.add(
        "lot", entity=item["id"], values={"supplier": "acme"}, aliquots=2
    )
    ledger.driver.execute("DROP VIEW `v_stock_lot`")
    ledger.driver.execute(SPEC2_STOCK_VIEW)
    ledger.driver.execute(
        "CREATE TABLE `aliquot_lot` ("
        "`id` VARCHAR(64) NOT NULL, `volume_ul` DECIMAL(10,1), "
        "CONSTRAINT `pk_aliquot_lot` PRIMARY KEY (`id`), "
        "CONSTRAINT `fk_aliquot_lot_id` FOREIGN KEY (`id`) "
        "REFERENCES `aliquot` (`id`))"
    )
    if populated:
        ledger.driver.execute(
            "INSERT INTO `aliquot_lot` (`id`, `volume_ul`) VALUES (%s, 7.5)",
            (stock["aliquots"][0],),
        )
    ledger.driver.execute("DELETE FROM `dolt_tests`")
    ledger.driver.execute(
        "UPDATE `ledger_meta` SET `value` = '2' WHERE `key` = 'spec_version'"
    )
    ledger.driver.commit("test: simulate a spec-2 ledger")
    ledger.read_meta()
    return ledger, stock


def test_spec_2_to_3_drops_the_empty_extension_and_recreates_the_views(
    ledger_factory, script_env: dict[str, str]
) -> None:
    """Verify bullet 3: drop all-NULL extension, spec-3 views, one commit, tag."""
    ledger, _stock = spec2_ledger(ledger_factory, populated=False)
    assert "aliquot_lot" in table_names(ledger)
    assert "aliquots_in_stock" not in column_names(ledger, "v_stock_lot")
    assert row_count(ledger, "dolt_tests") == 0

    head_before = head(ledger)
    commits_before = commit_count(ledger)
    view_count = len(view_statements(ledger.schema))

    applied = run_python(SPEC_2_TO_3, "--ledger", str(ledger.path), env=script_env)
    assert applied.returncode == 0, applied.stdout + applied.stderr
    assert "migration: spec 2 -> 3" in applied.stdout
    assert "CALL DOLT_TAG('pre-migrate-spec-2'" in applied.stdout
    assert "DROP TABLE IF EXISTS `aliquot_lot`" in applied.stdout
    assert applied.stdout.count("CREATE VIEW") == view_count
    assert "dolt tests: replace with" in applied.stdout
    assert "migrate(ledger): spec 2 -> 3" in applied.stdout

    ledger.read_meta()
    # The extension table is gone; the stock view has the spec-3 columns.
    assert "aliquot_lot" not in table_names(ledger)
    columns = column_names(ledger, "v_stock_lot")
    assert set(STOCK_VIEW_COLUMNS) <= columns
    row = ledger.driver.query(
        "SELECT aliquots_in_stock, aliquots_total, status FROM `v_stock_lot`"
    )[0]
    # They were created without a box, so they are pending, not in stock.
    assert row["aliquots_in_stock"] == 0
    assert row["aliquots_total"] == 2
    assert row["status"] == "depleted"

    # Counter, fingerprint and the regenerated derived tests, in one commit.
    assert ledger.meta["spec_version"] == "3"
    assert ledger.meta["tetramark_version"] == __version__
    assert ledger.meta["fingerprint"] == ledger.schema.fingerprint()
    assert row_count(ledger, "dolt_tests") == expected_installed_tests(ledger)
    assert row_count(ledger, "dolt_tests") > 0
    stale = ledger.driver.query(
        "SELECT COUNT(*) AS `n` FROM `dolt_tests` "
        "WHERE `test_query` LIKE '%aliquot_lot%'"
    )
    assert stale == [{"n": 0}]

    assert commit_count(ledger) == commits_before + 1
    head_after = head(ledger)
    assert commit_message(ledger, head_after) == "migrate(ledger): spec 2 -> 3"
    assert tags(ledger) == {"pre-migrate-spec-2": head_before}

    checked = tm_check(ledger)
    assert checked.exit_code == 0, checked.output
    assert "FAIL" not in checked.output
    assert "OK:" in checked.output

    # A second run refuses: the counter is 3, not 2.
    again = run_python(SPEC_2_TO_3, "--ledger", str(ledger.path), env=script_env)
    assert again.returncode == 1, again.stdout + again.stderr
    assert "refusing to run migration spec 2 -> 3" in again.stdout + again.stderr
    assert "spec_version 3, expected 2" in again.stdout + again.stderr
    assert head(ledger) == head_after


def test_spec_2_to_3_dry_run_writes_nothing(
    embedded_ledger_factory, script_env: dict[str, str]
) -> None:
    """Verify bullet 3: ``--dry-run`` prints the plan and writes nothing."""
    ledger, _stock = spec2_ledger(embedded_ledger_factory, populated=False)
    head_before = head(ledger)
    commits_before = commit_count(ledger)
    view_count = len(view_statements(ledger.schema))

    result = run_python(
        SPEC_2_TO_3, "--ledger", str(ledger.path), "--dry-run", env=script_env
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "dry run:" in result.stdout
    assert "DROP TABLE IF EXISTS `aliquot_lot`" in result.stdout
    assert result.stdout.count("CREATE VIEW") == view_count
    assert result.stdout.count("DROP VIEW IF EXISTS") == view_count
    assert "'spec_version', '3'" in result.stdout

    assert head(ledger) == head_before
    assert commit_count(ledger) == commits_before
    assert tags(ledger) == {}
    ledger.read_meta()
    assert ledger.meta["spec_version"] == "2"
    assert "aliquot_lot" in table_names(ledger)
    assert "aliquots_in_stock" not in column_names(ledger, "v_stock_lot")


def test_spec_2_to_3_refuses_a_populated_extension_and_writes_nothing(
    embedded_ledger_factory, script_env: dict[str, str]
) -> None:
    """Verify bullet 3: a populated extension table is listed, nothing is written."""
    ledger, _stock = spec2_ledger(embedded_ledger_factory, populated=True)
    head_before = head(ledger)
    commits_before = commit_count(ledger)

    for flags in (["--dry-run"], []):
        result = run_python(
            SPEC_2_TO_3, "--ledger", str(ledger.path), *flags, env=script_env
        )
        assert result.returncode == 1, result.stdout + result.stderr
        output = result.stdout + result.stderr
        assert "refusing to migrate spec 2 -> 3" in output
        # The offending table and column are named, with the value it holds.
        assert "aliquot_lot.volume_ul" in output
        assert "7.5" in output
        assert "Move those properties to the stock kind first" in output
        # Nothing was written: no commit, no tag, no DDL, counter unchanged.
        assert head(ledger) == head_before
        assert commit_count(ledger) == commits_before
        assert tags(ledger) == {}
        ledger.read_meta()
        assert ledger.meta["spec_version"] == "2"
        assert "aliquot_lot" in table_names(ledger)
        assert row_count(ledger, "aliquot_lot") == 1
        assert "aliquots_in_stock" not in column_names(ledger, "v_stock_lot")


# ---------------------------------------------------------------------------
# spec/3-to-4: per-stock-kind status sets, forced events, in-stock views
# ---------------------------------------------------------------------------

SPEC_3_TO_4 = ROOT / "migration" / "spec" / "3-to-4" / "migrate.py"
BASE_1_TO_2 = ROOT / "migration" / "presets" / "base" / "1-to-2" / "migrate.py"
LIBRARY_1_TO_2 = ROOT / "migration" / "presets" / "library" / "1-to-2" / "migrate.py"

SPEC3_STATUSES = ["pending", "stored", "missing", "consumed"]


def spec3_ledger(factory, *, statuses: list[str] | None = None):
    """A real spec-4 ledger downgraded to the spec-3 generated shape.

    The presets stay loadable (they already carry ``status_sets``), so the
    fixture is a normal init followed by hand SQL plus the legacy ``statuses:``
    key in ``ledger.yaml`` and its ``ledger_meta`` copy, as the ticket allows.
    The old views are dropped (the step recreates every generated view) and the
    spec-3 status CHECK is put back so the step has something to drop.
    """
    ledger = factory([LEGACY_BASE_V4], name="spec3", id_prefix="77")
    item = ledger.entities.add("item", name="alpha")
    stock = ledger.stocks.add(
        "lot",
        entity=item["id"],
        values={"supplier": "acme"},
        aliquots=2,
        quantity=1,
        unit="ml",
    )
    for statement in view_statements(ledger.schema):
        ledger.driver.execute(f"DROP VIEW `{statement.name}`")
    ledger.driver.execute("ALTER TABLE `aliquot_event` DROP COLUMN `forced`")
    ledger.driver.execute(
        "ALTER TABLE `aliquot` ADD CONSTRAINT `ck_aliquot_status` "
        "CHECK (`status` IN ('pending', 'stored', 'missing', 'consumed'))"
    )
    config_path = ledger.path / "ledger.yaml"
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    payload["statuses"] = list(statuses or SPEC3_STATUSES)
    text = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
    config_path.write_text(text, encoding="utf-8")
    ledger.driver.execute(
        "UPDATE `ledger_meta` SET `value` = %s WHERE `key` = 'ledger_yaml'",
        (text,),
    )
    ledger.driver.execute(
        "UPDATE `ledger_meta` SET `value` = '3' WHERE `key` = 'spec_version'"
    )
    ledger.driver.commit("test: simulate a spec-3 ledger")
    ledger.read_meta()
    return ledger, stock


def config_payload(ledger: Ledger) -> dict[str, Any]:
    return yaml.safe_load((ledger.path / "ledger.yaml").read_text(encoding="utf-8"))


def test_spec_3_to_4_migrates_statuses_forced_and_views(
    ledger_factory, script_env: dict[str, str]
) -> None:
    ledger, _stock = spec3_ledger(ledger_factory)
    assert "statuses" in config_payload(ledger)
    assert "forced" not in column_names(ledger, "aliquot_event")

    head_before = head(ledger)
    commits_before = commit_count(ledger)
    view_count = len(view_statements(ledger.schema))

    dry = run_python(SPEC_3_TO_4, "--ledger", str(ledger.path), "--dry-run", env=script_env)
    assert dry.returncode == 0, dry.stdout + dry.stderr
    assert "migration: spec 3 -> 4" in dry.stdout
    assert "CALL DOLT_TAG('pre-migrate-spec-3'" in dry.stdout
    assert "ADD COLUMN `forced`" in dry.stdout
    assert "DROP CHECK `ck_aliquot_status`" in dry.stdout
    assert dry.stdout.count("CREATE VIEW") == view_count
    assert "'spec_version', '4'" in dry.stdout
    # Dry run writes nothing — including the legacy key in ledger.yaml.
    assert "statuses" in config_payload(ledger)
    assert head(ledger) == head_before
    assert commit_count(ledger) == commits_before
    assert tags(ledger) == {}

    applied = run_python(SPEC_3_TO_4, "--ledger", str(ledger.path), env=script_env)
    assert applied.returncode == 0, applied.stdout + applied.stderr
    assert "migrate(ledger): spec 3 -> 4" in applied.stdout

    ledger.read_meta()
    assert ledger.meta["spec_version"] == "4"
    assert ledger.meta["tetramark_version"] == __version__
    assert ledger.meta["fingerprint"] == ledger.schema.fingerprint()
    assert "statuses" not in config_payload(ledger)
    assert "statuses" not in (yaml.safe_load(ledger.meta["ledger_yaml"]) or {})
    assert "forced" in column_names(ledger, "aliquot_event")
    assert "aliquots_in_stock" in column_names(ledger, "v_stock_lot")
    assert row_count(ledger, "dolt_tests") == expected_installed_tests(ledger)

    checks = ledger.driver.query(
        "SELECT `constraint_name` AS `name` FROM "
        "`information_schema`.`table_constraints` "
        "WHERE `table_schema` = DATABASE() AND `table_name` = 'aliquot' "
        "AND `constraint_type` = 'CHECK'"
    )
    assert checks == []
    assert commit_count(ledger) == commits_before + 1
    assert commit_message(ledger, head(ledger)) == "migrate(ledger): spec 3 -> 4"
    assert tags(ledger) == {"pre-migrate-spec-3": head_before}

    checked = tm_check(ledger)
    assert checked.exit_code == 0, checked.output
    assert "FAIL" not in checked.output

    again = run_python(SPEC_3_TO_4, "--ledger", str(ledger.path), env=script_env)
    assert again.returncode == 1, again.stdout + again.stderr
    assert "spec_version 4, expected 3" in again.stdout + again.stderr


def test_spec_3_to_4_refuses_a_custom_status_list_and_writes_nothing(
    embedded_ledger_factory, script_env: dict[str, str]
) -> None:
    custom = ["pending", "stored", "quarantined"]
    ledger, _stock = spec3_ledger(embedded_ledger_factory, statuses=custom)
    head_before = head(ledger)
    commits_before = commit_count(ledger)
    config_before = (ledger.path / "ledger.yaml").read_bytes()

    for flags in (["--dry-run"], []):
        result = run_python(
            SPEC_3_TO_4, "--ledger", str(ledger.path), *flags, env=script_env
        )
        assert result.returncode == 1, result.stdout + result.stderr
        output = result.stdout + result.stderr
        assert "refusing to migrate spec 3 -> 4" in output
        assert "quarantined" in output
        assert "status_set" in output
        assert "nothing was written" in output
        # Nothing was written: no commit, no tag, ledger.yaml byte-identical.
        assert head(ledger) == head_before
        assert commit_count(ledger) == commits_before
        assert tags(ledger) == {}
        assert (ledger.path / "ledger.yaml").read_bytes() == config_before


# ---------------------------------------------------------------------------
# presets/base/1-to-2 and presets/library/1-to-2
# ---------------------------------------------------------------------------


def test_base_1_to_2_bumps_the_counter_and_recreates_the_views(
    ledger_factory, script_env: dict[str, str]
) -> None:
    ledger = ledger_factory([BASE_V3], name="base-v1", id_prefix="77")
    item = ledger.entities.add("item", name="alpha")
    ledger.stocks.add("lot", entity=item["id"], values={"supplier": "acme"})
    # Pretend the ledger's metadata still records base 1 (the copy is current).
    ledger.driver.execute(
        "UPDATE `ledger_meta` SET `value` = %s WHERE `key` = 'preset_versions'",
        (json.dumps({"base": 1}),),
    )
    ledger.driver.commit("test: record the old base counter")
    ledger.read_meta()

    head_before = head(ledger)
    commits_before = commit_count(ledger)
    # The step installs the frozen base v2, so the plan recreates v2's views
    # (one box kind fewer than the base v3 copy this ledger started on).
    view_count = len(view_statements(base_v2_schema()))

    dry = run_python(BASE_1_TO_2, "--ledger", str(ledger.path), "--dry-run", env=script_env)
    assert dry.returncode == 0, dry.stdout + dry.stderr
    assert "migration: base 1 -> 2" in dry.stdout
    assert dry.stdout.count("CREATE VIEW") == view_count
    assert head(ledger) == head_before
    assert tags(ledger) == {}

    applied = run_python(BASE_1_TO_2, "--ledger", str(ledger.path), env=script_env)
    assert applied.returncode == 0, applied.stdout + applied.stderr
    assert "migrate(ledger): base 1 -> 2" in applied.stdout

    reload_schema(ledger)
    assert json.loads(ledger.meta["preset_versions"]) == {"base": 2}
    assert ledger.schema.preset_versions == {"base": 2}
    assert ledger.meta["fingerprint"] == ledger.schema.fingerprint()
    assert (ledger.path / "presets" / "base.yaml").read_bytes() == BASE_V2.read_bytes()
    assert commit_count(ledger) == commits_before + 1
    assert tags(ledger) == {"pre-migrate-base-1": head_before}
    checked = tm_check(ledger)
    assert checked.exit_code == 0, checked.output

    again = run_python(BASE_1_TO_2, "--ledger", str(ledger.path), env=script_env)
    assert again.returncode == 1, again.stdout + again.stderr
    assert "preset_versions[base] 2, expected 1" in again.stdout + again.stderr


def write_library_v1_presets(directory: Path) -> Path:
    """Materialise a library-v1 closure on the frozen base v3.

    The repository's ``presets/base.yaml`` is v5 since Tetramark-epn.4 (spec 6),
    so a ledger that reproduces the wave-10 content must start from the frozen
    base v3 copy the preset steps install.  The library copy is the frozen v2
    the step's own counter stops at (the repository copy is v3).
    """
    directory.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(BASE_V3, directory / "base.yaml")
    shutil.copyfile(FROZEN_LIBRARY_2, directory / "library.yaml")
    return directory / "library.yaml"


def test_library_1_to_2_bumps_its_counter_after_base(
    embedded_ledger_factory, script_env: dict[str, str], tmp_path: Path
) -> None:
    ledger = embedded_ledger_factory(
        [write_library_v1_presets(tmp_path / "library-v1-presets")],
        name="library-v1",
        id_prefix="77",
    )
    ledger.driver.execute(
        "UPDATE `ledger_meta` SET `value` = %s WHERE `key` = 'preset_versions'",
        (json.dumps({"base": 2, "library": 1}),),
    )
    ledger.driver.commit("test: record the old library counter")
    ledger.read_meta()

    applied = run_python(LIBRARY_1_TO_2, "--ledger", str(ledger.path), env=script_env)
    assert applied.returncode == 0, applied.stdout + applied.stderr
    assert "migrate(ledger): library 1 -> 2" in applied.stdout
    ledger.read_meta()
    assert json.loads(ledger.meta["preset_versions"]) == {
        "base": 2,
        "library": 2,
    }
    assert ledger.schema.statuses_for("copy")[1] == "stored"
    # The step installs the frozen base v3, not the repository's v4 copy.
    assert (ledger.path / "presets" / "base.yaml").read_bytes() == BASE_V3.read_bytes()


def test_preset_step_refuses_before_spec_4(
    embedded_ledger_factory, script_env: dict[str, str]
) -> None:
    ledger = embedded_ledger_factory(
        [REPO_PRESETS / "base.yaml"], name="base-spec3", id_prefix="77"
    )
    ledger.driver.execute(
        "UPDATE `ledger_meta` SET `value` = '3' WHERE `key` = 'spec_version'"
    )
    ledger.driver.execute(
        "UPDATE `ledger_meta` SET `value` = %s WHERE `key` = 'preset_versions'",
        (json.dumps({"base": 1}),),
    )
    ledger.driver.commit("test: spec 3 with an old base counter")
    ledger.read_meta()
    head_before = head(ledger)

    result = run_python(BASE_1_TO_2, "--ledger", str(ledger.path), env=script_env)
    assert result.returncode == 1, result.stdout + result.stderr
    output = result.stdout + result.stderr
    assert "refusing to migrate base 1 -> 2" in output
    assert "migration/spec/3-to-4" in output
    assert head(ledger) == head_before


def test_spec_3_to_4_refuses_an_aliquot_status_outside_its_stock_kind_set(
    embedded_ledger_factory, script_env: dict[str, str]
) -> None:
    ledger, stock = spec3_ledger(embedded_ledger_factory)
    alq = stock["aliquots"][0]
    # A real row whose status is not in the set the upgraded preset declares.
    ledger.driver.execute(
        "UPDATE `aliquot` SET `status` = 'missing' WHERE `id` = %s", (alq,)
    )
    ledger.driver.commit("test: a status the new set no longer knows")
    ledger.read_meta()
    # Remove ``missing`` from the ledger's own tube copy (as a wave-10 local
    # preset edit would); the step opens that copy, so the row is out of set.
    preset_copy = ledger.path / "presets" / "base.yaml"
    document = yaml.safe_load(preset_copy.read_text(encoding="utf-8"))
    document["status_sets"]["tube"].pop("missing")
    document["status_sets"]["tube"]["stored"]["to"] = ["consumed"]
    document["status_sets"]["tube"]["pending"]["to"] = ["stored", "consumed"]
    preset_copy.write_text(
        yaml.safe_dump(document, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    head_before = head(ledger)
    commits_before = commit_count(ledger)

    result = run_python(SPEC_3_TO_4, "--ledger", str(ledger.path), env=script_env)
    assert result.returncode == 1, result.stdout + result.stderr
    output = result.stdout + result.stderr
    assert "refusing to migrate spec 3 -> 4" in output
    assert f"{alq} (lot): missing" in output
    assert "Nothing was written" in output
    assert head(ledger) == head_before
    assert commit_count(ledger) == commits_before
    assert tags(ledger) == {}
    # The legacy ledger.yaml was restored too.
    assert "statuses" in config_payload(ledger)


# ---------------------------------------------------------------------------
# Ticket 27: molbio 2 -> 3 (a bundle of parts) and base 2 -> 3 (incubator)
# ---------------------------------------------------------------------------

MOLBIO_2_TO_3 = ROOT / "migration" / "presets" / "molbio" / "2-to-3" / "migrate.py"
MOLBIO_2_TO_3_DIR = MOLBIO_2_TO_3.parent
BASE_2_TO_3 = ROOT / "migration" / "presets" / "base" / "2-to-3" / "migrate.py"
CELL_CULTURE_1_TO_2 = (
    ROOT / "migration" / "presets" / "cell_culture" / "1-to-2" / "migrate.py"
)
CELL_CULTURE_1_TO_2_DIR = CELL_CULTURE_1_TO_2.parent
#: Ticket 28 froze the cell_culture v1 content in both step folders: molbio 2 -> 3
#: installs it (its counter stops at 1), cell_culture 1 -> 2 does not.
CELL_CULTURE_V1 = (
    ROOT / "migration" / "presets" / "cell_culture" / "1-to-2" / "cell_culture-1.yaml"
)
CELL_CULTURE_V2 = (
    ROOT / "migration" / "presets" / "cell_culture" / "1-to-2" / "cell_culture-2.yaml"
)

#: What a molbio-v2 ledger records before ticket 27's step.
MOLBIO_V2_VERSIONS = {"base": 2, "molbio": 2}

#: What the molbio 2 -> 3 step records: the bundle plus one key per part.
#: ``base`` stays at 2 (its own step bumps it), exactly like molbio 1 -> 2.
MOLBIO_V3_VERSIONS = {
    "bacteria": 1,
    "base": 2,
    "cell_culture": 1,
    "molbio": 3,
    "nucleic_acids": 1,
    "proteins": 1,
}

#: Every preset copy a molbio ledger needs after the step.  ``cell_culture`` is
#: the frozen v1 (ticket 28 bumped the repository copy to v2), ``bacteria`` the
#: frozen v1 (Tetramark-epn.4 bumped the repository copy to v2), ``proteins`` /
#: ``nucleic_acids`` are the frozen v1s (ticket rq9 bumped the repository copies
#: to v2) and ``base`` is the frozen v3 (the repository copy is v4 with the
#: ``grid96`` -> ``grid8x12`` rename since spec 5); all are compared separately
#: from repository content.
MOLBIO_V3_FILES = (
    "molbio.yaml",
    *[
        f"{part}.yaml"
        for part in PARTS_V3
        if part not in ("bacteria", "cell_culture", "proteins", "nucleic_acids")
    ],
)


def write_molbio_v2_presets(directory: Path) -> Path:
    """Materialise the frozen v2 pair under the flat names ``tm init`` copies."""
    directory.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(MOLBIO_V2, directory / "molbio.yaml")
    shutil.copyfile(BASE_V2, directory / "base.yaml")
    return directory / "molbio.yaml"


def molbio_v2_ledger(
    factory, directory: Path, *, name: str = "molbio-v2"
) -> Ledger:
    """A real molbio-v2 ledger, built from the frozen v2 presets.

    The repository's ``presets/`` holds v3, so this is the supported way to
    reproduce what a 0.2.0 ledger carries; the step under test is the only
    thing that may move it to v3.
    """
    ledger = factory([write_molbio_v2_presets(directory)], name=name, real_init=True)
    assert ledger.schema.preset_versions == MOLBIO_V2_VERSIONS
    assert json.loads(ledger.meta["preset_versions"]) == MOLBIO_V2_VERSIONS
    return ledger


def seed_molbio_v2(ledger: Ledger) -> dict[str, Any]:
    """One row per v2 entity kind and every stock kind, with aliquots."""
    construct = ledger.entities.add(
        "protein_construct",
        name="c01",
        values={"sequence": "ATGC", "molecular_weight_da": 12345.67},
    )
    plasmid = ledger.entities.add(
        "plasmid",
        name="p01",
        values={
            "sequence": "ATGG",
            "construct": construct["id"],
            "resistance": "amp",
        },
    )
    strain = ledger.entities.add(
        "bacterial_strain", name="DH5a", values={"genotype": "F- endA1"}
    )
    line = ledger.entities.add(
        "cell_line", name="HEK293T", values={"species": "Homo sapiens"}
    )
    # Spec 5: an in-stock aliquot has a box position, so the seed places its
    # tubes on one slotless shelf (the v2 schema has no per-box views either).
    ledger.boxes.add("shelf", "SEED-SHELF")
    glycerol = ledger.stocks.add(
        "bacterial_glycerol",
        entity=strain["id"],
        values={"strain": "DH5a", "medium": "LB", "glycerol_pct": 25},
        aliquots=2,
        box="SEED-SHELF",
        quantity=500,
        unit="ul",
    )
    prep = ledger.stocks.add(
        "plasmid_prep",
        entity=plasmid["id"],
        values={"concentration_ng_ul": 120.5, "prep_method": "miniprep"},
        aliquots=1,
        box="SEED-SHELF",
        quantity=50,
        unit="ul",
    )
    protein = ledger.stocks.add(
        "protein_prep",
        entity=construct["id"],
        values={"concentration_mg_ml": 1.5},
        aliquots=1,
        box="SEED-SHELF",
        quantity=100,
        unit="ul",
    )
    pellet = ledger.stocks.add(
        "bacterial_pellet",
        entity=plasmid["id"],
        values={"culture_volume_ml": 500, "od600": 2.4},
        aliquots=2,
        box="SEED-SHELF",
        quantity=250,
        unit="ml",
    )
    vial = ledger.stocks.add(
        "cell_vial",
        entity=line["id"],
        values={"passage": 7, "dmso_pct": 10},
        aliquots=1,
        box="SEED-SHELF",
        quantity=1,
        unit="vial",
    )
    cell_pellet = ledger.stocks.add(
        "cell_pellet",
        entity=line["id"],
        values={"cell_count": 1_000_000, "passage": 8},
        aliquots=1,
        box="SEED-SHELF",
        quantity=1,
        unit="tube",
    )
    ledger.aliases.add("stock", glycerol["id"], "OLD-GLY")
    return {
        "construct": construct,
        "plasmid": plasmid,
        "strain": strain,
        "line": line,
        "glycerol": glycerol,
        "prep": prep,
        "protein": protein,
        "pellet": pellet,
        "vial": vial,
        "cell_pellet": cell_pellet,
    }


#: Every table a molbio-v2 ledger has; the step must keep all of them.
MOLBIO_TABLES = (
    "entity",
    "stock",
    "aliquot",
    "alias",
    "entity_protein_construct",
    "entity_plasmid",
    "entity_bacterial_strain",
    "entity_cell_line",
    "stock_bacterial_glycerol",
    "stock_plasmid_prep",
    "stock_protein_prep",
    "stock_bacterial_pellet",
    "stock_cell_vial",
    "stock_cell_pellet",
)


def molbio_row_counts(ledger: Ledger) -> dict[str, int]:
    return row_counts(ledger, MOLBIO_TABLES)


def downgrade_to_spec3(ledger: Ledger) -> None:
    """Turn a spec-4 v2 ledger into the spec-3 shape the spec step upgrades.

    Same downgrade the spec/3-to-4 tests use: drop the generated views, remove
    ``aliquot_event.forced``, restore the global status CHECK, put the default
    ``statuses:`` list back into ``ledger.yaml`` (and its meta copy) and stamp
    ``spec_version = 3``.
    """
    driver = ledger.driver
    statements: list[str | tuple[str, Any]] = [
        f"DROP VIEW `{statement.name}`"
        for statement in view_statements(ledger.schema)
    ]
    statements.extend(
        [
            "ALTER TABLE `aliquot_event` DROP COLUMN `forced`",
            "ALTER TABLE `aliquot` ADD CONSTRAINT `ck_aliquot_status` "
            "CHECK (`status` IN ('pending', 'stored', 'missing', 'consumed'))",
        ]
    )
    config_path = ledger.path / "ledger.yaml"
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    payload["statuses"] = list(SPEC3_STATUSES)
    text = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
    config_path.write_text(text, encoding="utf-8")
    statements.extend(
        [
            (
                "UPDATE `ledger_meta` SET `value` = %s WHERE `key` = 'ledger_yaml'",
                (text,),
            ),
            "UPDATE `ledger_meta` SET `value` = '3' WHERE `key` = 'spec_version'",
        ]
    )
    driver.execute_many(statements)
    driver.commit("test: simulate a spec-3 molbio-v2 ledger")
    ledger.read_meta()


def test_molbio_2_to_3_adds_the_oligo_tables_and_keeps_every_row(
    ledger_factory, script_env: dict[str, str], tmp_path: Path
) -> None:
    """Verify bullet 3, both drivers: dry run, one commit, rows kept, check."""
    ledger = molbio_v2_ledger(ledger_factory, tmp_path / "molbio-v2-presets")
    seeded = seed_molbio_v2(ledger)
    counts_before = molbio_row_counts(ledger)
    assert counts_before["aliquot"] == 8
    copies_before = {
        name: (ledger.path / "presets" / name).read_bytes()
        for name in ("molbio.yaml", "base.yaml")
    }
    head_before = head(ledger)
    commits_before = commit_count(ledger)

    # -- dry run: the oligo DDL, the tag, the part counters; no writes -----
    dry = run_python(
        MOLBIO_2_TO_3, "--ledger", str(ledger.path), "--dry-run", env=script_env
    )
    assert dry.returncode == 0, dry.stdout + dry.stderr
    out = dry.stdout
    assert "migration: molbio 2 -> 3" in out
    assert "CALL DOLT_TAG('pre-migrate-molbio-2'" in out
    assert "CREATE TABLE `entity_oligo`" in out
    assert "CREATE TABLE `stock_oligo_stock`" in out
    assert "CREATE TABLE `box_incubator`" in out
    assert "\"bacteria\": 1" in out and "\"nucleic_acids\": 1" in out
    assert "\"molbio\": 3" in out
    assert "dolt tests: replace with" in out

    assert head(ledger) == head_before
    assert commit_count(ledger) == commits_before
    assert tags(ledger) == {}
    assert "entity_oligo" not in table_names(ledger)
    for name, data in copies_before.items():
        assert (ledger.path / "presets" / name).read_bytes() == data, name

    # -- apply: one commit, every counter, every row -----------------------
    applied = run_python(MOLBIO_2_TO_3, "--ledger", str(ledger.path), env=script_env)
    assert applied.returncode == 0, applied.stdout + applied.stderr
    assert "committed" in applied.stdout
    assert "migrate(ledger): molbio 2 -> 3" in applied.stdout

    reopened = open_ledger(ledger.path, allow_legacy_statuses=True)
    try:
        assert_fingerprint_refreshed(reopened)
        assert reopened.check_fingerprint(warn=False) is True
        assert json.loads(reopened.meta["preset_versions"]) == MOLBIO_V3_VERSIONS
        assert reopened.schema.preset_versions == {
            **MOLBIO_V3_VERSIONS,
            "base": 3,  # the placed copy is base v3; its counter is the base step's
        }

        tables = table_names(reopened)
        for table in (
            "entity_oligo",
            "stock_oligo_stock",
            "v_entity_oligo",
            "v_stock_oligo_stock",
            "box_incubator",
        ):
            assert table in tables, table
        # Spec 5: box kinds have a table but no v_box_<kind> view.
        assert "v_box_incubator" not in tables
        assert molbio_row_counts(reopened) == counts_before
        assert reopened.driver.query(
            "SELECT `alias` FROM `alias` WHERE `object_type` = 'stock' "
            "AND `object_id` = %s",
            (seeded["glycerol"]["id"],),
        ) == [{"alias": "OLD-GLY"}]

        # The ledger's copy is this checkout's v3 content, byte for byte -
        # except cell_culture and base, which the step froze at the versions
        # whose era it belongs to (ticket 28's bump is
        # migration/presets/cell_culture/1-to-2; spec 5's base v4 rename is
        # base/3-to-4).
        for name in MOLBIO_V3_FILES:
            assert (reopened.path / "presets" / name).read_bytes() == (
                REPO_PRESETS / name
            ).read_bytes(), name
        assert (reopened.path / "presets" / "base.yaml").read_bytes() == (
            BASE_V3.read_bytes()
        )
        assert (reopened.path / "presets" / "cell_culture.yaml").read_bytes() == (
            ROOT / "migration" / "presets" / "molbio" / "2-to-3"
            / "cell_culture-1.yaml"
        ).read_bytes()
        assert (reopened.path / "presets" / "cell_culture.yaml").read_bytes() == (
            CELL_CULTURE_V1.read_bytes()
        )
        # Tetramark-epn.4 bumps the repository bacteria copy to v2; this step
        # records bacteria 1, so it ships the frozen v1.
        assert (reopened.path / "presets" / "bacteria.yaml").read_bytes() == (
            MOLBIO_2_TO_3_DIR / "bacteria-1.yaml"
        ).read_bytes()
        # Ticket rq9's parts stay at v1: the step ships their frozen copies.
        assert (reopened.path / "presets" / "proteins.yaml").read_bytes() == (
            MOLBIO_2_TO_3_DIR / "proteins-1.yaml"
        ).read_bytes()
        assert (reopened.path / "presets" / "nucleic_acids.yaml").read_bytes() == (
            MOLBIO_2_TO_3_DIR / "nucleic_acids-1.yaml"
        ).read_bytes()

        # One commit with the contract message, tagged at the pre-step commit.
        assert commit_count(reopened) == commits_before + 1
        head_after = head(reopened)
        assert head_after != head_before
        assert commit_message(reopened, head_after) == "migrate(ledger): molbio 2 -> 3"
        assert tags(reopened) == {"pre-migrate-molbio-2": head_before}

        # tm check runs the regenerated v3 tests; the schema plan is empty.
        checked = tm_check(reopened)
        assert checked.exit_code == 0, checked.output
        assert "FAIL" not in checked.output
        assert "OK:" in checked.output
        planned = CliRunner().invoke(app, [*base_args(reopened), "schema", "plan"])
        assert planned.exit_code == 0, planned.output
        assert "up to date" in planned.output
    finally:
        reopened.close()

    # -- a second run refuses (molbio is already 3) ------------------------
    again = run_python(MOLBIO_2_TO_3, "--ledger", str(ledger.path), env=script_env)
    assert again.returncode == 1, again.stdout + again.stderr
    output = again.stdout + again.stderr
    assert "refusing to run migration molbio 2 -> 3" in output
    assert "preset_versions[molbio] 3, expected 2" in output
    assert head(ledger) == head_after


def test_base_2_to_3_adds_the_incubator_kind(
    embedded_ledger_factory, script_env: dict[str, str], tmp_path: Path
) -> None:
    """Ticket 27: base v3's slotless incubator is additive and idempotent."""
    ledger = molbio_v2_ledger(
        embedded_ledger_factory, tmp_path / "molbio-v2-presets", name="base-v2"
    )
    seed_molbio_v2(ledger)
    counts_before = molbio_row_counts(ledger)
    head_before = head(ledger)
    commits_before = commit_count(ledger)

    dry = run_python(
        BASE_2_TO_3, "--ledger", str(ledger.path), "--dry-run", env=script_env
    )
    assert dry.returncode == 0, dry.stdout + dry.stderr
    assert "migration: base 2 -> 3" in dry.stdout
    assert "CALL DOLT_TAG('pre-migrate-base-2'" in dry.stdout
    assert "box_incubator" in dry.stdout
    assert head(ledger) == head_before
    assert tags(ledger) == {}

    applied = run_python(BASE_2_TO_3, "--ledger", str(ledger.path), env=script_env)
    assert applied.returncode == 0, applied.stdout + applied.stderr
    assert "migrate(ledger): base 2 -> 3" in applied.stdout

    reopened = open_ledger(ledger.path, allow_legacy_statuses=True)
    try:
        # Only base's counter is bumped: molbio's own step records molbio/parts.
        assert json.loads(reopened.meta["preset_versions"]) == {
            "base": 3,
            "molbio": 2,
        }
        tables = table_names(reopened)
        # Spec 5: the kind has a table but no v_box_<kind> view; each box
        # gets its own view instead.
        assert "box_incubator" in tables
        assert "v_box_incubator" not in tables
        assert molbio_row_counts(reopened) == counts_before
        # Exactly one commit, with the contract message and the restore tag.
        assert commit_count(reopened) == commits_before + 1
        assert commit_message(reopened, head(reopened)) == "migrate(ledger): base 2 -> 3"
        assert tags(reopened) == {"pre-migrate-base-2": head_before}
        # The new box kind is usable: a slotless incubator holds boxes/vials.
        box = reopened.boxes.add("incubator", "INC-1")
        assert reopened.driver.query(
            "SELECT `name`, `kind` FROM `box` WHERE `id` = %s", (box["id"],)
        ) == [{"name": "INC-1", "kind": "incubator"}]
        checked = tm_check(reopened)
        assert checked.exit_code == 0, checked.output
        assert "FAIL" not in checked.output
    finally:
        reopened.close()

    again = run_python(BASE_2_TO_3, "--ledger", str(ledger.path), env=script_env)
    assert again.returncode == 1, again.stdout + again.stderr
    assert "preset_versions[base] 3, expected 2" in again.stdout + again.stderr

    # The molbio step still runs afterwards and only bumps its own counters.
    molbio_after = run_python(
        MOLBIO_2_TO_3, "--ledger", str(ledger.path), env=script_env
    )
    assert molbio_after.returncode == 0, molbio_after.stdout + molbio_after.stderr
    reopened = open_ledger(ledger.path, allow_legacy_statuses=True)
    try:
        assert json.loads(reopened.meta["preset_versions"]) == {
            **MOLBIO_V3_VERSIONS,
            "base": 3,
        }
        assert reopened.check_fingerprint(warn=False) is True
    finally:
        reopened.close()


def test_spec_3_to_4_then_molbio_2_to_3_is_the_zsqlab10_path(
    embedded_ledger_factory, script_env: dict[str, str], tmp_path: Path
) -> None:
    """The real wave-11 ordering for a spec-3 molbio-v2 ledger, on a test copy.

    ``spec/3-to-4`` -> ``spec/4-to-5`` -> the preset steps (``base/2-to-3``,
    ``molbio/2-to-3``, ``cell_culture/1-to-2``) -> ``base/3-to-4`` last.  No row
    is lost, every kind table survives, the box kind is renamed to ``grid8x12``,
    the counters reach spec 5 / base 4 and ``tm check`` is clean afterwards.
    """
    ledger = molbio_v2_ledger(
        embedded_ledger_factory, tmp_path / "molbio-v2-presets", name="zsqlab10"
    )
    seed_molbio_v2(ledger)
    counts_before = molbio_row_counts(ledger)
    downgrade_to_spec3(ledger)
    assert ledger.meta["spec_version"] == "3"

    steps = (
        (SPEC_3_TO_4, "migrate(ledger): spec 3 -> 4"),
        (SPEC_4_TO_5, "migrate(ledger): spec 4 -> 5"),
        (BASE_2_TO_3, "migrate(ledger): base 2 -> 3"),
        (MOLBIO_2_TO_3, "migrate(ledger): molbio 2 -> 3"),
        (CELL_CULTURE_1_TO_2, "migrate(ledger): cell_culture 1 -> 2"),
        (BASE_3_TO_4, "migrate(ledger): base 3 -> 4"),
    )
    for script, message in steps:
        result = run_python(script, "--ledger", str(ledger.path), env=script_env)
        assert result.returncode == 0, result.stdout + result.stderr
        assert message in result.stdout, result.stdout

    reopened = open_ledger(ledger.path)
    try:
        assert reopened.meta["spec_version"] == "5"
        expected_versions = {
            **MOLBIO_V3_VERSIONS,
            "base": 4,
            "cell_culture": 2,
        }
        assert json.loads(reopened.meta["preset_versions"]) == expected_versions
        assert reopened.schema.preset_versions == expected_versions
        assert reopened.check_fingerprint(warn=False) is True
        assert molbio_row_counts(reopened) == counts_before
        tables = table_names(reopened)
        assert {"entity_oligo", "stock_oligo_stock"} <= tables
        assert "box_grid8x12" in tables
        assert "box_grid96" not in tables
        checked = tm_check(reopened)
        assert checked.exit_code == 0, checked.output
        assert "FAIL" not in checked.output
        planned = CliRunner().invoke(app, [*base_args(reopened), "schema", "plan"])
        assert planned.exit_code == 0, planned.output
        assert "up to date" in planned.output
    finally:
        reopened.close()


def test_molbio_2_to_3_refuses_a_spec3_ledger_and_writes_nothing(
    embedded_ledger_factory, script_env: dict[str, str], tmp_path: Path
) -> None:
    """The bundle copy needs spec 5; a legacy ledger runs the spec steps first."""
    ledger = molbio_v2_ledger(
        embedded_ledger_factory, tmp_path / "molbio-v2-presets", name="legacy"
    )
    seed_molbio_v2(ledger)
    downgrade_to_spec3(ledger)
    head_before = head(ledger)
    commits_before = commit_count(ledger)
    copies_before = {
        name: (ledger.path / "presets" / name).read_bytes()
        for name in ("molbio.yaml", "base.yaml")
    }

    for flags in (["--dry-run"], []):
        result = run_python(
            MOLBIO_2_TO_3, "--ledger", str(ledger.path), *flags, env=script_env
        )
        assert result.returncode == 1, result.stdout + result.stderr
        output = result.stdout + result.stderr
        assert "refusing to migrate molbio 2 -> 3" in output
        assert "migration/spec/4-to-5" in output
        assert "nothing was written" in output
        # No commit, no tag, no DDL, and the placed copies were restored.
        assert head(ledger) == head_before
        assert commit_count(ledger) == commits_before
        assert tags(ledger) == {}
        assert "entity_oligo" not in table_names(ledger)
        for name, data in copies_before.items():
            assert (ledger.path / "presets" / name).read_bytes() == data, name


# ---------------------------------------------------------------------------
# Ticket 28: cell_culture 1 -> 2 (cell_vial extras and the cell_biosample kind)
# ---------------------------------------------------------------------------


def write_cell_culture_v1_presets(directory: Path) -> Path:
    """Materialise a cell_culture-v1 closure from the frozen step copies.

    The repository's ``presets/`` holds cell_culture v2, so the step copy
    shipped next to ``migration/presets/cell_culture/1-to-2/migrate.py`` is the
    only copy of what a pre-ticket-28 ledger carries.  Its includes are the
    wave-10 content: the frozen base v3 (the repository copy is v4 since spec 5)
    plus nucleic_acids 1 and proteins 1 (ticket rq9 bumped the repository copies
    to v2, so their frozen v1 copies sit next to this step's ``migrate.py``),
    which is exactly what a molbio-v3 ledger has.
    """
    directory.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(BASE_V3, directory / "base.yaml")
    for part in ("nucleic_acids", "proteins"):
        shutil.copyfile(
            CELL_CULTURE_1_TO_2_DIR / f"{part}-1.yaml", directory / f"{part}.yaml"
        )
    shutil.copyfile(CELL_CULTURE_V1, directory / "cell_culture.yaml")
    return directory / "cell_culture.yaml"


def cell_culture_v1_ledger(
    factory, directory: Path, *, name: str = "cell-culture-v1"
) -> Ledger:
    """A real cell_culture-v1 ledger built from the frozen step copy."""
    ledger = factory(
        [write_cell_culture_v1_presets(directory)], name=name, real_init=True
    )
    assert ledger.schema.preset_versions == {
        "base": 3,
        "cell_culture": 1,
        "nucleic_acids": 1,
        "proteins": 1,
    }
    return ledger


def test_cell_culture_1_to_2_adds_the_vial_fields_and_the_cell_biosample_kind(
    embedded_ledger_factory, script_env: dict[str, str], tmp_path: Path
) -> None:
    """Ticket 28: content-only, one commit, tagged, dry-run safe, additive."""
    ledger = cell_culture_v1_ledger(
        embedded_ledger_factory, tmp_path / "cell-culture-v1-presets"
    )
    line = ledger.entities.add(
        "cell_line", name="HEK293T", values={"species": "Homo sapiens"}
    )
    # Spec 5: an in-stock vial is placed; one freezer holds both seed stocks.
    ledger.boxes.add("freezer", "CRYO-SEED")
    vial = ledger.stocks.add(
        "cell_vial",
        entity=line["id"],
        values={"passage": 7, "medium": "DMEM + 10% FBS", "dmso_pct": 10},
        aliquots=1,
        box="CRYO-SEED",
        quantity=1,
        unit="vial",
    )
    ledger.stocks.add(
        "cell_pellet",
        entity=line["id"],
        values={"cell_count": 2_000_000, "passage": 7},
        aliquots=1,
        box="CRYO-SEED",
        quantity=1,
        unit="pellet",
    )
    assert column_names(ledger, "stock_cell_vial") == {
        "id",
        "kind",
        "passage",
        "medium",
        "freezing_medium",
        "cells_per_vial",
        "dmso_pct",
    }
    assert "stock_cell_biosample" not in table_names(ledger)
    head_before = head(ledger)
    commits_before = commit_count(ledger)
    copy_before = (ledger.path / "presets" / "cell_culture.yaml").read_bytes()
    assert copy_before == CELL_CULTURE_V1.read_bytes()

    # -- dry run: the additive DDL, the tag and the counter; no writes ------
    dry = run_python(
        CELL_CULTURE_1_TO_2, "--ledger", str(ledger.path), "--dry-run", env=script_env
    )
    assert dry.returncode == 0, dry.stdout + dry.stderr
    out = dry.stdout
    assert "migration: cell_culture 1 -> 2" in out
    assert "CALL DOLT_TAG('pre-migrate-cell_culture-1'" in out
    assert "ALTER TABLE `stock_cell_vial` ADD COLUMN `selection`" in out
    assert "ALTER TABLE `stock_cell_vial` ADD COLUMN `mycoplasma`" in out
    assert "ADD COLUMN `mycoplasma_tested_on`" in out
    assert "CREATE TABLE `stock_cell_biosample`" in out
    assert '"cell_culture": 2' in out
    assert "dolt tests: replace with" in out
    assert head(ledger) == head_before
    assert commit_count(ledger) == commits_before
    assert tags(ledger) == {}
    assert "stock_cell_biosample" not in table_names(ledger)
    assert (ledger.path / "presets" / "cell_culture.yaml").read_bytes() == copy_before

    # -- apply: one commit, every row kept, the new objects usable ---------
    applied = run_python(CELL_CULTURE_1_TO_2, "--ledger", str(ledger.path), env=script_env)
    assert applied.returncode == 0, applied.stdout + applied.stderr
    assert "committed" in applied.stdout
    assert "migrate(ledger): cell_culture 1 -> 2" in applied.stdout

    reopened = open_ledger(ledger.path, allow_legacy_statuses=True)
    try:
        assert_fingerprint_refreshed(reopened)
        assert reopened.check_fingerprint(warn=False) is True
        assert json.loads(reopened.meta["preset_versions"]) == {
            "base": 3,
            "cell_culture": 2,
            "nucleic_acids": 1,
            "proteins": 1,
        }
        head_after_migration = head(reopened)
        # The frozen v2 content is what the ledger now carries, byte for byte;
        # base stays the frozen v3 (the repository copy is v4 since spec 5).
        assert (reopened.path / "presets" / "cell_culture.yaml").read_bytes() == (
            CELL_CULTURE_V2.read_bytes()
        )
        assert (reopened.path / "presets" / "base.yaml").read_bytes() == (
            BASE_V3.read_bytes()
        )

        # Three additive columns plus the new stock kind and its view.
        assert column_names(reopened, "stock_cell_vial") == {
            "id",
            "kind",
            "passage",
            "medium",
            "freezing_medium",
            "cells_per_vial",
            "dmso_pct",
            "selection",
            "mycoplasma",
            "mycoplasma_tested_on",
        }
        assert {
            "stock_cell_biosample",
            "v_stock_cell_biosample",
        } <= table_names(reopened)
        # Every pre-step row survived the content swap.
        assert row_count(reopened, "stock_cell_vial") == 1
        assert row_count(reopened, "stock_cell_pellet") == 1
        row = reopened.driver.query(
            "SELECT `passage`, `dmso_pct`, `mycoplasma` FROM `stock_cell_vial` "
            "WHERE `id` = %s",
            (vial["id"],),
        )[0]
        assert (row["passage"], float(row["dmso_pct"])) == (7, 10.0)
        # Nothing sets the new fields: the enum default is the v2 default.
        assert row["mycoplasma"] == "not_tested"
        assert row_count(reopened, "stock_cell_biosample") == 0

        # One commit with the contract message, tagged at the pre-step commit.
        assert commit_count(reopened) == commits_before + 1
        head_after = head(reopened)
        assert head_after != head_before
        assert commit_message(reopened, head_after) == (
            "migrate(ledger): cell_culture 1 -> 2"
        )
        assert tags(reopened) == {"pre-migrate-cell_culture-1": head_before}

        # The new kind is usable and its status set is the base ``tube`` set.
        biosample = reopened.stocks.add(
            "cell_biosample",
            entity=line["id"],
            values={"sample_type": "dna", "collected_on": "2026-10-02"},
            aliquots=1,
            quantity=1,
            unit="tube",
        )
        assert reopened.schema.statuses_for("cell_biosample") == [
            "pending",
            "stored",
            "missing",
            "consumed",
        ]
        assert reopened.driver.query(
            "SELECT `sample_type` FROM `stock_cell_biosample` WHERE `id` = %s",
            (biosample["id"],),
        ) == [{"sample_type": "dna"}]

        # tm check runs the regenerated v2 tests; the schema plan is empty.
        checked = tm_check(reopened)
        assert checked.exit_code == 0, checked.output
        assert "FAIL" not in checked.output
        assert "OK:" in checked.output
        planned = CliRunner().invoke(app, [*base_args(reopened), "schema", "plan"])
        assert planned.exit_code == 0, planned.output
        assert "up to date" in planned.output
    finally:
        reopened.close()

    # -- a second run refuses (cell_culture is already 2) ------------------
    again = run_python(CELL_CULTURE_1_TO_2, "--ledger", str(ledger.path), env=script_env)
    assert again.returncode == 1, again.stdout + again.stderr
    assert "preset_versions[cell_culture] 2, expected 1" in (
        again.stdout + again.stderr
    )
    assert head(ledger) != head_after_migration  # the test's own biosample write
    assert commit_message(ledger, head_after_migration) == (
        "migrate(ledger): cell_culture 1 -> 2"
    )


def test_cell_culture_1_to_2_refuses_a_ledger_without_cell_culture(
    embedded_ledger_factory, script_env: dict[str, str], tmp_path: Path
) -> None:
    """A molbio-v2 ledger has no cell_culture counter: molbio 2 -> 3 comes first."""
    ledger = molbio_v2_ledger(
        embedded_ledger_factory, tmp_path / "molbio-v2-presets", name="no-cell-culture"
    )
    head_before = head(ledger)
    commits_before = commit_count(ledger)
    copies_before = {
        name: (ledger.path / "presets" / name).read_bytes()
        for name in ("molbio.yaml", "base.yaml")
    }

    for flags in (["--dry-run"], []):
        result = run_python(
            CELL_CULTURE_1_TO_2, "--ledger", str(ledger.path), *flags, env=script_env
        )
        assert result.returncode == 1, result.stdout + result.stderr
        output = result.stdout + result.stderr
        # The shared runner refuses before the step's own plan runs: the ledger
        # has no cell_culture counter at all (the cell kinds are still inside
        # molbio v2).  The step README names molbio/2-to-3 as the way in.
        assert "no preset_versions[cell_culture]" in output
        assert head(ledger) == head_before
        assert commit_count(ledger) == commits_before
        assert tags(ledger) == {}
        assert "stock_cell_biosample" not in table_names(ledger)
        for name, data in copies_before.items():
            assert (ledger.path / "presets" / name).read_bytes() == data, name


def test_molbio_v3_ledger_upgrades_to_cellbio_era(
    embedded_ledger_factory, script_env: dict[str, str], tmp_path: Path
) -> None:
    """The documented order: molbio 2 -> 3, base 2 -> 3, then cell_culture 1 -> 2."""
    ledger = molbio_v2_ledger(
        embedded_ledger_factory, tmp_path / "molbio-v2-presets", name="chain28"
    )
    seed_molbio_v2(ledger)
    counts_before = molbio_row_counts(ledger)

    for script, message in (
        (MOLBIO_2_TO_3, "migrate(ledger): molbio 2 -> 3"),
        (BASE_2_TO_3, "migrate(ledger): base 2 -> 3"),
        (CELL_CULTURE_1_TO_2, "migrate(ledger): cell_culture 1 -> 2"),
    ):
        result = run_python(script, "--ledger", str(ledger.path), env=script_env)
        assert result.returncode == 0, result.stdout + result.stderr
        assert message in result.stdout

    reopened = open_ledger(ledger.path, allow_legacy_statuses=True)
    try:
        assert json.loads(reopened.meta["preset_versions"]) == {
            "bacteria": 1,
            "base": 3,
            "cell_culture": 2,
            "molbio": 3,
            "nucleic_acids": 1,
            "proteins": 1,
        }
        assert reopened.check_fingerprint(warn=False) is True
        assert molbio_row_counts(reopened) == counts_before
        assert {"stock_cell_biosample", "v_stock_cell_biosample", "box_incubator"} <= (
            table_names(reopened)
        )
        assert "mycoplasma" in column_names(reopened, "stock_cell_vial")
        checked = tm_check(reopened)
        assert checked.exit_code == 0, checked.output
        assert "FAIL" not in checked.output
        planned = CliRunner().invoke(app, [*base_args(reopened), "schema", "plan"])
        assert planned.exit_code == 0, planned.output
        assert "up to date" in planned.output
    finally:
        reopened.close()


# ---------------------------------------------------------------------------
# Ticket rq9: proteins/nucleic_acids 1 -> 2 (IUPAC sequence patterns)
# ---------------------------------------------------------------------------

PROTEINS_1_TO_2 = ROOT / "migration" / "presets" / "proteins" / "1-to-2" / "migrate.py"
PROTEINS1_TO_2_DIR = PROTEINS_1_TO_2.parent
PROTEINS_2 = PROTEINS1_TO_2_DIR / "proteins-2.yaml"
NUCLEIC_ACIDS_1_TO_2 = (
    ROOT / "migration" / "presets" / "nucleic_acids" / "1-to-2" / "migrate.py"
)
NUCLEIC_ACIDS_1_TO_2_DIR = NUCLEIC_ACIDS_1_TO_2.parent
NUCLEIC_ACIDS_2 = NUCLEIC_ACIDS_1_TO_2_DIR / "nucleic_acids-2.yaml"

#: The frozen v1 copies ticket rq9 retrofitted into the wave-10 steps: a molbio
#: ledger before the pattern steps carries these exact files.
FROZEN_PROTEINS_1 = (
    ROOT / "migration" / "presets" / "cell_culture" / "1-to-2" / "proteins-1.yaml"
)
FROZEN_NUCLEIC_ACIDS_1 = (
    ROOT
    / "migration"
    / "presets"
    / "cell_culture"
    / "1-to-2"
    / "nucleic_acids-1.yaml"
)

#: ``preset_versions`` of a molbio-v3 ledger before ticket rq9: base 4 (the
#: spec-5 rename ran), cell_culture 2, proteins/nucleic_acids still 1.
PATTERN_V1_VERSIONS = {
    "bacteria": 1,
    "base": 4,
    "cell_culture": 2,
    "molbio": 3,
    "nucleic_acids": 1,
    "proteins": 1,
}


def write_pattern_v1_presets(directory: Path) -> Path:
    """A molbio-v3 closure before ticket rq9 (proteins/nucleic_acids at v1).

    The repository ships proteins 3 / nucleic_acids 3 now, so the ledger's own
    copies come from the frozen v1 files the retrofit steps place; base is the
    frozen v4 (ticket 30's rename already ran), bacteria the frozen v1 and
    cell_culture the frozen v2, the versions the repository shipped before
    Tetramark-epn.4 bumped them.  ``molbio`` itself is unchanged.
    """
    directory.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(LEGACY_BASE_V4, directory / "base.yaml")
    shutil.copyfile(FROZEN_BACTERIA_1, directory / "bacteria.yaml")
    shutil.copyfile(FROZEN_CELL_CULTURE_2, directory / "cell_culture.yaml")
    shutil.copyfile(REPO_PRESETS / "molbio.yaml", directory / "molbio.yaml")
    shutil.copyfile(FROZEN_PROTEINS_1, directory / "proteins.yaml")
    shutil.copyfile(FROZEN_NUCLEIC_ACIDS_1, directory / "nucleic_acids.yaml")
    return directory / "molbio.yaml"


def pattern_v1_ledger(factory, directory: Path, *, name: str = "pattern-v1") -> Ledger:
    ledger = factory([write_pattern_v1_presets(directory)], name=name, real_init=True)
    assert ledger.schema.preset_versions == PATTERN_V1_VERSIONS
    assert json.loads(ledger.meta["preset_versions"]) == PATTERN_V1_VERSIONS
    return ledger


def seed_pattern_v1(ledger: Ledger) -> dict[str, Any]:
    """One row per patterned field, then the invalid values a v1 ledger allows.

    The v1 presets have no pattern, so a direct SQL update (committed, so the
    migration sees a clean working set) reproduces rows an old ledger may well
    hold: ``ACGTZ``/``ACGT1`` in nucleotide fields and ``MKW.`` in the protein
    sequence.
    """
    construct = ledger.entities.add(
        "protein_construct",
        name="c01",
        values={
            "sequence": "ACGTN",
            "mature_sequence": "MKWV*",
            "molecular_weight_da": 12345.67,
        },
    )
    plasmid = ledger.entities.add(
        "plasmid",
        name="p01",
        values={"sequence": "ATGG", "construct": construct["id"]},
    )
    oligo = ledger.entities.add("oligo", name="o01", values={"sequence": "ACGTN"})
    ledger.driver.execute(
        "UPDATE `entity_protein_construct` SET `sequence` = 'ACGTZ', "
        "`mature_sequence` = 'MKW.' WHERE `id` = %s",
        (construct["id"],),
    )
    ledger.driver.execute(
        "UPDATE `entity_plasmid` SET `sequence` = 'ACGT1' WHERE `id` = %s",
        (plasmid["id"],),
    )
    ledger.driver.execute(
        "UPDATE `entity_oligo` SET `sequence` = 'ACGTZ' WHERE `id` = %s",
        (oligo["id"],),
    )
    ledger.driver.commit("test: seed rows the v2 patterns reject")
    ledger.read_meta()
    return {"construct": construct, "plasmid": plasmid, "oligo": oligo}


def set_recorded_versions(ledger: Ledger, payload: dict[str, Any], note: str) -> None:
    """Rewrite ``ledger_meta.preset_versions`` to simulate another ledger era."""
    ledger.driver.execute(
        "UPDATE `ledger_meta` SET `value` = %s WHERE `key` = 'preset_versions'",
        (json.dumps(payload, sort_keys=True),),
    )
    ledger.driver.commit(f"test: {note}")
    ledger.read_meta()


def load_step_module(script: Path):
    """Import a migration script so a precondition refusal runs in-process."""
    name = f"rq9_{script.parent.parent.name}_{script.parent.name}"
    spec = importlib.util.spec_from_file_location(name, script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_pattern_presets_1_to_2_migration_chain(
    embedded_ledger_factory, script_env: dict[str, str], tmp_path: Path
) -> None:
    """Ticket rq9: proteins then nucleic_acids, one real ledger end to end.

    One real init keeps this bounded: the refusals (order, spec 5, base 4), the
    dry-run/apply contract (one commit, the restore tag, frozen copies,
    counters), ``tm check`` reporting the rows outside the new patterns with
    their ids, a corrected row passing, and the second-run refusals.
    """
    ledger = pattern_v1_ledger(embedded_ledger_factory, tmp_path / "pattern-v1-presets")
    seeded = seed_pattern_v1(ledger)
    # The v1 content has no pattern tests: these rows pass every check.
    assert ledger.check().passed
    copies_before = {
        name: (ledger.path / "presets" / name).read_bytes()
        for name in ("base.yaml", "proteins.yaml", "nucleic_acids.yaml", "molbio.yaml")
    }

    # -- the order matters: nucleic_acids needs proteins 2 first -------------
    refused = run_python(
        NUCLEIC_ACIDS_1_TO_2, "--ledger", str(ledger.path), "--dry-run", env=script_env
    )
    assert refused.returncode == 1, refused.stdout + refused.stderr
    output = refused.stdout + refused.stderr
    assert "refusing to migrate nucleic_acids 1 -> 2" in output
    assert "migration/presets/proteins/1-to-2" in output
    assert "nothing was written" in output

    # -- proteins refusals: spec 5 first, then base 4 ----------------------
    # The same ``plan`` the script runs, in-process (no second real init).
    proteins_step = load_step_module(PROTEINS_1_TO_2).step
    ledger.driver.execute(
        "UPDATE `ledger_meta` SET `value` = '4' WHERE `key` = 'spec_version'"
    )
    ledger.driver.commit("test: pretend a spec-4 ledger")
    ledger.read_meta()
    head_before_refusals = head(ledger)
    commits_before_refusals = commit_count(ledger)
    with pytest.raises(LedgerError, match="migration/spec/4-to-5"):
        proteins_step.plan(ledger)
    assert head(ledger) == head_before_refusals
    assert commit_count(ledger) == commits_before_refusals

    ledger.driver.execute(
        "UPDATE `ledger_meta` SET `value` = '5' WHERE `key` = 'spec_version'"
    )
    ledger.driver.commit("test: back to spec 5")
    ledger.read_meta()
    set_recorded_versions(
        ledger, {**PATTERN_V1_VERSIONS, "base": 3}, "pretend a base-3 ledger"
    )
    with pytest.raises(LedgerError, match="migration/presets/base/3-to-4"):
        proteins_step.plan(ledger)
    set_recorded_versions(ledger, PATTERN_V1_VERSIONS, "restore the base-4 counters")
    for name, data in copies_before.items():
        assert (ledger.path / "presets" / name).read_bytes() == data, name

    head_before = head(ledger)
    commits_before = commit_count(ledger)

    # -- proteins 1 -> 2: dry run, then one commit -------------------------
    dry = run_python(
        PROTEINS_1_TO_2, "--ledger", str(ledger.path), "--dry-run", env=script_env
    )
    assert dry.returncode == 0, dry.stdout + dry.stderr
    assert "migration: proteins 1 -> 2" in dry.stdout
    assert "CALL DOLT_TAG('pre-migrate-proteins-1'" in dry.stdout
    assert '"proteins": 2' in dry.stdout
    assert "dolt tests: replace with" in dry.stdout
    assert head(ledger) == head_before
    assert commit_count(ledger) == commits_before
    assert tags(ledger) == {}
    for name, data in copies_before.items():
        assert (ledger.path / "presets" / name).read_bytes() == data, name

    applied = run_python(PROTEINS_1_TO_2, "--ledger", str(ledger.path), env=script_env)
    assert applied.returncode == 0, applied.stdout + applied.stderr
    assert "committed" in applied.stdout
    assert "migrate(ledger): proteins 1 -> 2" in applied.stdout

    proteins_era = open_ledger(ledger.path, allow_legacy_statuses=True)
    try:
        assert_fingerprint_refreshed(proteins_era)
        assert proteins_era.check_fingerprint(warn=False) is True
        expected_versions = {**PATTERN_V1_VERSIONS, "proteins": 2}
        assert json.loads(proteins_era.meta["preset_versions"]) == expected_versions
        assert proteins_era.schema.preset_versions == expected_versions
        # The step places the frozen v2 target; nucleic_acids stays at v1.
        assert (proteins_era.path / "presets" / "proteins.yaml").read_bytes() == (
            PROTEINS_2.read_bytes()
        )
        assert (
            proteins_era.path / "presets" / "nucleic_acids.yaml"
        ).read_bytes() == FROZEN_NUCLEIC_ACIDS_1.read_bytes()
        # The step places the frozen base v4 it reads (spec 6 made the
        # repository base the fixed model, so the era copy is the frozen one).
        assert (proteins_era.path / "presets" / "base.yaml").read_bytes() == (
            LEGACY_BASE_V4.read_bytes()
        )
        # One commit with the contract message, tagged at the pre-step commit.
        assert commit_count(proteins_era) == commits_before + 1
        head_after = head(proteins_era)
        assert head_after != head_before
        assert commit_message(proteins_era, head_after) == (
            "migrate(ledger): proteins 1 -> 2"
        )
        assert tags(proteins_era) == {"pre-migrate-proteins-1": head_before}
        # Every pre-step row survived.
        assert row_count(proteins_era, "entity_protein_construct") == 1
        assert row_count(proteins_era, "entity_plasmid") == 1
        assert row_count(proteins_era, "entity_oligo") == 1

        # ``tm check`` now FAILs on the new pattern tests, naming the rows
        # (the same run_check the CLI calls, with ``-v`` detail queries).
        report = run_check(proteins_era.driver, proteins_era.schema, verbose=True)
        assert not report.passed
        failed = {result.name for result in report.blocking_failures}
        assert {
            "schema_protein_construct_pattern_sequence",
            "schema_protein_construct_pattern_mature_sequence",
            "schema_plasmid_pattern_sequence",
        } <= failed
        detail = {
            (result.name, row["id"], row["value"])
            for result in report.blocking_failures
            for row in result.detail_rows
        }
        assert (
            "schema_protein_construct_pattern_sequence",
            seeded["construct"]["id"],
            "ACGTZ",
        ) in detail
        assert (
            "schema_protein_construct_pattern_mature_sequence",
            seeded["construct"]["id"],
            "MKW.",
        ) in detail
        assert (
            "schema_plasmid_pattern_sequence",
            seeded["plasmid"]["id"],
            "ACGT1",
        ) in detail

        # A corrected row passes again.
        proteins_era.driver.execute(
            "UPDATE `entity_protein_construct` SET `sequence` = 'ACGTN', "
            "`mature_sequence` = 'MKWV*' WHERE `id` = %s",
            (seeded["construct"]["id"],),
        )
        proteins_era.driver.execute(
            "UPDATE `entity_plasmid` SET `sequence` = 'ATGG' WHERE `id` = %s",
            (seeded["plasmid"]["id"],),
        )
        proteins_era.driver.commit("test: correct the sequences")
        proteins_era.read_meta()
        assert proteins_era.check().passed

        # -- nucleic_acids 1 -> 2: dry run, then one commit ----------------
        head_before = head(proteins_era)
        commits_before = commit_count(proteins_era)
        proteins_tag = tags(proteins_era)
        assert set(proteins_tag) == {"pre-migrate-proteins-1"}
        assert proteins_tag["pre-migrate-proteins-1"] != head_before
        dry = run_python(
            NUCLEIC_ACIDS_1_TO_2,
            "--ledger",
            str(ledger.path),
            "--dry-run",
            env=script_env,
        )
        assert dry.returncode == 0, dry.stdout + dry.stderr
        assert "migration: nucleic_acids 1 -> 2" in dry.stdout
        assert "CALL DOLT_TAG('pre-migrate-nucleic_acids-1'" in dry.stdout
        assert '"nucleic_acids": 2' in dry.stdout
        assert "dolt tests: replace with" in dry.stdout
        # The dry run wrote nothing and restored the copies it placed.
        assert head(proteins_era) == head_before
        assert commit_count(proteins_era) == commits_before
        assert tags(proteins_era) == proteins_tag
        assert (proteins_era.path / "presets" / "nucleic_acids.yaml").read_bytes() == (
            FROZEN_NUCLEIC_ACIDS_1.read_bytes()
        )
    finally:
        proteins_era.close()

    applied = run_python(
        NUCLEIC_ACIDS_1_TO_2, "--ledger", str(ledger.path), env=script_env
    )
    assert applied.returncode == 0, applied.stdout + applied.stderr
    assert "migrate(ledger): nucleic_acids 1 -> 2" in applied.stdout

    reopened = open_ledger(ledger.path, allow_legacy_statuses=True)
    try:
        assert_fingerprint_refreshed(reopened)
        assert reopened.check_fingerprint(warn=False) is True
        expected_versions = {
            **PATTERN_V1_VERSIONS,
            "proteins": 2,
            "nucleic_acids": 2,
        }
        assert json.loads(reopened.meta["preset_versions"]) == expected_versions
        assert reopened.schema.preset_versions == expected_versions
        assert (reopened.path / "presets" / "nucleic_acids.yaml").read_bytes() == (
            NUCLEIC_ACIDS_2.read_bytes()
        )
        assert (reopened.path / "presets" / "proteins.yaml").read_bytes() == (
            PROTEINS_2.read_bytes()
        )
        assert commit_count(reopened) == commits_before + 1
        head_after = head(reopened)
        assert commit_message(reopened, head_after) == (
            "migrate(ledger): nucleic_acids 1 -> 2"
        )
        assert tags(reopened) == {
            "pre-migrate-proteins-1": proteins_tag["pre-migrate-proteins-1"],
            "pre-migrate-nucleic_acids-1": head_before,
        }

        report = run_check(reopened.driver, reopened.schema, verbose=True)
        assert not report.passed
        assert "schema_oligo_pattern_sequence" in {
            result.name for result in report.blocking_failures
        }
        assert (
            "schema_oligo_pattern_sequence",
            seeded["oligo"]["id"],
            "ACGTZ",
        ) in {
            (result.name, row["id"], row["value"])
            for result in report.blocking_failures
            for row in result.detail_rows
        }
        reopened.driver.execute(
            "UPDATE `entity_oligo` SET `sequence` = 'ACGTN' WHERE `id` = %s",
            (seeded["oligo"]["id"],),
        )
        reopened.driver.commit("test: correct the oligo")
        reopened.read_meta()
        assert reopened.check().passed
        planned = CliRunner().invoke(app, [*base_args(reopened), "schema", "plan"])
        assert planned.exit_code == 0, planned.output
        assert "up to date" in planned.output
    finally:
        reopened.close()

    # -- a second run refuses each counter --------------------------------
    again = run_python(PROTEINS_1_TO_2, "--ledger", str(ledger.path), env=script_env)
    assert again.returncode == 1, again.stdout + again.stderr
    assert "preset_versions[proteins] 2, expected 1" in (
        again.stdout + again.stderr
    )
    again = run_python(
        NUCLEIC_ACIDS_1_TO_2, "--ledger", str(ledger.path), env=script_env
    )
    assert again.returncode == 1, again.stdout + again.stderr
    assert "preset_versions[nucleic_acids] 2, expected 1" in (
        again.stdout + again.stderr
    )


# ---------------------------------------------------------------------------
# Tetramark-3ht: proteins 3 -> 4 (a construct's sequence is amino acids)
# ---------------------------------------------------------------------------

PROTEINS_3_TO_4 = (
    ROOT / "migration" / "presets" / "proteins" / "3-to-4" / "migrate.py"
)
PROTEINS_3_TO_4_DIR = PROTEINS_3_TO_4.parent
PROTEINS_4 = PROTEINS_3_TO_4_DIR / "proteins-4.yaml"
PROTEINS_3 = (
    ROOT / "migration" / "presets" / "proteins" / "2-to-3" / "proteins-3.yaml"
)

#: ``preset_versions`` of the spec-6 closure with proteins still at 3.
PROTEINS_V3_VERSIONS = {
    "bacteria": 2,
    "base": 6,
    "cell_culture": 3,
    "molbio": 3,
    "nucleic_acids": 3,
    "proteins": 3,
}

#: The amino-acid sequence a legacy construct carries in its ``sequence``.
CONSTRUCT_AA = "MGSSHHHHHHSSGMVNEVIDINEAVRAYIAQIEGLRAEIGRLDATIATLR"


def write_proteins_v3_presets(directory: Path) -> Path:
    """The spec-6 closure with proteins at v3, before Tetramark-3ht.

    The repository ships proteins 4 now, so the ledger's own copy comes from the
    frozen v3 file the manual ``proteins/2-to-3`` step installs; every other
    part is the current spec-6 copy.
    """
    directory.mkdir(parents=True, exist_ok=True)
    for name in ("base", "bacteria", "cell_culture", "nucleic_acids", "molbio"):
        shutil.copyfile(REPO_PRESETS / f"{name}.yaml", directory / f"{name}.yaml")
    shutil.copyfile(PROTEINS_3, directory / "proteins.yaml")
    return directory / "molbio.yaml"


def proteins_v3_ledger(
    factory, directory: Path, *, name: str = "proteins-v3"
) -> Ledger:
    ledger = factory([write_proteins_v3_presets(directory)], name=name, real_init=True)
    assert ledger.schema.preset_versions == PROTEINS_V3_VERSIONS
    assert json.loads(ledger.meta["preset_versions"]) == PROTEINS_V3_VERSIONS
    return ledger


def seed_amino_acid_construct(ledger: Ledger) -> dict[str, Any]:
    """One construct holding the legacy amino-acid sequence in ``sequence``.

    The v3 preset still validates ``sequence`` as nucleotide, so the row is
    added with a valid value and corrected with SQL: exactly the row the legacy
    import produced and the v3 pattern never saw.
    """
    construct = ledger.entities.add(
        "protein_construct",
        name="gPFD",
        values={"sequence": "ACGTN", "molecular_weight_da": 12345.67},
    )
    ledger.driver.execute(
        "UPDATE `entity_protein_construct` SET `sequence` = %s WHERE `id` = %s",
        (CONSTRUCT_AA, construct["id"]),
    )
    ledger.driver.commit("test: seed the legacy amino-acid construct")
    ledger.read_meta()
    return construct


def test_proteins_3_to_4_renames_the_construct_length(
    embedded_ledger_factory, script_env: dict[str, str], tmp_path: Path
) -> None:
    """Tetramark-3ht: one real spec-6 ledger, dry run then one commit.

    The v3 nucleotide pattern reports the legacy construct; the step places the
    frozen v4 copy, renames ``length_bp`` to ``length_aa`` in place and
    regenerates the tests, so ``tm check`` is green again with the same row.
    """
    ledger = proteins_v3_ledger(
        embedded_ledger_factory, tmp_path / "proteins-v3-presets"
    )
    seeded = seed_amino_acid_construct(ledger)
    report = run_check(ledger.driver, ledger.schema)
    assert not report.passed
    assert "schema_protein_construct_pattern_sequence" in {
        result.name for result in report.blocking_failures
    }
    copies_before = {
        name: (ledger.path / "presets" / f"{name}.yaml").read_bytes()
        for name in PROTEINS_V3_VERSIONS
    }

    # -- a spec-5 ledger refuses: the v4 preset belongs to spec 6 ----------
    commits_pre = commit_count(ledger)
    step = load_step_module(PROTEINS_3_TO_4).step
    ledger.driver.execute(
        "UPDATE `ledger_meta` SET `value` = '5' WHERE `key` = 'spec_version'"
    )
    ledger.driver.commit("test: pretend a spec-5 ledger")
    ledger.read_meta()
    head_pretend = head(ledger)
    with pytest.raises(LedgerError, match="migration/spec/5-to-6"):
        step.plan(ledger)
    # The refusal wrote nothing: only the test's own commit is new.
    assert head(ledger) == head_pretend
    assert commit_count(ledger) == commits_pre + 1
    assert tags(ledger) == {}
    ledger.driver.execute(
        "UPDATE `ledger_meta` SET `value` = '6' WHERE `key` = 'spec_version'"
    )
    ledger.driver.commit("test: back to spec 6")
    ledger.read_meta()

    head_before = head(ledger)
    commits_before = commit_count(ledger)

    # -- dry run writes nothing -------------------------------------------
    dry = run_python(
        PROTEINS_3_TO_4, "--ledger", str(ledger.path), "--dry-run", env=script_env
    )
    assert dry.returncode == 0, dry.stdout + dry.stderr
    assert "migration: proteins 3 -> 4" in dry.stdout
    assert "CALL DOLT_TAG('pre-migrate-proteins-3'" in dry.stdout
    assert "RENAME COLUMN `length_bp` TO `length_aa`" in dry.stdout
    assert '"proteins": 4' in dry.stdout
    assert head(ledger) == head_before
    assert commit_count(ledger) == commits_before
    assert tags(ledger) == {}
    for name, data in copies_before.items():
        assert (ledger.path / "presets" / f"{name}.yaml").read_bytes() == data, name

    # -- apply: exactly one commit ----------------------------------------
    applied = run_python(PROTEINS_3_TO_4, "--ledger", str(ledger.path), env=script_env)
    assert applied.returncode == 0, applied.stdout + applied.stderr
    assert "migrate(ledger): proteins 3 -> 4" in applied.stdout

    reopened = open_ledger(ledger.path)
    try:
        assert_fingerprint_refreshed(reopened)
        assert reopened.check_fingerprint(warn=False) is True
        assert json.loads(reopened.meta["preset_versions"]) == {
            **PROTEINS_V3_VERSIONS,
            "proteins": 4,
        }
        assert (reopened.path / "presets" / "proteins.yaml").read_bytes() == (
            PROTEINS_4.read_bytes()
        )
        assert commit_count(reopened) == commits_before + 1
        assert tags(reopened) == {"pre-migrate-proteins-3": head_before}
        assert commit_message(reopened, head(reopened)) == (
            "migrate(ledger): proteins 3 -> 4"
        )

        columns = column_names(reopened, "entity_protein_construct")
        assert "length_aa" in columns
        assert "length_bp" not in columns
        # The value moved in place: same sequence, same length, no data change.
        rows = reopened.driver.query(
            "SELECT `sequence`, `length_aa` FROM `entity_protein_construct` "
            "WHERE `id` = %s",
            (seeded["id"],),
        )
        assert rows == [
            {"sequence": CONSTRUCT_AA, "length_aa": len(CONSTRUCT_AA)}
        ]
        assert row_count(reopened, "entity_protein_construct") == 1

        # The amino-acid pattern now matches the row: the check is green.
        checked = tm_check(reopened)
        assert checked.exit_code == 0, checked.output
        assert "FAIL" not in checked.output
        planned = CliRunner().invoke(app, [*base_args(reopened), "schema", "plan"])
        assert planned.exit_code == 0, planned.output
        assert "up to date" in planned.output
    finally:
        reopened.close()

    # -- a second run refuses the counter ---------------------------------
    again = run_python(PROTEINS_3_TO_4, "--ledger", str(ledger.path), env=script_env)
    assert again.returncode == 1, again.stdout + again.stderr
    assert "preset_versions[proteins] 4, expected 3" in (
        again.stdout + again.stderr
    )


def write_proteins_v3_base_5_presets(directory: Path) -> Path:
    """A spec-6 closure with proteins 3 still on the **base-5** copy.

    Tetramark-500: the frozen ``proteins-4.yaml`` includes ``base``, so running
    proteins 3 -> 4 on a base-5 closure would pull the capacity change into that
    step's plan.  The step therefore pins ``BASE_VERSION = 6`` and must refuse
    this ledger, naming the manual base step.
    """
    directory.mkdir(parents=True, exist_ok=True)
    for name in ("bacteria", "cell_culture", "nucleic_acids", "molbio"):
        shutil.copyfile(REPO_PRESETS / f"{name}.yaml", directory / f"{name}.yaml")
    shutil.copyfile(BASE_5, directory / "base.yaml")
    shutil.copyfile(PROTEINS_3, directory / "proteins.yaml")
    return directory / "molbio.yaml"


def test_proteins_3_to_4_refuses_a_base_5_closure(
    embedded_ledger_factory, script_env: dict[str, str], tmp_path: Path
) -> None:
    """Tetramark-500: a base-5 closure must run base/5-to-6 first.

    The refusal is actionable, writes nothing (no commit, no tag) and leaves the
    ledger's proteins copy exactly as it found it.
    """
    ledger = embedded_ledger_factory(
        [write_proteins_v3_base_5_presets(tmp_path / "proteins-v3-base-5-presets")],
        name="proteins-v3-base-5-ledger",
        real_init=True,
    )
    versions = {**PROTEINS_V3_VERSIONS, "base": 5}
    assert ledger.schema.preset_versions == versions
    assert json.loads(ledger.meta["preset_versions"]) == versions
    head_before = head(ledger)
    commits_before = commit_count(ledger)
    proteins_copy = (ledger.path / "presets" / "proteins.yaml").read_bytes()

    result = run_python(PROTEINS_3_TO_4, "--ledger", str(ledger.path), env=script_env)
    assert result.returncode == 1, result.stdout + result.stderr
    output = result.stdout + result.stderr
    assert "the loaded base copy is version 5, expected 6" in output
    assert "migration/presets/base/5-to-6/README.md" in output
    # Nothing was written: no tag, no commit, and the placed v4 copy was restored.
    assert head(ledger) == head_before
    assert commit_count(ledger) == commits_before
    assert tags(ledger) == {}
    assert (ledger.path / "presets" / "proteins.yaml").read_bytes() == proteins_copy


# ---------------------------------------------------------------------------
# spec/4-to-5: one view per box, positions follow in stock (ticket 30)
# ---------------------------------------------------------------------------

SPEC_4_TO_5 = ROOT / "migration" / "spec" / "4-to-5" / "migrate.py"

#: The base 3 -> 4 preset step and its frozen copies.  It runs on a copy of the
#: spec-4 fixture **after** ``spec/4-to-5``: the ledger formats at spec 5 while
#: its own base copy is still v3 (``grid96``), then the base step renames that
#: kind to ``grid8x12`` and bumps ``preset_versions.base``.
BASE_3_TO_4 = ROOT / "migration" / "presets" / "base" / "3-to-4" / "migrate.py"
BASE4_PRESET = ROOT / "migration" / "presets" / "base" / "3-to-4" / "base-4.yaml"
BASE3_PRESET = ROOT / "migration" / "presets" / "base" / "3-to-4" / "base-3.yaml"
BASE4_COMMIT = "migrate(ledger): base 3 -> 4"
BASE4_TAG = "pre-migrate-base-3"

#: The checked-in spec-4 fixture, built by
#: ``tests/data/ledgers/build_spec4_fixture.py`` from the 0.3.0 checkout
#: (``2aee18b``): ``spec_version=4``, base 3, ``grid96`` still a box kind, the
#: spec-4 per-box-kind views and a consumed tube that keeps its position.
SPEC4_FIXTURE = ROOT / "tests" / "data" / "ledgers" / "spec4-boxes"
BUILD_SPEC4_FIXTURE = ROOT / "tests" / "data" / "ledgers" / "build_spec4_fixture.py"

#: ``dolt_tests`` the 0.3.0 tool generated for base 3 (spec 4).
SPEC4_DOLT_TESTS = 14

#: The spec-5 script's description and the meta bump it makes.
SPEC4_COMMIT = "migrate(ledger): spec 4 -> 5"
SPEC4_TAG = "pre-migrate-spec-4"
CLEARED_NOTE = "spec 4-to-5"


def copy_spec4_fixture(destination: Path) -> Path:
    """Copy the checked-in spec-4 ledger directory to ``destination``."""
    assert (SPEC4_FIXTURE / ".dolt").is_dir(), (
        f"the checked-in spec-4 fixture is missing at {SPEC4_FIXTURE}; "
        f"rebuild it with: uv run python {BUILD_SPEC4_FIXTURE.relative_to(ROOT)}"
    )
    shutil.copytree(SPEC4_FIXTURE, destination)
    return destination


def open_spec4_fixture(
    tmp_path: Path,
    dolt_env: dict[str, str],
    *,
    name: str = "spec4",
) -> Ledger:
    """A fresh copy of the checked-in spec-4 fixture, opened embedded.

    A spec-4 ledger records the fingerprint the 0.3.0 tool computed, so the
    current loader warns about the stale fingerprint; migration steps are the
    supported reader of that shape, so the helper opens it the same way.
    """
    ledger_path = tmp_path / name
    copy_spec4_fixture(ledger_path)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        ledger = open_ledger(ledger_path, env=dolt_env)
    assert ledger.warnings, "a spec-4 ledger must warn about its stale fingerprint"
    assert any("fingerprint does not match" in text for text in ledger.warnings)
    return ledger


@pytest.fixture
def spec4_ledger(tmp_path: Path, dolt_env: dict[str, str]) -> Iterator[Ledger]:
    """A fresh copy of the checked-in spec-4 fixture (embedded driver)."""
    ledger = open_spec4_fixture(tmp_path, dolt_env)
    try:
        yield ledger
    finally:
        ledger.close()


def spec4_placed_aliquots(ledger: Ledger) -> list[tuple[str, str | None, str]]:
    """``(box name, slot, aliquot id)`` for every placed aliquot, by id."""
    rows = ledger.driver.query(
        "SELECT a.`id` AS `id`, b.`name` AS `box`, a.`slot` AS `slot` "
        "FROM `aliquot` a JOIN `box` b ON b.`id` = a.`box_id` "
        "ORDER BY a.`id`"
    )
    return [
        (str(row["box"]), row.get("slot"), str(row["id"])) for row in rows
    ]


def spec4_view_names(ledger: Ledger) -> set[str]:
    """Every live view (Dolt stores view names in lowercase)."""
    rows = ledger.driver.query(
        "SELECT `table_name` AS `name` FROM `information_schema`.`tables` "
        "WHERE `table_schema` = DATABASE() AND `table_type` = 'VIEW'"
    )
    return {str(row["name"]) for row in rows}


def spec4_expected_view_names(ledger: Ledger, box_names: set[str]) -> set[str]:
    """The spec-5 view set: generated views + one ``v_box_<name>`` per box."""
    generated = {statement.name.lower() for statement in view_statements(ledger.schema)}
    return generated | {f"v_box_{name}".lower() for name in box_names}


def _assert_refused_without_writing(
    ledger: Ledger,
    script_env: dict[str, str],
    *,
    expected: list[str],
) -> None:
    """The step refuses on both ``--dry-run`` and apply and writes nothing."""
    head_before = head(ledger)
    commits_before = commit_count(ledger)
    views_before = spec4_view_names(ledger)
    for flags in (["--dry-run"], []):
        result = run_python(
            SPEC_4_TO_5, "--ledger", str(ledger.path), *flags, env=script_env
        )
        assert result.returncode == 1, result.stdout + result.stderr
        output = result.stdout + result.stderr
        assert "refusing to migrate spec 4 -> 5" in output, output
        for fragment in expected:
            assert fragment in output, output
        assert "Nothing was written" in output, output
        # Nothing was written: no commit, no tag, no DDL, counter unchanged.
        assert head(ledger) == head_before
        assert commit_count(ledger) == commits_before
        assert tags(ledger) == {}
        ledger.read_meta()
        assert ledger.meta["spec_version"] == "4"
        assert spec4_view_names(ledger) == views_before


def test_spec_4_to_5_clears_positions_and_creates_the_box_views(
    spec4_ledger: Ledger, script_env: dict[str, str]
) -> None:
    """Acceptance: kind views gone, one view per box, cleared positions, check."""
    ledger = spec4_ledger
    boxes = {
        str(row["name"]): row
        for row in ledger.driver.query(
            "SELECT `id`, `name`, `kind`, `parent_box_id`, `parent_slot` "
            "FROM `box`"
        )
    }
    # The issue's named-view acceptance: the rack R holds child box G in slot 1.
    assert (boxes["G"]["parent_box_id"], boxes["G"]["parent_slot"]) == (
        boxes["R"]["id"],
        "1",
    )
    placed = spec4_placed_aliquots(ledger)
    by_position = {
        (box, slot): aliquot
        for box, slot, aliquot in placed
        if slot is not None
    }
    shelf_ids = sorted(aliquot for box, slot, aliquot in placed if box == "S")
    consumed = ledger.driver.query(
        "SELECT `id`, `box_id`, `slot`, `status` FROM `aliquot` "
        "WHERE `status` = 'consumed'"
    )
    assert len(consumed) == 1
    consumed_id = str(consumed[0]["id"])
    assert (str(consumed[0]["box_id"]), consumed[0]["slot"]) == (
        boxes["H"]["id"],
        "C3",
    )

    # -- the checked-in fixture is a genuine spec-4 ledger -------------------
    assert ledger.meta["spec_version"] == "4"
    assert row_count(ledger, "dolt_tests") == SPEC4_DOLT_TESTS
    views_before = spec4_view_names(ledger)
    assert {
        "v_box_grid9x9",
        "v_box_grid96",
        "v_box_rack",
        "v_box_shelf",
        "v_box_freezer",
    } <= views_before
    assert not {f"v_box_{name}".lower() for name in boxes} & views_before
    # The installed spec-4 tests pass on the fixture itself (the builder asserts
    # the same thing with the 0.3.0 tool that wrote them).  ``run_check`` would
    # also append the spec-5 per-box-view drift check, which a spec-4 ledger is
    # *expected* to fail, so query the installed tests directly.
    installed = ledger.driver.query(
        "SELECT `test_name` AS `name`, `status` AS `status` FROM `dolt_test_run`()"
    )
    assert len(installed) == SPEC4_DOLT_TESTS
    failed_installed = [
        str(row["name"])
        for row in installed
        if str(row["status"]).upper() == "FAIL"
    ]
    assert not failed_installed, failed_installed

    head_before = head(ledger)
    commits_before = commit_count(ledger)
    assert tags(ledger) == {}

    # -- dry run: the whole plan, nothing written ----------------------------
    dry = run_python(
        SPEC_4_TO_5, "--ledger", str(ledger.path), "--dry-run", env=script_env
    )
    assert dry.returncode == 0, dry.stdout + dry.stderr
    assert "migration: spec 4 -> 5" in dry.stdout
    assert f"CALL DOLT_TAG('{SPEC4_TAG}'" in dry.stdout
    assert "UPDATE `aliquot` SET `box_id` = NULL, `slot` = NULL WHERE `id` = " in dry.stdout
    assert "'move'" in dry.stdout
    assert "'C3'" in dry.stdout
    assert f"'{CLEARED_NOTE}'" in dry.stdout
    assert "DROP VIEW IF EXISTS `v_box_grid9x9`" in dry.stdout
    assert "DROP VIEW IF EXISTS `v_box_grid96`" in dry.stdout
    expected_views = spec4_expected_view_names(ledger, set(boxes))
    assert dry.stdout.count("CREATE VIEW") == len(expected_views)
    assert (
        f"dolt tests: replace with {expected_installed_tests(ledger)} generated test(s)"
        in dry.stdout
    )
    assert "'spec_version', '5'" in dry.stdout
    assert head(ledger) == head_before
    assert commit_count(ledger) == commits_before
    assert tags(ledger) == {}
    ledger.read_meta()
    assert ledger.meta["spec_version"] == "4"
    assert spec4_view_names(ledger) == views_before

    # -- apply: one commit, tag, spec 5, the new view set --------------------
    applied = run_python(SPEC_4_TO_5, "--ledger", str(ledger.path), env=script_env)
    assert applied.returncode == 0, applied.stdout + applied.stderr
    assert SPEC4_COMMIT in applied.stdout
    assert "is now at spec_version 5" in applied.stdout
    ledger.read_meta()
    assert ledger.meta["spec_version"] == "5"
    assert ledger.meta["tetramark_version"] == __version__
    assert ledger.meta["fingerprint"] == ledger.schema.fingerprint()
    assert commit_count(ledger) == commits_before + 1
    assert commit_message(ledger, head(ledger)) == SPEC4_COMMIT
    assert tags(ledger) == {SPEC4_TAG: head_before}
    assert row_count(ledger, "dolt_tests") == expected_installed_tests(ledger)

    views = spec4_view_names(ledger)
    assert views == spec4_expected_view_names(ledger, set(boxes))
    kind_views = {f"v_box_{str(row['kind'])}".lower() for row in boxes.values()}
    assert not kind_views & views, f"box kind views survived: {kind_views & views}"

    # -- v_box_G: a 9x9 pivot over A-H,J, empty rows included ---------------
    assert column_names(ledger, "v_box_G") == {"row"} | {
        str(number) for number in range(1, 10)
    }
    g_rows = ledger.driver.query("SELECT * FROM `v_box_G`")
    assert [str(row["row"]) for row in g_rows] == list("ABCDEFGHJ")
    g_by_row = {str(row["row"]): row for row in g_rows}
    g_cells = {
        (label, column): value
        for label, row in g_by_row.items()
        for column, value in row.items()
        if column != "row" and value is not None
    }
    assert g_cells == {
        ("A", "1"): by_position[("G", "A1")],
        ("B", "2"): by_position[("G", "B2")],
    }

    # -- v_box_H (grid96): 8 rows A-H, 12 columns, C3 cleared ---------------
    assert column_names(ledger, "v_box_H") == {"row"} | {
        str(number) for number in range(1, 13)
    }
    h_rows = ledger.driver.query("SELECT * FROM `v_box_H`")
    assert [str(row["row"]) for row in h_rows] == list("ABCDEFGH")
    h_by_row = {str(row["row"]): row for row in h_rows}
    h_cells = {
        (label, column): value
        for label, row in h_by_row.items()
        for column, value in row.items()
        if column != "row" and value is not None
    }
    assert h_cells == {
        ("A", "1"): by_position[("H", "A1")],
        ("B", "2"): by_position[("H", "B2")],
    }
    assert h_by_row["C"]["3"] is None

    # -- v_box_R: 10 named slots; slot 1 holds the child BOX G (the issue's
    #    ``1 | G`` acceptance row), never one of its aliquots -----------------
    assert column_names(ledger, "v_box_R") == {"slot", "occupants"}
    r_rows = ledger.driver.query("SELECT * FROM `v_box_R`")
    assert [str(row["slot"]) for row in r_rows] == [
        str(number) for number in range(1, 11)
    ]
    assert r_rows[0]["occupants"] == "G"
    assert [row["occupants"] for row in r_rows[1:]] == [None] * 9
    # G is a box, not an aliquot of R: R has no aliquots at all, and G's own
    # tubes stay in v_box_G (asserted above) rather than showing up in R.
    occupant = str(r_rows[0]["occupants"])
    assert boxes[occupant]["kind"] == "grid9x9"
    assert "G" not in {aliquot for _, _, aliquot in placed}
    assert not any(box == "R" for box, _, _ in placed)

    # -- v_box_S: two tubes; v_box_F: zero rows ----------------------------
    assert column_names(ledger, "v_box_S") == {"occupant"}
    s_rows = ledger.driver.query("SELECT * FROM `v_box_S`")
    assert [str(row["occupant"]) for row in s_rows] == shelf_ids
    assert column_names(ledger, "v_box_F") == {"occupant"}
    assert ledger.driver.query("SELECT * FROM `v_box_F`") == []

    # -- the consumed tube's position is cleared, its history keeps it ------
    cleared = ledger.driver.query(
        "SELECT `box_id`, `slot`, `status` FROM `aliquot` WHERE `id` = %s",
        (consumed_id,),
    )
    assert cleared == [{"box_id": None, "slot": None, "status": "consumed"}]
    events = ledger.driver.query(
        "SELECT `aliquot_id`, `event`, `from_box_id`, `from_slot`, `to_box_id`, "
        "`to_slot`, `old_status`, `new_status`, `actor`, `note` "
        "FROM `aliquot_event` WHERE `event` = 'move' ORDER BY `id`"
    )
    assert len(events) == 1
    event = events[0]
    assert str(event["aliquot_id"]) == consumed_id
    assert event["from_box_id"] == boxes["H"]["id"]
    assert event["from_slot"] == "C3"
    assert event["to_box_id"] is None
    assert event["to_slot"] is None
    assert event["old_status"] == event["new_status"] == "consumed"
    assert event["actor"] == ledger.actor
    assert event["note"] == CLEARED_NOTE

    # -- validation and reconciliation -------------------------------------
    checked = tm_check(ledger)
    assert checked.exit_code == 0, checked.output
    assert "FAIL" not in checked.output
    assert "OK:" in checked.output
    planned = CliRunner().invoke(app, [*base_args(ledger), "schema", "plan"])
    assert planned.exit_code == 0, planned.output
    assert "up to date" in planned.output

    # A second run refuses: the counter is 5, not 4.
    head_after = head(ledger)
    again = run_python(SPEC_4_TO_5, "--ledger", str(ledger.path), env=script_env)
    assert again.returncode == 1, again.stdout + again.stderr
    assert "spec_version 5, expected 4" in again.stdout + again.stderr
    assert head(ledger) == head_after


@pytest.mark.parametrize(
    "case",
    [
        pytest.param("core-name", id="core-name-collision"),
        pytest.param("too-long", id="too-long"),
        pytest.param("case-duplicate", id="case-only-duplicate"),
    ],
)
def test_spec_4_to_5_refuses_view_unsafe_box_names(
    case: str,
    tmp_path: Path,
    dolt_env: dict[str, str],
    script_env: dict[str, str],
) -> None:
    """Acceptance: the three name rules are refusals that write nothing."""
    ledger = open_spec4_fixture(tmp_path, dolt_env, name=f"names-{case}")
    try:
        if case == "core-name":
            ledger.driver.execute(
                "UPDATE `box` SET `name` = 'slots' WHERE `name` = 'S'"
            )
            expected = ["'slots' would make the view 'v_box_slots'"]
        elif case == "too-long":
            name = "X" * 59
            ledger.driver.execute(
                "UPDATE `box` SET `name` = %s WHERE `name` = 'S'", (name,)
            )
            expected = ["is 59 characters long", "the limit is 58"]
        else:
            ledger.driver.execute(
                "UPDATE `box` SET `name` = 'g' WHERE `name` = 'S'"
            )
            expected = ["differs only in case", "'G'", "'g'"]
        ledger.driver.commit("test: make a box name unsafe")
        ledger.read_meta()
        _assert_refused_without_writing(ledger, script_env, expected=expected)
    finally:
        ledger.close()


def test_spec_4_to_5_refuses_a_grid_box_holding_a_box(
    tmp_path: Path,
    dolt_env: dict[str, str],
    script_env: dict[str, str],
) -> None:
    """Acceptance: a grid box holds tubes only (refusal, nothing written)."""
    ledger = open_spec4_fixture(tmp_path, dolt_env, name="grid-child")
    try:
        (grid,) = ledger.driver.query("SELECT `id` FROM `box` WHERE `name` = 'G'")
        ledger.driver.execute(
            "INSERT INTO `box` (`id`, `kind`, `name`, `parent_box_id`, "
            "`parent_slot`) VALUES ('test-inner', 'grid9x9', 'INNER', %s, 'A1')",
            (grid["id"],),
        )
        ledger.driver.execute(
            "INSERT INTO `box_grid9x9` (`id`) VALUES ('test-inner')"
        )
        ledger.driver.commit("test: nest a box in a grid box")
        ledger.read_meta()
        _assert_refused_without_writing(
            ledger,
            script_env,
            expected=["INNER is inside grid box G (grid9x9)"],
        )
    finally:
        ledger.close()


def test_spec_4_to_5_refuses_an_in_stock_aliquot_without_a_position(
    tmp_path: Path,
    dolt_env: dict[str, str],
    script_env: dict[str, str],
) -> None:
    """Lead decision: in stock means placed, so an unplaced stored tube refuses."""
    ledger = open_spec4_fixture(tmp_path, dolt_env, name="unplaced")
    try:
        target = next(
            aliquot
            for box, slot, aliquot in spec4_placed_aliquots(ledger)
            if (box, slot) == ("G", "A1")
        )
        ledger.driver.execute(
            "UPDATE `aliquot` SET `box_id` = NULL, `slot` = NULL WHERE `id` = %s",
            (target,),
        )
        ledger.driver.commit("test: an in-stock tube with no position")
        ledger.read_meta()
        _assert_refused_without_writing(
            ledger,
            script_env,
            expected=[target, "has no box", f"tm aliquot move {target}"],
        )
    finally:
        ledger.close()


def test_spec_4_to_5_refuses_an_out_of_set_aliquot_status(
    tmp_path: Path,
    dolt_env: dict[str, str],
    script_env: dict[str, str],
) -> None:
    """A status outside the stock kind's set cannot be classified, so it refuses."""
    ledger = open_spec4_fixture(tmp_path, dolt_env, name="out-of-set")
    try:
        target = next(
            aliquot
            for box, slot, aliquot in spec4_placed_aliquots(ledger)
            if (box, slot) == ("G", "B2")
        )
        ledger.driver.execute(
            "UPDATE `aliquot` SET `status` = 'lost' WHERE `id` = %s", (target,)
        )
        ledger.driver.commit("test: a status outside the stock kind's set")
        ledger.read_meta()
        _assert_refused_without_writing(
            ledger,
            script_env,
            expected=[target, "lost", "not in their stock kind's status set"],
        )
    finally:
        ledger.close()


def test_spec_4_to_5_clears_every_positioned_not_in_stock_row_in_id_order(
    tmp_path: Path,
    dolt_env: dict[str, str],
    script_env: dict[str, str],
) -> None:
    """One ``move`` event per cleared row, ordered by aliquot id."""
    ledger = open_spec4_fixture(tmp_path, dolt_env, name="clear-order")
    try:
        placed = spec4_placed_aliquots(ledger)
        by_position = {
            (box, slot): aliquot
            for box, slot, aliquot in placed
            if slot is not None
        }
        (consumed,) = ledger.driver.query(
            "SELECT `id` FROM `aliquot` WHERE `status` = 'consumed'"
        )
        first_id = str(consumed["id"])
        extra_id = by_position[("G", "A1")]
        # A second spec-4-shaped row: consumed but still placed.
        ledger.driver.execute(
            "UPDATE `aliquot` SET `status` = 'consumed' WHERE `id` = %s",
            (extra_id,),
        )
        ledger.driver.commit("test: a second consumed tube that still has a position")
        ledger.read_meta()
        assert ledger.meta["spec_version"] == "4"
        expected = sorted([first_id, extra_id])

        dry = run_python(
            SPEC_4_TO_5, "--ledger", str(ledger.path), "--dry-run", env=script_env
        )
        assert dry.returncode == 0, dry.stdout + dry.stderr
        assert dry.stdout.count("UPDATE `aliquot` SET `box_id` = NULL") == 2
        assert dry.stdout.count("INSERT INTO `aliquot_event`") == 2

        applied = run_python(SPEC_4_TO_5, "--ledger", str(ledger.path), env=script_env)
        assert applied.returncode == 0, applied.stdout + applied.stderr
        events = ledger.driver.query(
            "SELECT `aliquot_id`, `from_box_id`, `from_slot`, `to_box_id`, "
            "`to_slot`, `old_status`, `new_status`, `note` "
            "FROM `aliquot_event` WHERE `event` = 'move' ORDER BY `id`"
        )
        assert [str(event["aliquot_id"]) for event in events] == expected
        box_ids = {
            str(row["name"]): str(row["id"])
            for row in ledger.driver.query("SELECT `id`, `name` FROM `box`")
        }
        assert {
            str(event["aliquot_id"]): (
                event["from_box_id"],
                event["from_slot"],
            )
            for event in events
        } == {
            first_id: (box_ids["H"], "C3"),
            extra_id: (box_ids["G"], "A1"),
        }
        for event in events:
            assert event["to_box_id"] is None
            assert event["to_slot"] is None
            assert event["old_status"] == event["new_status"] == "consumed"
            assert event["note"] == CLEARED_NOTE
        for aliquot_id in expected:
            assert ledger.driver.query(
                "SELECT `box_id`, `slot` FROM `aliquot` WHERE `id` = %s",
                (aliquot_id,),
            ) == [{"box_id": None, "slot": None}]
        # The cleared tube no longer shows in the grid.
        (row_a,) = ledger.driver.query(
            "SELECT * FROM `v_box_G` WHERE `row` = 'A'"
        )
        assert row_a["1"] is None
    finally:
        ledger.close()


# ---------------------------------------------------------------------------
# base 3 -> 4 on the spec-5 ledger (preset rename, ticket 30)
# ---------------------------------------------------------------------------


def open_spec5_fixture(
    tmp_path: Path,
    dolt_env: dict[str, str],
    script_env: dict[str, str],
    *,
    name: str = "base-3-to-4",
) -> Ledger:
    """A copy of the spec-4 fixture after ``migration/spec/4-to-5``.

    The base 3 -> 4 step requires spec 5 and a ledger whose own base copy is
    still v3 (``grid96``); that is exactly the state ``spec/4-to-5`` leaves.
    """
    ledger_path = tmp_path / name
    copy_spec4_fixture(ledger_path)
    applied = run_python(SPEC_4_TO_5, "--ledger", str(ledger_path), env=script_env)
    assert applied.returncode == 0, applied.stdout + applied.stderr
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        ledger = open_ledger(ledger_path, env=dolt_env)
    return ledger


def test_base_3_to_4_renames_the_grid_kind_on_a_spec5_ledger(
    tmp_path: Path,
    dolt_env: dict[str, str],
    script_env: dict[str, str],
) -> None:
    """Spec 5 + base 3 -> the box kind, counter, views, check and empty plan."""
    ledger = open_spec5_fixture(tmp_path, dolt_env, script_env)
    try:
        # -- the input state: spec 5, base copy still v3, kind table grid96 ----
        assert ledger.meta["spec_version"] == "5"
        assert ledger.schema.preset_versions == {"base": 3}
        assert (ledger.path / "presets" / "base.yaml").read_bytes() == (
            BASE3_PRESET.read_bytes()
        )
        assert "box_grid96" in table_names(ledger)
        assert "box_grid8x12" not in table_names(ledger)
        views_before = spec4_view_names(ledger)
        boxes_before = {
            str(row["name"]): str(row["kind"])
            for row in ledger.driver.query("SELECT `name`, `kind` FROM `box`")
        }

        head_before = head(ledger)
        commits_before = commit_count(ledger)
        tags_before = tags(ledger)
        assert set(tags_before) == {SPEC4_TAG}

        # -- dry run: the rename plan, the tag, the counter; no writes ---------
        dry = run_python(
            BASE_3_TO_4, "--ledger", str(ledger.path), "--dry-run", env=script_env
        )
        assert dry.returncode == 0, dry.stdout + dry.stderr
        out = dry.stdout
        assert "migration: base 3 -> 4" in out
        assert f"CALL DOLT_TAG('{BASE4_TAG}'" in out
        assert "ALTER TABLE `box_grid96` RENAME TO `box_grid8x12`" in out
        assert "UPDATE `box` SET `kind` = 'grid8x12' WHERE `kind` = 'grid96'" in out
        assert '"base": 4' in out
        assert "dolt tests: replace with" in out
        assert head(ledger) == head_before
        assert commit_count(ledger) == commits_before
        # Only the spec step's restore tag exists; the base step wrote none.
        assert tags(ledger) == tags_before
        # A dry run restores the preset copies it placed: still base v3.
        assert (ledger.path / "presets" / "base.yaml").read_bytes() == (
            BASE3_PRESET.read_bytes()
        )
        assert "box_grid96" in table_names(ledger)

        # -- apply: one commit, the renamed kind, the frozen v4 copy ------------
        applied = run_python(
            BASE_3_TO_4, "--ledger", str(ledger.path), env=script_env
        )
        assert applied.returncode == 0, applied.stdout + applied.stderr
        assert BASE4_COMMIT in applied.stdout
        assert "is now at preset_versions[base] 4" in applied.stdout

        reload_schema(ledger)
        assert json.loads(ledger.meta["preset_versions"]) == {"base": 4}
        assert ledger.schema.preset_versions == {"base": 4}
        assert ledger.meta["fingerprint"] == ledger.schema.fingerprint()
        assert ledger.check_fingerprint(warn=False) is True

        tables = table_names(ledger)
        assert "box_grid8x12" in tables
        assert "box_grid96" not in tables
        assert ledger.driver.query(
            "SELECT DISTINCT `kind` FROM `box` ORDER BY `kind`"
        ) == [
            {"kind": "freezer"},
            {"kind": "grid8x12"},
            {"kind": "grid9x9"},
            {"kind": "rack"},
            {"kind": "shelf"},
        ]
        assert ledger.driver.query(
            "SELECT COUNT(*) AS `n` FROM `box` WHERE `kind` = 'grid96'"
        ) == [{"n": 0}]
        # The per-box views the spec step created are still there and unchanged
        # in shape: the rename changes the kind table, not the box views.
        assert spec4_view_names(ledger) == views_before
        assert column_names(ledger, "v_box_H") == {"row"} | {
            str(number) for number in range(1, 13)
        }
        assert len(ledger.driver.query("SELECT * FROM `v_box_H`")) == 8
        assert len(ledger.driver.query("SELECT * FROM `v_box_G`")) == 9
        assert len(ledger.driver.query("SELECT * FROM `v_box_R`")) == 10
        assert len(ledger.driver.query("SELECT * FROM `v_box_S`")) == 2
        assert ledger.driver.query("SELECT * FROM `v_box_F`") == []
        # The box names are untouched; only the grid96 rows' kind changed.
        assert {
            str(row["name"]): str(row["kind"])
            for row in ledger.driver.query("SELECT `name`, `kind` FROM `box`")
        } == {
            name: ("grid8x12" if kind == "grid96" else kind)
            for name, kind in boxes_before.items()
        }
        # The ledger now carries the frozen base v4 copy it installed.
        assert (ledger.path / "presets" / "base.yaml").read_bytes() == (
            BASE4_PRESET.read_bytes()
        )
        copy = yaml.safe_load(
            (ledger.path / "presets" / "base.yaml").read_text(encoding="utf-8")
        )
        assert copy["version"] == 4 and "grid8x12" in copy["kinds"]

        # One commit with the contract message, tagged at the pre-step commit.
        assert commit_count(ledger) == commits_before + 1
        head_after = head(ledger)
        assert head_after != head_before
        assert commit_message(ledger, head_after) == BASE4_COMMIT
        assert tags(ledger) == {**tags_before, BASE4_TAG: head_before}
        assert row_count(ledger, "dolt_tests") == expected_installed_tests(ledger)

        # -- validation and reconciliation -------------------------------------
        checked = tm_check(ledger)
        assert checked.exit_code == 0, checked.output
        assert "FAIL" not in checked.output
        assert "OK:" in checked.output
        planned = CliRunner().invoke(app, [*base_args(ledger), "schema", "plan"])
        assert planned.exit_code == 0, planned.output
        assert "up to date" in planned.output

        # A second run refuses: the counter is 4, not 3.
        again = run_python(BASE_3_TO_4, "--ledger", str(ledger.path), env=script_env)
        assert again.returncode == 1, again.stdout + again.stderr
        assert "preset_versions[base] 4, expected 3" in (
            again.stdout + again.stderr
        )
        assert head(ledger) == head_after
    finally:
        ledger.close()


def test_base_3_to_4_refuses_a_spec4_ledger(
    tmp_path: Path,
    dolt_env: dict[str, str],
    script_env: dict[str, str],
) -> None:
    """The base step runs after spec/4-to-5; a spec-4 ledger is refused."""
    ledger = open_spec4_fixture(tmp_path, dolt_env, name="base-before-spec5")
    try:
        head_before = head(ledger)
        commits_before = commit_count(ledger)
        copy_before = (ledger.path / "presets" / "base.yaml").read_bytes()
        for flags in (["--dry-run"], []):
            result = run_python(
                BASE_3_TO_4, "--ledger", str(ledger.path), *flags, env=script_env
            )
            assert result.returncode == 1, result.stdout + result.stderr
            output = result.stdout + result.stderr
            assert "refusing to migrate base 3 -> 4" in output
            assert "migration/spec/4-to-5" in output
            assert "nothing was written" in output
            # No commit, no tag, no DDL; the preset copies were restored.
            assert head(ledger) == head_before
            assert commit_count(ledger) == commits_before
            assert tags(ledger) == {}
            assert "box_grid96" in table_names(ledger)
            assert "box_grid8x12" not in table_names(ledger)
            assert (ledger.path / "presets" / "base.yaml").read_bytes() == copy_before
    finally:
        ledger.close()


# ---------------------------------------------------------------------------
# The checked-in spec-4 fixture: drift guard
# ---------------------------------------------------------------------------


def spec4_fixture_data(dolt: str, ledger: Path) -> dict[str, Any]:
    """The logical data shape of a spec-4 fixture (ids are random UUIDs)."""

    def rows(sql: str) -> list[dict[str, Any]]:
        result = subprocess.run(
            [dolt, "sql", "--result-format", "json", "-q", sql],
            cwd=str(ledger),
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return json.loads(result.stdout).get("rows", [])

    meta = {
        str(row["key"]): str(row["value"])
        for row in rows("SELECT `key`, `value` FROM `ledger_meta`")
    }
    boxes = [
        (
            str(row["name"]),
            str(row["kind"]),
            # the parent is compared by name: box ids are random UUIDs
            row.get("parent"),
            row.get("parent_slot"),
        )
        for row in rows(
            "SELECT c.`name` AS `name`, c.`kind` AS `kind`, "
            "p.`name` AS `parent`, c.`parent_slot` AS `parent_slot` "
            "FROM `box` c LEFT JOIN `box` p ON p.`id` = c.`parent_box_id` "
            "ORDER BY c.`name`"
        )
    ]
    aliquots = [
        (str(row["box"]), row.get("slot"), str(row["status"]))
        for row in rows(
            "SELECT b.`name` AS `box`, a.`slot` AS `slot`, a.`status` AS `status` "
            "FROM `aliquot` a LEFT JOIN `box` b ON b.`id` = a.`box_id` "
            "ORDER BY b.`name`, a.`slot`, a.`seq`"
        )
    ]
    slots = {
        str(row["box"]): int(row["n"])
        for row in rows(
            "SELECT b.`name` AS `box`, COUNT(s.`label`) AS `n` "
            "FROM `box` b LEFT JOIN `slot` s ON s.`box_id` = b.`id` "
            "GROUP BY b.`name` ORDER BY b.`name`"
        )
    }
    return {
        "meta": {
            key: meta.get(key)
            for key in ("spec_version", "preset_versions", "tetramark_version")
        },
        "boxes": boxes,
        "aliquots": aliquots,
        "slots": slots,
    }


def _load_build_spec4_fixture():
    spec = importlib.util.spec_from_file_location(
        "tetramark_build_spec4_fixture", BUILD_SPEC4_FIXTURE
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def rebuilt_spec4_boxes(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Rebuild the spec-4 fixture once per module; skip when impossible.

    The build needs ``git``, ``uv`` and ``dolt``, the legacy commit in this
    clone, and either network or a warm uv cache.  A machine that cannot
    materialise the legacy checkout skips the drift guard instead of failing;
    the migration tests still run from the checked-in copy.
    """
    builder = _load_build_spec4_fixture()
    missing = [name for name in ("git", "uv", "dolt") if shutil.which(name) is None]
    if missing:
        pytest.skip(f"cannot rebuild the spec-4 fixture without: {', '.join(missing)}")
    probe = subprocess.run(
        [
            "git",
            "-C",
            str(ROOT),
            "cat-file",
            "-e",
            f"{builder.LEGACY_COMMIT}^{{commit}}",
        ],
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        pytest.skip(
            f"the legacy commit {builder.LEGACY_COMMIT[:12]} is not in this clone"
        )
    target = tmp_path_factory.mktemp("spec4-boxes-rebuilt") / "spec4-boxes"
    result = subprocess.run(
        [sys.executable, str(BUILD_SPEC4_FIXTURE), "--out", str(target)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        output = (result.stdout + result.stderr).strip()
        pytest.skip(f"cannot rebuild the spec-4 fixture here:\n{output[-1200:]}")
    return target


def test_spec4_boxes_fixture_matches_its_builder(
    rebuilt_spec4_boxes: Path, dolt_binary: str, tmp_path: Path
) -> None:
    """The checked-in ledger still has the shape the builder produces.

    Compares the **logical** shape — schema fingerprint, table/view list,
    ``dolt_tests`` count, meta and the box/aliquot/slot rows — plus the
    checked-in preset files, never the ``.dolt`` bytes or the random UUIDs.
    The checked-in copy is queried from a temporary copy so the repository's
    fixture directory is never written to.
    """
    checked_in = copy_spec4_fixture(tmp_path / "checked-in")
    checked = fixture_shape(dolt_binary, checked_in)
    fresh = fixture_shape(dolt_binary, rebuilt_spec4_boxes)

    differences = [
        f"{key}: checked-in={checked[key]!r}, builder={fresh[key]!r}"
        for key in ("fingerprint", "tables", "views", "dolt_tests")
        if checked[key] != fresh[key]
    ]
    checked_data = spec4_fixture_data(dolt_binary, checked_in)
    fresh_data = spec4_fixture_data(dolt_binary, rebuilt_spec4_boxes)
    for key in ("meta", "boxes", "aliquots", "slots"):
        if checked_data[key] != fresh_data[key]:
            differences.append(
                f"{key}: checked-in={checked_data[key]!r}, "
                f"builder={fresh_data[key]!r}"
            )
    for name in ("ledger.yaml", "presets/base.yaml"):
        checked_file = SPEC4_FIXTURE / name
        if not checked_file.is_file():
            differences.append(f"{name}: missing from the checked-in fixture")
        elif checked_file.read_bytes() != (rebuilt_spec4_boxes / name).read_bytes():
            differences.append(f"{name}: differs from the builder's file byte for byte")

    assert not differences, (
        "the checked-in spec-4 fixture no longer matches what "
        f"{BUILD_SPEC4_FIXTURE.relative_to(ROOT)} produces from the 0.3.0 "
        "checkout:\n  - "
        + "\n  - ".join(differences)
        + f"\nRebuild it with: uv run python "
        f"{BUILD_SPEC4_FIXTURE.relative_to(ROOT)} --force"
    )
    # ... and it is still the pre-migration shape the step needs, not a shell.
    assert checked["fingerprint"]
    assert checked_data["meta"]["spec_version"] == "4"
    assert checked_data["meta"]["preset_versions"] == '{"base": 3}'
    assert checked_data["meta"]["tetramark_version"] == "0.3.0"
    assert "v_box_grid9x9" in checked["views"]
    assert "v_box_g" not in checked["views"]
    assert ("H", "C3", "consumed") in checked_data["aliquots"]
    # The named-view acceptance shape: rack R holds child box G in slot 1.
    assert ("G", "grid9x9", "R", "1") in checked_data["boxes"]
    assert ("R", "1", "stored") not in checked_data["aliquots"]


def test_commit_meta_read_error_propagates(monkeypatch) -> None:
    """A failed dolt_log read is the error, not a bogus version mismatch."""
    from types import SimpleNamespace

    from tetramark.migrate import _read_commit_and_meta

    class BrokenDriver:
        def query_many(self, statements):
            raise LedgerError("dolt_log unreadable: simulated")

    ledger = SimpleNamespace(driver=BrokenDriver(), meta={"version": "4"})

    with pytest.raises(LedgerError, match="dolt_log unreadable"):
        _read_commit_and_meta(ledger, "deadbeef")
    assert ledger.meta == {"version": "4"}


# ---------------------------------------------------------------------------
# The one-folder-per-counter rule (README-only steps included)
# ---------------------------------------------------------------------------

#: The step folders of one counter: ``migration/spec/`` or
#: ``migration/presets/<name>/``.
STEP_ROOT = ROOT / "migration"


def step_folders(root: Path) -> dict[str, Path]:
    """The ``<n>-to-<n+1>`` folders under ``root``, by name."""
    folders: dict[str, Path] = {}
    if not root.is_dir():
        return folders
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        parts = entry.name.split("-to-")
        if len(parts) != 2 or not (parts[0].isdigit() and parts[1].isdigit()):
            continue
        folders[entry.name] = entry
    return folders


def preset_version(path: Path) -> int:
    """The ``version:`` a preset file declares."""
    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return int(document.get("version") or 1)


@pytest.mark.fast
def test_every_counter_step_has_one_folder_with_a_readme() -> None:
    """One folder per counter bump, and a README in it (scripted or manual).

    The maintenance rule in ``migration/README.md``: whoever bumps
    ``SPEC_VERSION`` or a preset's ``version:`` adds exactly one folder for
    that step.  Every step folder holds a ``README.md``; it may also hold a
    ``migrate.py`` (a scripted step) or be **README-only** (a manual step) —
    the spec 5 -> 6 upgrade is manual, so a ``migrate.py`` must *not* be
    required and no dummy script may be invented for it.
    """
    # -- the spec counter --------------------------------------------------
    spec_folders = step_folders(STEP_ROOT / "spec")
    spec_steps = sorted(int(name.split("-to-")[0]) for name in spec_folders)
    assert spec_steps == list(range(1, len(spec_steps) + 1)), (
        "the spec steps must be contiguous from 1 to 2: "
        f"{sorted(spec_folders)}"
    )
    assert len(spec_steps) >= model.SPEC_VERSION - 1, (
        f"spec {model.SPEC_VERSION} needs a folder for every step up to "
        f"{model.SPEC_VERSION - 1} -> {model.SPEC_VERSION}: {sorted(spec_folders)}"
    )

    # -- every preset ------------------------------------------------------
    for preset_path in sorted(REPO_PRESETS.glob("*.yaml")):
        name = preset_path.stem
        version = preset_version(preset_path)
        folders = step_folders(STEP_ROOT / "presets" / name)
        steps = sorted(int(folder.split("-to-")[0]) for folder in folders)
        assert steps == list(range(1, len(steps) + 1)), (
            f"the {name} steps must be contiguous from 1 to 2: {sorted(folders)}"
        )
        assert len(steps) >= version - 1, (
            f"preset {name} is at version {version}: every step up to "
            f"{version - 1} -> {version} needs a folder: {sorted(folders)}"
        )

    # -- every step folder has a README, and is scripted or README-only ----
    for root in [STEP_ROOT / "spec", *sorted((STEP_ROOT / "presets").iterdir())]:
        for name, folder in step_folders(root).items():
            readme = folder / "README.md"
            assert readme.is_file(), f"{folder.relative_to(ROOT)} has no README.md"
            files = {entry.name for entry in folder.iterdir() if entry.is_file()}
            if "migrate.py" not in files:
                # A README-only (manual) step holds no script — not even a
                # dummy one.  A frozen preset copy next to the README is fine.
                stray = {
                    entry
                    for entry in files
                    if not entry.endswith((".md", ".yaml", ".yml"))
                }
                assert not stray, (
                    f"{folder.relative_to(ROOT)} is a README-only step and must "
                    f"not hold {sorted(stray)}"
                )


# ---------------------------------------------------------------------------
# Tetramark-500: base 5 -> 6, the capacity-free slotless containers
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_base_5_and_base_6_frozen_copies_keep_versions_and_capacities() -> None:
    """T2: the historical copies cannot drift with the shipped base preset.

    ``4-to-5`` installs the frozen v5 content (a bound on the slotless kinds),
    ``5-to-6`` installs the frozen v6 content (no bound), and the shipped base
    is the v6 file.  The mouse ``cage`` keeps its own capacity 5: a cage's
    occupant limit is a property of the kind, unlike a shelf/freezer/drawer.
    """
    base_5 = yaml.safe_load(BASE_5.read_text(encoding="utf-8"))
    assert base_5["version"] == 5
    for kind, capacity in (("shelf", 12), ("freezer", 6), ("drawer", 1)):
        assert base_5["kinds"][kind]["geometry"] == {
            "type": "slotless",
            "capacity": capacity,
        }, kind
        # The historical descriptions still promise the bound.
        assert "holds" in base_5["kinds"][kind]["description"], kind

    base_6 = yaml.safe_load(BASE_6.read_text(encoding="utf-8"))
    assert base_6["version"] == 6
    for kind in ("shelf", "freezer", "drawer", "incubator"):
        assert base_6["kinds"][kind]["geometry"] == {"type": "slotless"}, kind

    shipped = REPO_PRESETS / "base.yaml"
    assert BASE_6.read_bytes() == shipped.read_bytes()
    assert yaml.safe_load(shipped.read_text(encoding="utf-8"))["version"] == 6

    mouse = yaml.safe_load(
        (REPO_PRESETS / "mouse.yaml").read_text(encoding="utf-8")
    )
    assert mouse["kinds"]["cage"]["geometry"] == {
        "type": "slotless",
        "capacity": 5,
    }


@pytest.mark.fast
def test_manual_base_step_readmes_copy_their_frozen_preset_files() -> None:
    """T3: both manual base steps copy their frozen file, never the checkout's."""
    readme_4_to_5 = (
        ROOT / "migration" / "presets" / "base" / "4-to-5" / "README.md"
    ).read_text(encoding="utf-8")
    assert (
        'cp migration/presets/base/4-to-5/base-5.yaml "$LEDGER/presets/base.yaml"'
        in readme_4_to_5
    )
    assert "cp presets/base.yaml" not in readme_4_to_5
    assert not (BASE_5.parent / "migrate.py").exists()

    readme_5_to_6 = BASE_5_TO_6_README.read_text(encoding="utf-8")
    assert (
        'cp migration/presets/base/5-to-6/base-6.yaml "$LEDGER/presets/base.yaml"'
        in readme_5_to_6
    )
    assert "schema is up to date" in readme_5_to_6
    assert "ledger_meta is stale" in readme_5_to_6
    assert "schema apply" in readme_5_to_6
    assert "apply(schema): refresh ledger_meta" in readme_5_to_6
    # The step is README-only: no runner, no dummy migrate.py.
    assert not (BASE_6.parent / "migrate.py").exists()


def test_base_5_to_6_manual_step_lifts_the_slotless_capacity(
    embedded_ledger_factory, tmp_path: Path
) -> None:
    """T1: a real base-5 ledger refuses the second drawer box until it upgrades.

    Follows ``migration/presets/base/5-to-6/README.md``: copy the frozen v6
    file, plan (up to date + stale meta), apply (one metadata-only commit), then
    the previously refused write succeeds and ``tm check`` still has no FAIL.
    """
    ledger = embedded_ledger_factory(
        [BASE_5], name="base-5-capacity", real_init=True
    )
    assert ledger.schema.preset_versions == {"base": 5}
    ledger.boxes.add("drawer", "DRAWER")
    ledger.boxes.add("rack", "RACK-1", parent="DRAWER")
    with pytest.raises(LedgerError, match=r"box 'DRAWER' is full \(capacity 1"):
        ledger.boxes.add("rack", "RACK-2", parent="DRAWER")

    tables_before = table_names(ledger)
    counts_before = row_counts(ledger, tables_before)
    head_before = head(ledger)
    commits_before = commit_count(ledger)

    # The README's step 1: replace the ledger's copy with the frozen v6 file.
    shutil.copyfile(BASE_6, ledger.path / "presets" / "base.yaml")
    ledger.close()
    reopened = open_ledger(ledger.path)
    try:
        assert reopened.schema.preset_versions == {"base": 6}
        planned = CliRunner().invoke(app, [*base_args(reopened), "schema", "plan"])
        assert planned.exit_code == 0, planned.output
        assert "schema is up to date" in planned.output
        assert "ledger_meta is stale" in planned.output
        applied = CliRunner().invoke(
            app, [*base_args(reopened), "schema", "apply"]
        )
        assert applied.exit_code == 0, applied.output
        assert "Applied" in applied.output
    finally:
        reopened.close()

    migrated = open_ledger(ledger.path)
    try:
        # The counter, fingerprint and ledger.yaml copy moved in one commit.
        assert migrated.schema.preset_versions == {"base": 6}
        assert json.loads(migrated.meta["preset_versions"]) == {"base": 6}
        assert migrated.check_fingerprint(warn=False) is True
        assert commit_count(migrated) == commits_before + 1
        assert commit_message(migrated, head(migrated)) == (
            "apply(schema): refresh ledger_meta"
        )
        assert tags(migrated) == {}
        # No DDL and no row moves: the table set and every row count are intact.
        assert table_names(migrated) == tables_before
        assert row_counts(migrated, tables_before) == counts_before
        assert "capacity" not in (
            migrated.path / "presets" / "base.yaml"
        ).read_text(encoding="utf-8")

        # The write the old bound refused now succeeds.
        migrated.boxes.add("rack", "RACK-2", parent="DRAWER")
        checked = tm_check(migrated)
        assert checked.exit_code == 0, checked.output
        assert "FAIL" not in checked.output
        planned = CliRunner().invoke(app, [*base_args(migrated), "schema", "plan"])
        assert planned.exit_code == 0, planned.output
        assert "up to date" in planned.output
    finally:
        migrated.close()


@pytest.mark.fast
def test_manual_step_readme_points_at_the_next_manual_step() -> None:
    """A step behind its ledger names the next (manual) step's README."""
    from tetramark.migrate import manual_step_readme

    def step(kind: str, from_version: int, preset: str | None = None):
        return MigrationStep(
            kind=kind, from_version=from_version, plan=lambda ledger: [], preset=preset
        )

    # spec 4 -> 5 on a spec-5 ledger: the next step is the manual 5 -> 6.
    assert manual_step_readme(step("spec", 4)) == "migration/spec/5-to-6/README.md"
    # A preset step: the next step's README, in the preset's own folder.
    assert (
        manual_step_readme(step("preset", 3, preset="base"))
        == "migration/presets/base/4-to-5/README.md"
    )
    # T5 (Tetramark-500): base 5 -> 6 is the next manual step after 4 -> 5.
    assert (
        manual_step_readme(step("preset", 4, preset="base"))
        == "migration/presets/base/5-to-6/README.md"
    )
    # A scripted next step keeps the plain counter message: there the
    # migrate.py is the thing to run, not a README to follow.
    assert manual_step_readme(step("spec", 3)) is None
    assert manual_step_readme(step("preset", 2, preset="base")) is None


@pytest.mark.fast
def test_the_manual_spec_step_readme_quotes_the_check_and_the_remap() -> None:
    """The README must quote the SQL the spec-6 contract states."""
    from test_fixed_status_model import MIGRATION_CHECK

    readme = (
        ROOT / "migration" / "spec" / "5-to-6" / "README.md"
    ).read_text(encoding="utf-8")

    # The very statement the tool's contract adds, character for character.
    assert MIGRATION_CHECK in readme

    # Every old word of the per-kind vocabularies is remapped, and the README
    # says which fixed name it becomes.
    remap = {
        "planned": "pending",
        "alive": "stored",
        "crossed": "stored",
        "growing": "stored",
        "starved": "stored",
        "lost": "missing",
        "thawed": "consumed",
        "euthanized": "consumed",
        "found_dead": "consumed",
        "transferred": "consumed",
        "cancelled": "consumed",
        "discarded": "consumed",
        "contaminated": "consumed",
    }
    for old, new in remap.items():
        assert f"'{old}'" in readme, old
        assert f"'{new}'" in readme, new

    # One migrated event per remapped aliquot, with the old word in the note.
    assert "migrated: " in readme
    assert "`old_status`" in readme
    assert "`new_status`" in readme
    # The past is never rewritten, and no property is backfilled.
    assert "never rewritten" in readme
    assert "not backfilled" in readme


def test_a_step_behind_its_ledger_points_at_the_manual_readme(
    embedded_ledger_factory, script_env: dict[str, str]
) -> None:
    """A scripted step on a newer ledger refuses and names the next README."""
    ledger = embedded_ledger_factory(
        [REPO_PRESETS / "base.yaml"], name="behind-the-ledger", id_prefix="77"
    )
    # A spec-5 ledger: the spec 4 -> 5 step is behind it.
    ledger.driver.execute(
        "UPDATE `ledger_meta` SET `value` = '5' WHERE `key` = 'spec_version'"
    )
    ledger.driver.execute(
        "UPDATE `ledger_meta` SET `value` = %s WHERE `key` = 'preset_versions'",
        (json.dumps({"base": 4}),),
    )
    ledger.driver.commit("test: pretend a spec-5 ledger at base 4")
    ledger.read_meta()
    head_before = head(ledger)
    commits_before = commit_count(ledger)

    result = run_python(SPEC_4_TO_5, "--ledger", str(ledger.path), env=script_env)
    assert result.returncode == 1, result.stdout + result.stderr
    output = result.stdout + result.stderr
    assert "refusing to run migration spec 4 -> 5" in output
    assert "migration/spec/5-to-6/README.md" in output
    # Nothing was written: no tag, no commit, no DDL.
    assert head(ledger) == head_before
    assert commit_count(ledger) == commits_before
    assert "pre-migrate-spec-4" not in tags(ledger)

    # The same for a preset step: base 3 -> 4 on a base-4 ledger.
    result = run_python(BASE_3_TO_4, "--ledger", str(ledger.path), env=script_env)
    assert result.returncode == 1, result.stdout + result.stderr
    output = result.stdout + result.stderr
    assert "refusing to run migration base 3 -> 4" in output
    assert "migration/presets/base/4-to-5/README.md" in output
    assert head(ledger) == head_before
    assert commit_count(ledger) == commits_before


# ---------------------------------------------------------------------------
# spec/6-to-7: aliquot_event.message replaces note/purpose
# ---------------------------------------------------------------------------

SPEC_6_TO_7 = ROOT / "migration" / "spec" / "6-to-7" / "migrate.py"

#: The spec-7 ``aliquot_event`` column order the step must produce.
SPEC7_EVENT_COLUMNS = (
    "id",
    "aliquot_id",
    "event",
    "amount",
    "unit",
    "from_box_id",
    "from_slot",
    "to_box_id",
    "to_slot",
    "old_status",
    "new_status",
    "forced",
    "actor",
    "message",
    "at",
)


def spec6_fingerprint(ledger: Ledger) -> str:
    """The fingerprint a spec-6 tool would have written (SPEC_VERSION=6)."""
    saved = model.SPEC_VERSION
    model.SPEC_VERSION = 6
    try:
        return ledger.schema.fingerprint()
    finally:
        model.SPEC_VERSION = saved


def downgrade_to_spec6(
    ledger: Ledger,
    *,
    note: bool = True,
    message: bool = False,
    purpose: bool = True,
    name: str = "spec6",
) -> None:
    """Turn a fresh spec-7 ledger into a spec-6 (or hand-patched) event shape.

    ``note``/``message``/``purpose`` say which event-text columns the ledger
    ends up with: the normal spec-6 shape is ``note + purpose``; the matrix
    tests pick the ambiguous (both) and damaged (neither) shapes from the same
    helper.  The counter and fingerprint are the spec-6 ones, so the ledger
    looks exactly like one written by 0.8.7.
    """
    statements: list[Any] = []
    if note and message:
        # The ambiguous hand-patched shape: both text columns exist.
        statements.append(
            "ALTER TABLE `aliquot_event` ADD COLUMN `note` MEDIUMTEXT"
        )
    elif note:
        statements.append(
            "ALTER TABLE `aliquot_event` RENAME COLUMN `message` TO `note`"
        )
    elif not message:
        statements.append("ALTER TABLE `aliquot_event` DROP COLUMN `message`")
    if purpose:
        statements.append(
            "ALTER TABLE `aliquot_event` ADD COLUMN `purpose` MEDIUMTEXT "
            "AFTER `actor`"
        )
    statements.extend(
        [
            "UPDATE `ledger_meta` SET `value` = '6' WHERE `key` = 'spec_version'",
            "UPDATE `ledger_meta` SET `value` = '0.8.7' "
            "WHERE `key` = 'tetramark_version'",
            (
                "UPDATE `ledger_meta` SET `value` = %s WHERE `key` = 'fingerprint'",
                (spec6_fingerprint(ledger),),
            ),
        ]
    )
    ledger.driver.execute_many(statements)
    ledger.driver.commit(f"test: simulate the spec-6 shape ({name})")
    ledger.read_meta()


def spec6_ledger(factory, *, name: str = "spec6", **shape: bool):
    """A ledger with one stock, one placed tube, and a spec-6 event table."""
    ledger = factory([PRESETS / "base"], name=name, id_prefix="77")
    item = ledger.entities.add("item", name="alpha")
    shelf = ledger.boxes.add("shelf", "SHELF")
    stock = ledger.stocks.add(
        "lot", entity=item["id"], aliquots=1, box="SHELF", quantity=10, unit="ul"
    )
    downgrade_to_spec6(ledger, name=name, **shape)
    return ledger, stock["aliquots"][0]


def add_spec6_event(
    ledger: Ledger,
    alq: str,
    *,
    purpose: str | None,
    note: str | None,
    text_column: str = "note",
) -> None:
    """One hand-written event row with the spec-6 text columns.

    ``text_column`` is ``note`` for the normal spec-6 shape and ``message`` for
    the hand-patched ``message + purpose`` matrix row.
    """
    ledger.driver.execute(
        f"INSERT INTO `aliquot_event` (`aliquot_id`, `event`, `purpose`, "
        f"`{text_column}`) VALUES (%s, 'edit', %s, %s)",
        (alq, purpose, note),
    )


def event_columns_in_order(ledger: Ledger) -> list[str]:
    rows = ledger.driver.query(
        "SELECT `column_name` AS `name` FROM `information_schema`.`columns` "
        "WHERE `table_schema` = DATABASE() AND `table_name` = 'aliquot_event' "
        "ORDER BY `ordinal_position`"
    )
    return [str(row["name"]) for row in rows]


def test_spec_6_to_7_renames_folds_and_drops_in_one_commit(
    ledger_factory, script_env: dict[str, str]
) -> None:
    """Apply: the rename, all four fold cases, the drop, one commit and a tag."""
    ledger, alq = spec6_ledger(ledger_factory, name="spec6-fold")
    # All four fold cases, plus the whitespace-only variants: "empty" means
    # NULL or TRIM(x) = ''; the values are copied verbatim, never trimmed.
    add_spec6_event(ledger, alq, purpose=None, note="note only")
    add_spec6_event(ledger, alq, purpose="purpose only", note=None)
    add_spec6_event(ledger, alq, purpose="why", note="the note")
    add_spec6_event(ledger, alq, purpose="   ", note="whitespace purpose")
    add_spec6_event(ledger, alq, purpose=None, note="   ")
    ledger.driver.commit("test: seed the fold cases")
    ledger.read_meta()
    assert "purpose" in column_names(ledger, "aliquot_event")

    head_before = head(ledger)
    commits_before = commit_count(ledger)
    head_before_columns = event_columns_in_order(ledger)

    applied = run_python(
        SPEC_6_TO_7, "--ledger", str(ledger.path), "--apply", env=script_env
    )
    assert applied.returncode == 0, applied.stdout + applied.stderr
    assert "purpose folded into message: 2 row(s)" in applied.stdout
    assert "migrate(ledger): spec 6 -> 7" in applied.stdout

    ledger.read_meta()
    assert ledger.meta["spec_version"] == "7"
    assert ledger.meta["tetramark_version"] == __version__
    assert ledger.meta["fingerprint"] == ledger.schema.fingerprint()

    columns = column_names(ledger, "aliquot_event")
    assert "note" not in columns and "purpose" not in columns
    assert "message" in columns
    assert event_columns_in_order(ledger) == list(SPEC7_EVENT_COLUMNS)
    assert event_columns_in_order(ledger) != head_before_columns

    messages = [
        row["message"]
        for row in ledger.driver.query(
            "SELECT `message` FROM `aliquot_event` ORDER BY `id`"
        )
    ]
    # The ledger's own create event comes first (no text), then the five rows.
    assert messages[:3] == [None, "note only", "purpose only"]
    assert messages[3] == "why; the note"
    assert messages[4] == "whitespace purpose"
    assert messages[5] == "   "

    assert commit_count(ledger) == commits_before + 1
    assert commit_message(ledger, head(ledger)) == "migrate(ledger): spec 6 -> 7"
    assert tags(ledger) == {"pre-migrate-spec-6": head_before}
    assert row_count(ledger, "dolt_tests") == expected_installed_tests(ledger)

    checked = tm_check(ledger)
    assert checked.exit_code == 0, checked.output
    assert "FAIL" not in checked.output


def test_spec_6_to_7_dry_run_is_the_default_and_writes_nothing(
    ledger_factory, script_env: dict[str, str]
) -> None:
    """No ``--apply`` is a dry run; ``--dry-run`` prints exactly the same plan."""
    ledger, alq = spec6_ledger(ledger_factory, name="spec6-dry")
    add_spec6_event(ledger, alq, purpose="why", note="kept")
    ledger.driver.commit("test: one foldable row")
    ledger.read_meta()
    head_before = head(ledger)
    commits_before = commit_count(ledger)
    tags_before = tags(ledger)

    plain = run_python(SPEC_6_TO_7, "--ledger", str(ledger.path), env=script_env)
    assert plain.returncode == 0, plain.stdout + plain.stderr
    assert "migration: spec 6 -> 7" in plain.stdout
    assert "purpose folded into message: 1 row(s)" in plain.stdout
    assert "RENAME COLUMN `note` TO `message`" in plain.stdout
    assert "DROP COLUMN `purpose`" in plain.stdout
    assert "'spec_version', '7'" in plain.stdout
    assert "dry run:" in plain.stdout
    # The dry run writes nothing: HEAD, commits and tags are unchanged.
    assert head(ledger) == head_before
    assert commit_count(ledger) == commits_before
    assert tags(ledger) == tags_before
    ledger.read_meta()
    assert ledger.meta["spec_version"] == "6"
    assert "purpose" in column_names(ledger, "aliquot_event")

    explicit = run_python(
        SPEC_6_TO_7, "--ledger", str(ledger.path), "--dry-run", env=script_env
    )
    assert explicit.returncode == 0, explicit.stdout + explicit.stderr
    assert explicit.stdout == plain.stdout
    assert head(ledger) == head_before


@pytest.mark.parametrize(
    ("shape", "expected", "forbidden"),
    [
        (
            {"note": True, "purpose": True},
            [
                "RENAME COLUMN `note` TO `message`",
                "DROP COLUMN `purpose`",
                "CONCAT(`purpose`, '; ', `message`)",
            ],
            ["MODIFY COLUMN `message`"],
        ),
        (
            {"note": True, "purpose": False},
            ["RENAME COLUMN `note` TO `message`"],
            ["DROP COLUMN `purpose`", "MODIFY COLUMN `message`"],
        ),
        (
            {"note": False, "message": True, "purpose": True},
            ["DROP COLUMN `purpose`", "CONCAT(`purpose`, '; ', `message`)"],
            ["RENAME COLUMN `note` TO `message`", "MODIFY COLUMN `message`"],
        ),
        (
            {"note": False, "message": True, "purpose": False},
            ["MODIFY COLUMN `message` MEDIUMTEXT AFTER `actor`"],
            ["RENAME COLUMN `note` TO `message`", "DROP COLUMN `purpose`"],
        ),
    ],
    ids=["note+purpose", "note only", "message+purpose", "message only"],
)
def test_spec_6_to_7_matrix_plans_each_live_shape(
    ledger_factory,
    script_env: dict[str, str],
    shape: dict[str, bool],
    expected: list[str],
    forbidden: list[str],
) -> None:
    """Acceptance 5: the four migratable matrix rows build the right statements."""
    ledger, alq = spec6_ledger(ledger_factory, name="spec6-matrix", **shape)
    if shape.get("purpose"):
        add_spec6_event(
            ledger,
            alq,
            purpose="why",
            note="kept",
            text_column="note" if shape.get("note") else "message",
        )
        ledger.driver.commit("test: one foldable row")
        ledger.read_meta()
    result = run_python(SPEC_6_TO_7, "--ledger", str(ledger.path), env=script_env)
    assert result.returncode == 0, result.stdout + result.stderr
    for text in expected:
        assert text in result.stdout, result.stdout
    for text in forbidden:
        assert text not in result.stdout, result.stdout
    assert "migration: spec 6 -> 7" in result.stdout


@pytest.mark.parametrize(
    ("shape", "reason"),
    [
        (
            {"note": True, "message": True, "purpose": True},
            "both `note` and `message`",
        ),
        (
            {"note": False, "message": False, "purpose": True},
            "neither `note` nor `message`",
        ),
    ],
    ids=["ambiguous", "damaged"],
)
def test_spec_6_to_7_matrix_refusals_write_nothing(
    ledger_factory,
    script_env: dict[str, str],
    shape: dict[str, bool],
    reason: str,
) -> None:
    """Acceptance 5: the two refusal rows write nothing, not even the tag."""
    ledger, _alq = spec6_ledger(ledger_factory, name="spec6-refuse", **shape)
    head_before = head(ledger)
    commits_before = commit_count(ledger)

    for flags in ((), ("--apply",)):
        result = run_python(
            SPEC_6_TO_7, "--ledger", str(ledger.path), *flags, env=script_env
        )
        assert result.returncode == 1, result.stdout + result.stderr
        output = result.stdout + result.stderr
        assert "refusing to migrate spec 6 -> 7" in output
        assert reason in output
        assert head(ledger) == head_before
        assert commit_count(ledger) == commits_before
        assert tags(ledger) == {}
    ledger.read_meta()
    assert ledger.meta["spec_version"] == "6"


def test_spec_6_to_7_refuses_a_spec_7_ledger_by_the_counter(
    ledger_factory, script_env: dict[str, str]
) -> None:
    """The (d) case: a fresh, already spec-7 ledger is not stepped twice."""
    ledger = ledger_factory([PRESETS / "base"], name="already-spec7", id_prefix="77")
    ledger.entities.add("item", name="alpha")
    head_before = head(ledger)
    commits_before = commit_count(ledger)

    result = run_python(SPEC_6_TO_7, "--ledger", str(ledger.path), env=script_env)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "spec_version 7, expected 6" in result.stdout + result.stderr
    assert head(ledger) == head_before
    assert commit_count(ledger) == commits_before
    assert tags(ledger) == {}


# ---------------------------------------------------------------------------
# The spec-7 event guard: three paths refuse a spec-6 ledger by name
# ---------------------------------------------------------------------------


def spec6_guard_message(ledger: Ledger) -> str:
    """The exact LedgerError text the three spec-7 paths must raise."""
    return (
        "ledger is spec 6, tm is spec 7: aliquot_event still has the spec-6 "
        "columns (note, purpose). Upgrade it with:\n"
        f"uv run migration/spec/6-to-7/migrate.py --ledger {ledger.path} --apply\n"
        "(dry run without --apply; see migration/spec/6-to-7/README.md)"
    )


def test_the_spec7_event_guard_refuses_show_and_history(
    ledger_factory, script_env: dict[str, str]
) -> None:
    """``show_aliquot`` and the history event read name the upgrade, not a DB error."""
    ledger, alq = spec6_ledger(ledger_factory, name="spec6-guard-reads")
    expected = spec6_guard_message(ledger)

    with pytest.raises(LedgerError) as shown:
        ledger.aliquots.show(alq)
    assert str(shown.value) == expected
    assert shown.value.exit_code == 1

    with pytest.raises(LedgerError) as historic:
        ledger.history(alq)
    assert str(historic.value) == expected

    # The CLI surfaces the same text for both commands.
    for command in (("aliquot", "show", alq), ("history", alq)):
        result = CliRunner().invoke(app, [*base_args(ledger), *command])
        assert result.exit_code == 1, result.output
        assert expected in str(result.exception)


def test_the_spec7_event_guard_refuses_every_event_write(
    ledger_factory, script_env: dict[str, str], tmp_path: Path
) -> None:
    """use/move/status/import abort before the INSERT; nothing is committed."""
    from tetramark.ops.io_csv import import_csv

    ledger, alq = spec6_ledger(ledger_factory, name="spec6-guard-writes")
    expected = spec6_guard_message(ledger)
    box = ledger.driver.query("SELECT `name` FROM `box`")[0]["name"]
    head_before = head(ledger)
    commits_before = commit_count(ledger)
    events_before = row_count(ledger, "aliquot_event")
    quantity_before = ledger.driver.query(
        "SELECT `quantity`, `status`, `box_id`, `slot` FROM `aliquot` WHERE `id` = %s",
        (alq,),
    )

    for flags in (("aliquot", "use", alq, "1", "ul", "-m", "x"),
                  ("aliquot", "move", alq, "--box", str(box), "-m", "x"),
                  ("aliquot", "status", alq, "missing", "--note", "x")):
        result = CliRunner().invoke(app, [*base_args(ledger), *flags])
        assert result.exit_code == 1, result.output
        assert str(result.exception) == expected

    # An import that creates an aliquot goes through record_event too.
    csv_path = tmp_path / "lots.csv"
    csv_path.write_text(
        "entity,aliquots,quantity,unit\n" f"{ledger.stocks.ls('lot')[0]['entity_id']},1,5,ul\n",
        encoding="utf-8",
    )
    with pytest.raises(LedgerError) as refused:
        import_csv(ledger, "lot", csv_path)
    assert str(refused.value) == expected

    # Nothing was committed and no row moved.
    assert head(ledger) == head_before
    assert commit_count(ledger) == commits_before
    assert row_count(ledger, "aliquot_event") == events_before
    assert ledger.driver.query(
        "SELECT `quantity`, `status`, `box_id`, `slot` FROM `aliquot` WHERE `id` = %s",
        (alq,),
    ) == quantity_before


def test_a_spec_6_ledger_keeps_the_warn_only_commands(
    ledger_factory, script_env: dict[str, str]
) -> None:
    """``tm status``, ``tm schema plan`` and the list commands still work."""
    ledger, _alq = spec6_ledger(ledger_factory, name="spec6-warn-only")

    for command in (("status",), ("schema", "plan"), ("aliquot", "list")):
        result = CliRunner().invoke(app, [*base_args(ledger), *command])
        assert result.exit_code == 0, (command, result.output)
