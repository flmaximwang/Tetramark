"""``migration/`` steps and the shared runner (ticket 21, spec §5).

Every test uses a **real** Dolt database (no mocks): the spec/1-to-2 step and
the rename-map hook are exercised end to end, either embedded or through a
worker's ``dolt sql-server`` (the driver the ticket-24 migration will use).

The spec-1 fixture is built from the current tool (init, then downgrade the
generated views to their pre-``861faae`` shape and stamp ``spec_version=1``
with the fingerprint a spec-1 tool would have written), which the ticket allows
as an alternative to checking in a historical DDL dump.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import warnings
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from tetramark import __version__
from tetramark.cli.app import app
from tetramark.core.ledger import Ledger, build_id_strategies, open as open_ledger
from tetramark.errors import UsageError
from tetramark.migrate import (
    MigrationStep,
    counter,
    load_renames,
    open_target,
    run,
)
from tetramark.schema.ddl import view_statements
from tetramark.schema.diff import RenameMap, apply, plan
from tetramark.schema.loader import load_ledger_schema

import tetramark.schema.model as model

from conftest import PRESETS

ROOT = Path(__file__).resolve().parents[1]
MIGRATE_SCRIPT = ROOT / "migration" / "spec" / "1-to-2" / "migrate.py"


def base_args(ledger: Ledger) -> list[str]:
    return [
        "--workspace",
        str(ledger.workspace.data_dir),
        "--ledger",
        ledger.name,
    ]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(
    params=[
        pytest.param("embedded", id="embedded"),
        pytest.param("server", id="server", marks=pytest.mark.server),
    ]
)
def ledger_factory(request):
    """Both driver backends: the step must be driver-agnostic."""
    if request.param == "server":
        return request.getfixturevalue("server_ledger_factory")
    return request.getfixturevalue("embedded_ledger_factory")


@pytest.fixture
def script_env(dolt_env: dict[str, str]) -> dict[str, str]:
    """The environment a ``migration/…/migrate.py`` subprocess runs with."""
    env = {**os.environ, **dolt_env}
    env.pop("TETRAMARK_WORKSPACE", None)
    return env


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
    for statement in view_statements(ledger.schema):
        ledger.driver.execute(f"DROP VIEW `{statement.name}`")
        ledger.driver.execute(v1_view_sql(statement.sql))
    fingerprint = spec1_fingerprint(ledger)
    ledger.driver.execute(
        "UPDATE `ledger_meta` SET `value` = '1' WHERE `key` = 'spec_version'"
    )
    ledger.driver.execute(
        "UPDATE `ledger_meta` SET `value` = '0.1.1' "
        "WHERE `key` = 'tetramark_version'"
    )
    ledger.driver.execute(
        "UPDATE `ledger_meta` SET `value` = %s WHERE `key` = 'fingerprint'",
        (fingerprint,),
    )
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


def write_step(
    directory: Path,
    *,
    kind: str,
    from_version: int,
    statements: list[str],
    preset: str | None = None,
) -> Path:
    """A throwaway ``migrate.py`` importing the shared runner."""
    script = directory / "step.py"
    script.write_text(
        "from tetramark.migrate import MigrationStep, main\n"
        "\n"
        "def plan(ledger):\n"
        f"    return {statements!r}\n"
        "\n"
        f"step = MigrationStep(kind={kind!r}, from_version={from_version!r}, "
        f"preset={preset!r}, plan=plan)\n"
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
    for statement in view_statements(ledger.schema):
        expected = (
            "occupant_alias_list"
            if statement.name == "v_box_slots"
            else "alias_list"
        )
        assert expected in column_names(ledger, statement.name), statement.name
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


@pytest.mark.server
def test_failed_step_prints_last_good_commit_and_restore_command(
    server_ledger_factory, script_env: dict[str, str], tmp_path: Path
) -> None:
    """Verify contract 5: DDL is not transactional, no commit, report printed."""
    ledger = server_ledger_factory([PRESETS / "base"])
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


@pytest.mark.server
def test_preset_step_bumps_preset_versions_counter(
    server_ledger_factory, script_env: dict[str, str], tmp_path: Path
) -> None:
    """The helper's preset branch (ticket 22 will use it): counter + message."""
    ledger = server_ledger_factory([PRESETS / "base"])
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


@pytest.mark.server
def test_migration_can_run_on_a_branch(
    server_ledger_factory, script_env: dict[str, str]
) -> None:
    """``--branch``: the commit lands on the branch, the checkout stays put."""
    ledger = server_ledger_factory([PRESETS / "base"])
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

    branch = open_ledger(ledger.workspace, ledger.name, branch="upgrade")
    try:
        branch.read_meta()
        assert branch.meta["spec_version"] == "2"
        branch_head = head(branch)
        assert branch_head != main_head
        assert commit_message(branch, branch_head) == "migrate(ledger): spec 1 -> 2"
        assert tags(branch) == {"pre-migrate-spec-1": main_head}
    finally:
        branch.close()


@pytest.mark.server
def test_current_tm_opens_a_spec_1_ledger_with_a_fingerprint_warning(
    server_ledger_factory,
) -> None:
    """The documented fact: no spec guard, a fingerprint warning, writes allowed."""
    ledger = server_ledger_factory([PRESETS / "base"])
    downgrade_to_spec1(ledger)
    spec1_head = head(ledger)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        reopened = open_ledger(ledger.workspace, ledger.name)
    try:
        messages = [str(item.message) for item in caught]
        assert any("fingerprint does not match" in text for text in messages), messages
        assert reopened.warnings  # collected on the ledger too
        assert reopened.check_fingerprint(warn=False) is False
        # Opening does not rewrite, refuse or upgrade anything.
        assert reopened.meta["spec_version"] == "1"
        assert head(reopened) == spec1_head
        # The fingerprint warning is the only notice: no spec_version guard.
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
            "aliquot_fields": {"glycerol_pct": {"type": "decimal", "default": 15}},
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
        "aliquot_fields": {"glycerol_pct": {"type": "decimal", "default": 15}},
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


def write_preset(directory: Path, kinds: dict[str, Any], *, version: int) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "preset.yaml"
    path.write_text(
        yaml.safe_dump(
            {"preset": "rn", "version": version, "kinds": kinds},
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def reload_schema(ledger: Ledger) -> None:
    """Re-read ledger.yaml + the preset, as a fresh ``open`` would."""
    config, schema = load_ledger_schema(ledger.path)
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
        "INSERT INTO `aliquot_bacterial` (`id`, `glycerol_pct`) VALUES ('S1.1', 15)"
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


@pytest.mark.server
def test_rename_map_renames_kind_tables_and_kind_values_without_data_loss(
    server_ledger_factory, tmp_path: Path
) -> None:
    """Verify bullet 2: map-driven rename == ``renamed_from``, rows preserved."""
    preset_map = tmp_path / "preset_map"
    preset_rf = tmp_path / "preset_renamed_from"
    write_preset(preset_map, OLD_PRESET["kinds"], version=1)
    write_preset(preset_rf, OLD_PRESET["kinds"], version=1)
    ledger_map = server_ledger_factory([preset_map], name="rename-map")
    ledger_rf = server_ledger_factory([preset_rf], name="rename-renamed-from")
    for ledger in (ledger_map, ledger_rf):
        seed_rename_data(ledger)

    write_preset(preset_map, NEW_KINDS, version=2)
    write_preset(preset_rf, renamed_from_kinds(), version=2)
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
        ("rename_table", "aliquot_bacterial_glycerol"),
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
        assert "aliquot_bacterial_glycerol" in tables
        for gone in (
            "entity_construct",
            "stock_bacterial",
            "aliquot_bacterial",
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
        extension_rows = ledger.driver.query(
            "SELECT `id`, `glycerol_pct` FROM `aliquot_bacterial_glycerol`"
        )
        assert [row["id"] for row in extension_rows] == ["S1.1"]
        assert float(extension_rows[0]["glycerol_pct"]) == 15.0
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


@pytest.mark.server
def test_open_target_rejects_a_workspace_directory(
    server_ledger_factory,
) -> None:
    ledger = server_ledger_factory([PRESETS / "base"])
    with pytest.raises(UsageError):
        open_target(ledger.workspace.data_dir)


@pytest.mark.server
def test_run_refuses_a_dirty_working_set(
    server_ledger_factory, script_env: dict[str, str]
) -> None:
    """A migration commit must be the only change in its commit."""
    ledger = server_ledger_factory([PRESETS / "base"])
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


@pytest.mark.server
def test_counter_reads_spec_and_preset_counters(
    server_ledger_factory,
) -> None:
    ledger = server_ledger_factory([PRESETS / "base"])
    spec_step = MigrationStep(
        kind="spec", from_version=1, plan=lambda opened: ["SELECT 1"]
    )
    preset_step = MigrationStep(
        kind="preset", preset="base", from_version=1, plan=lambda opened: ["SELECT 1"]
    )
    assert counter(spec_step, ledger) == 2
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
