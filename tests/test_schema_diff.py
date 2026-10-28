"""Schema plan/apply against real Dolt databases (ticket 09).

Every test mutates the YAML preset of an initialised ledger that already holds
data, checks the tags of the resulting plan, applies it, and asserts that the
plan is empty afterwards and the data survived.  No mocks: each ledger is a
real Dolt repository under ``tmp_path``.
"""

from __future__ import annotations

import copy
import json
import sys
import warnings
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from tetramark import __version__
from tetramark.cli.app import app
from tetramark.core.ledger import Ledger, build_id_strategies
from tetramark.errors import EXIT_ERROR, LedgerError, UsageError
from tetramark.schema.ddl import view_statements
from tetramark.schema.diff import (
    ADDITIVE,
    DESTRUCTIVE,
    RenameMap,
    SchemaPlan,
    apply,
    plan,
)
from tetramark.schema.loader import load_ledger_schema

from conftest import PRESETS


@pytest.fixture
def ledger_factory(server_ledger_factory):
    """Schema plan/apply needs only the ``Ledger``/``Driver`` interfaces."""
    return server_ledger_factory


#: Baseline preset used by most tests.  It has one entity kind with every
#: interesting column shape, and one stock kind without aliquot fields.
BASE_KINDS: dict[str, Any] = {
    "sample": {
        "object": "entity",
        "fields": {
            "label": {"type": "string", "required": True, "unique": True},
            "color": {"type": "enum", "values": ["red", "green"]},
            "seq": {"type": "sequence", "unique": True},
            "code": {"type": "string", "length": 12},
            "display": {
                "type": "computed",
                "expr": "CONCAT(code, '-', color)",
                "sql_type": "string",
            },
        },
    },
    "bacterial": {
        "object": "stock",
        "entity_kinds": ["sample"],
        "fields": {"strain": {"type": "string"}},
    },
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class PresetWriter:
    """Rewrite the preset of a live ledger between plans.

    ``tm init`` copies the preset into ``<ledger>/presets/`` and the ledger only
    ever reads that copy (ticket 20), so once attached the writer updates the
    copy as well; the source directory is kept to prove it is what init read.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.path = directory / "preset.yaml"
        self.ledger: Ledger | None = None
        directory.mkdir(parents=True, exist_ok=True)

    def attach(self, ledger: Ledger) -> None:
        self.ledger = ledger

    def write(self, kinds: dict[str, Any]) -> None:
        payload = {"preset": "evolving", "version": 1, "kinds": kinds}
        text = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
        self.path.write_text(text, encoding="utf-8")
        if self.ledger is not None:
            copy = self.ledger.path / "presets" / "evolving" / "preset.yaml"
            copy.write_text(text, encoding="utf-8")


@pytest.fixture
def kinds() -> dict[str, Any]:
    return copy.deepcopy(BASE_KINDS)


@pytest.fixture
def preset(tmp_path: Path, kinds: dict[str, Any]) -> PresetWriter:
    writer = PresetWriter(tmp_path / "evolving")
    writer.write(kinds)
    return writer


@pytest.fixture
def ledger(ledger_factory, preset: PresetWriter) -> Ledger:
    ledger = ledger_factory([preset.directory], name="evolving")
    preset.attach(ledger)
    return ledger


def reload_schema(ledger: Ledger) -> None:
    """Re-read ledger.yaml + the preset, as a fresh ``open`` would."""
    config, schema = load_ledger_schema(ledger.path)
    ledger.config = config
    ledger.schema = schema
    ledger.ids = build_id_strategies(config)
    ledger.read_meta()


def insert_entity(
    ledger: Ledger, kind: str, entity_id: str, **fields: Any
) -> None:
    ledger.driver.execute(
        "INSERT INTO `entity` (`id`, `kind`) VALUES (%s, %s)",
        (entity_id, kind),
    )
    table = f"entity_{kind}"
    if not fields:
        ledger.driver.execute(f"INSERT INTO `{table}` (`id`) VALUES (%s)", (entity_id,))
        return
    columns = ", ".join(f"`{name}`" for name in fields)
    placeholders = ", ".join(["%s"] * len(fields))
    ledger.driver.execute(
        f"INSERT INTO `{table}` (`id`, {columns}) VALUES (%s, {placeholders})",
        (entity_id, *fields.values()),
    )


def column_names(ledger: Ledger, table: str) -> set[str]:
    rows = ledger.driver.query(
        "SELECT column_name AS name FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND table_name = %s",
        (table,),
    )
    return {str(row["name"]) for row in rows}


def table_names(ledger: Ledger) -> set[str]:
    rows = ledger.driver.query(
        "SELECT table_name AS name FROM information_schema.tables "
        "WHERE table_schema = DATABASE()"
    )
    return {str(row["name"]) for row in rows}


def commit_count(ledger: Ledger) -> int:
    return int(ledger.driver.query("SELECT COUNT(*) AS n FROM dolt_log")[0]["n"])


def latest_commit(ledger: Ledger) -> dict[str, Any]:
    return ledger.driver.query(
        "SELECT message, author, author_email FROM dolt_log "
        "ORDER BY date DESC, commit_order DESC LIMIT 1"
    )[0]


def actions(current: SchemaPlan) -> list[tuple[str, str, str]]:
    return [(step.tag, step.action, step.name) for step in current.steps]


def action_steps(
    current: SchemaPlan, action: str, *, tag: str | None = None
) -> list[Any]:
    return [
        step
        for step in current.steps
        if step.action == action and (tag is None or step.tag == tag)
    ]


def assert_clean(ledger: Ledger) -> None:
    current = plan(ledger)
    assert current.is_empty, actions(current)
    assert not current.meta_stale


def apply_clean(ledger: Ledger, *, allow_destructive: bool = False) -> None:
    current = plan(ledger)
    result = apply(ledger, allow_destructive=allow_destructive, schema_plan=current)
    assert result.applied is True
    assert result.commit_hash
    assert_clean(ledger)


def insert_stock(
    ledger: Ledger, kind: str, stock_id: str, entity_id: str, **fields: Any
) -> None:
    ledger.driver.execute(
        "INSERT INTO `stock` (`id`, `kind`, `entity_id`) VALUES (%s, %s, %s)",
        (stock_id, kind, entity_id),
    )
    table = f"stock_{kind}"
    if fields:
        columns = ", ".join(f"`{name}`" for name in fields)
        placeholders = ", ".join(["%s"] * len(fields))
        ledger.driver.execute(
            f"INSERT INTO `{table}` (`id`, {columns}) VALUES (%s, {placeholders})",
            (stock_id, *fields.values()),
        )
    else:
        ledger.driver.execute(f"INSERT INTO `{table}` (`id`) VALUES (%s)", (stock_id,))


# ---------------------------------------------------------------------------
# Baseline
# ---------------------------------------------------------------------------


def test_plan_is_empty_for_a_fresh_ledger(ledger_factory) -> None:
    ledger = ledger_factory([PRESETS / "full"], name="fresh")
    current = plan(ledger)
    assert current.is_empty
    assert not current.meta_stale
    assert current.sql == ""
    assert current.to_dict()["summary"] == {
        "total": 0,
        "additive": 0,
        "destructive": 0,
    }


def test_plan_reads_ledger_meta_fingerprint(ledger: Ledger) -> None:
    current = plan(ledger)
    assert current.live_fingerprint == ledger.schema.fingerprint()
    assert current.expected_fingerprint == ledger.fingerprint
    ledger.driver.execute(
        "UPDATE `ledger_meta` SET `value` = 'stale' WHERE `key` = 'fingerprint'"
    )
    ledger.read_meta()
    stale = plan(ledger)
    assert stale.is_empty
    assert stale.meta_stale


def test_plan_is_stale_when_tetramark_version_is_missing(ledger: Ledger) -> None:
    ledger.driver.execute(
        "DELETE FROM `ledger_meta` WHERE `key` = 'tetramark_version'"
    )
    ledger.read_meta()
    current = plan(ledger)
    assert current.is_empty
    assert current.meta_stale
    assert current.to_dict()["fingerprint"]["stale"] is True


# ---------------------------------------------------------------------------
# Format upgrade: spec v1 views -> v2 (alias columns)
# ---------------------------------------------------------------------------


def v1_view_sql(statement_sql: str) -> str:
    """The v1 definition of a generated view: v2 minus its last alias column.

    The alias column is the final select item of every generated view (spec
    §3), so dropping it reproduces the v1 shape without keeping a second copy
    of the view DDL in the test.
    """
    select, separator, tail = statement_sql.rpartition("\nFROM ")
    assert separator, statement_sql
    last_item = select.rstrip()
    expected = ("`alias_list`", "`occupant_alias_list`")
    assert last_item.endswith(expected), last_item[-80:]
    comma = last_item.rfind(",\n")
    assert comma > 0
    return last_item[:comma] + separator + tail


def downgrade_views_to_v1(ledger: Ledger) -> list[str]:
    """Rewrite every generated view in the pre-TM-07 (spec v1) shape."""
    names: list[str] = []
    for statement in view_statements(ledger.schema):
        ledger.driver.execute(f"DROP VIEW `{statement.name}`")
        ledger.driver.execute(v1_view_sql(statement.sql))
        names.append(statement.name)
    return names


def test_upgrade_from_spec_v1_adds_alias_columns_and_refreshes_meta(
    ledger: Ledger,
) -> None:
    """An existing v1 ledger upgrades additively; no destructive step, exit 0."""
    names = downgrade_views_to_v1(ledger)
    assert "alias_list" not in column_names(ledger, "v_entity_sample")
    assert "occupant_alias_list" not in column_names(ledger, "v_box_slots")

    # A v1 ledger records format version 1 and a fingerprint that no longer
    # matches the tool's schema.
    ledger.driver.execute(
        "UPDATE `ledger_meta` SET `value` = '1' WHERE `key` = 'spec_version'"
    )
    ledger.driver.execute(
        "UPDATE `ledger_meta` SET `value` = '0.0.9' "
        "WHERE `key` = 'tetramark_version'"
    )
    ledger.driver.execute(
        "UPDATE `ledger_meta` SET `value` = 'v1-fingerprint' "
        "WHERE `key` = 'fingerprint'"
    )
    ledger.read_meta()

    current = plan(ledger)
    assert current.destructive == []
    assert current.meta_stale
    assert current.pending
    actions_seen = {(step.action, step.name) for step in current.additive}
    for name in names:
        assert ("drop_view", name) in actions_seen
        assert ("create_view", name) in actions_seen

    # ``tm schema plan`` exits 0 and announces the additive view replacement
    # plus the meta refresh.
    runner = CliRunner()
    base = [
        "--workspace",
        str(ledger.workspace.data_dir),
        "--ledger",
        ledger.name,
    ]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        planned = runner.invoke(app, [*base, "schema", "plan"])
        applied = runner.invoke(app, [*base, "schema", "apply"])
    assert planned.exit_code == 0, planned.output
    assert "drop_view v_entity_sample" in planned.output
    assert "0 destructive" in planned.output
    assert "meta refresh" in planned.output

    assert applied.exit_code == 0, applied.output
    assert "Applied" in applied.output

    ledger.read_meta()
    assert ledger.meta["spec_version"] == "2"
    assert ledger.meta["tetramark_version"] == __version__
    for name, column in (
        ("v_entity_sample", "alias_list"),
        ("v_stock_bacterial", "alias_list"),
        ("v_stock_status", "alias_list"),
        ("v_aliquot_location", "alias_list"),
        ("v_box_slots", "occupant_alias_list"),
    ):
        assert column in column_names(ledger, name), name
    assert_clean(ledger)


def test_apply_refreshes_tetramark_version_but_keeps_created_with(
    ledger: Ledger,
) -> None:
    # Simulate a ledger written by an older release.
    ledger.driver.execute(
        "UPDATE `ledger_meta` SET `value` = '0.0.9' "
        "WHERE `key` = 'tetramark_version'"
    )
    ledger.driver.execute(
        "UPDATE `ledger_meta` SET `value` = '0.0.1' WHERE `key` = 'created_with'"
    )
    ledger.read_meta()

    current = plan(ledger)
    assert current.is_empty
    assert current.meta_stale

    result = apply(ledger, schema_plan=current)
    assert result.applied is True
    ledger.read_meta()
    assert ledger.meta["tetramark_version"] == __version__
    # Provenance written at init is frozen: apply never rewrites it.
    assert ledger.meta["created_with"] == "0.0.1"


def test_plan_is_empty_with_a_custom_status_vocabulary(ledger_factory) -> None:
    ledger = ledger_factory(
        [PRESETS / "full"],
        statuses=["pending", "stored", "quarantined"],
    )
    assert plan(ledger).is_empty


# ---------------------------------------------------------------------------
# Additive changes
# ---------------------------------------------------------------------------


def test_new_kind_is_additive_and_keeps_other_kinds(
    ledger: Ledger, kinds: dict[str, Any], preset: PresetWriter
) -> None:
    insert_entity(ledger, "sample", "E1", label="kept", seq="ACGT", code="ABC")
    kinds["plasmid"] = {
        "object": "entity",
        "fields": {"name": {"type": "string", "required": True}},
    }
    preset.write(kinds)
    reload_schema(ledger)

    current = plan(ledger)
    assert not current.destructive
    assert ("additive", "create_table", "entity_plasmid") in actions(current)
    assert ("additive", "create_view", "v_entity_plasmid") in actions(current)

    apply_clean(ledger)
    assert "entity_plasmid" in table_names(ledger)
    assert ledger.driver.query("SELECT label FROM entity_sample") == [
        {"label": "kept"}
    ]


def test_new_nullable_and_defaulted_fields_are_additive(
    ledger: Ledger, kinds: dict[str, Any], preset: PresetWriter
) -> None:
    insert_entity(ledger, "sample", "E1", label="kept", seq="ACGT")
    fields = kinds["sample"]["fields"]
    fields["note"] = {"type": "string"}
    fields["count"] = {"type": "int", "default": 3}
    fields["active"] = {"type": "bool", "default": True}
    fields["born"] = {"type": "date", "default": "2020-02-03"}
    fields["ratio"] = {"type": "decimal", "precision": 8, "scale": 3, "default": 1.5}
    preset.write(kinds)
    reload_schema(ledger)

    current = plan(ledger)
    assert not current.destructive
    additions = action_steps(current, "add_column")
    assert {step.name for step in additions} == {
        "entity_sample.note",
        "entity_sample.count",
        "entity_sample.active",
        "entity_sample.born",
        "entity_sample.ratio",
    }
    assert all(step.tag == ADDITIVE for step in additions)

    apply_clean(ledger)
    row = ledger.driver.query(
        "SELECT note, count, active, born, ratio FROM entity_sample WHERE id = 'E1'"
    )[0]
    assert row["note"] is None
    assert row["count"] == 3
    assert row["active"] in (1, True)
    assert str(row["born"]).startswith("2020-02-03")
    assert Decimal(str(row["ratio"])) == Decimal("1.500")


def test_new_enum_value_is_additive(
    ledger: Ledger, kinds: dict[str, Any], preset: PresetWriter
) -> None:
    insert_entity(ledger, "sample", "E1", label="kept", color="red", seq="ACGT")
    kinds["sample"]["fields"]["color"] = {
        "type": "enum",
        "values": ["red", "green", "blue"],
    }
    preset.write(kinds)
    reload_schema(ledger)

    current = plan(ledger)
    assert not current.destructive
    assert action_steps(current, "drop_constraint")
    assert action_steps(current, "add_constraint")

    apply_clean(ledger)
    insert_entity(ledger, "sample", "E2", label="two", color="blue", seq="TGCA")
    assert ledger.driver.query("SELECT color FROM entity_sample WHERE id = 'E2'") == [
        {"color": "blue"}
    ]


def test_relax_not_null_is_additive(
    ledger: Ledger, kinds: dict[str, Any], preset: PresetWriter
) -> None:
    insert_entity(ledger, "sample", "E1", label="kept", seq="ACGT")
    kinds["sample"]["fields"]["label"] = {"type": "string", "unique": True}
    preset.write(kinds)
    reload_schema(ledger)

    current = plan(ledger)
    assert not current.destructive
    modifies = action_steps(current, "modify_column", tag=ADDITIVE)
    assert [step.name for step in modifies] == ["entity_sample.label"]

    apply_clean(ledger)
    assert ledger.driver.query("SELECT label FROM entity_sample") == [{"label": "kept"}]


def test_field_rename_is_additive_and_preserves_values_and_unique(
    ledger: Ledger, kinds: dict[str, Any], preset: PresetWriter
) -> None:
    insert_entity(ledger, "sample", "E1", label="kept", seq="ACGT")
    fields = kinds["sample"]["fields"]
    fields["title"] = {**fields.pop("label"), "renamed_from": "label"}
    preset.write(kinds)
    reload_schema(ledger)

    current = plan(ledger)
    assert not current.destructive
    assert ("additive", "rename_column", "entity_sample.title") in actions(current)
    # The view is regenerated from the new column list.
    assert ("additive", "drop_view", "v_entity_sample") in actions(current)

    apply_clean(ledger)
    assert ledger.driver.query("SELECT title FROM entity_sample") == [
        {"title": "kept"}
    ]
    assert "label" not in column_names(ledger, "entity_sample")
    view_columns = column_names(ledger, "v_entity_sample")
    assert "title" in view_columns

    # The UNIQUE constraint followed the renamed column and still bites.
    with pytest.raises(LedgerError, match="(?i)duplicate"):
        ledger.driver.execute(
            "INSERT INTO `entity` (`id`, `kind`) VALUES ('E2', 'sample')"
        )
        ledger.driver.execute(
            "INSERT INTO `entity_sample` (`id`, `title`, `seq`) "
            "VALUES ('E2', 'kept', 'TGCA')"
        )


def test_sequence_field_rename_preserves_value_and_hash(
    ledger: Ledger, kinds: dict[str, Any], preset: PresetWriter
) -> None:
    insert_entity(ledger, "sample", "E1", label="kept", seq="ACGT")
    before = ledger.driver.query(
        "SELECT seq_sha256 FROM entity_sample WHERE id = 'E1'"
    )[0]["seq_sha256"]

    fields = kinds["sample"]["fields"]
    fields["dna"] = {**fields.pop("seq"), "renamed_from": "seq"}
    preset.write(kinds)
    reload_schema(ledger)

    current = plan(ledger)
    assert not current.destructive
    renamed = {step.name for step in action_steps(current, "rename_column")}
    assert {"entity_sample.dna", "entity_sample.dna_sha256"} <= renamed

    apply_clean(ledger)
    row = ledger.driver.query(
        "SELECT dna, dna_sha256 FROM entity_sample WHERE id = 'E1'"
    )[0]
    assert row["dna"] == "ACGT"
    assert row["dna_sha256"] == before


def test_kind_rename_is_additive_and_preserves_rows(
    ledger: Ledger, kinds: dict[str, Any], preset: PresetWriter
) -> None:
    insert_entity(ledger, "sample", "E1", label="kept", seq="ACGT")
    specimen = {**kinds.pop("sample"), "renamed_from": "sample"}
    kinds["specimen"] = specimen
    kinds["bacterial"] = {**kinds["bacterial"], "entity_kinds": ["specimen"]}
    preset.write(kinds)
    reload_schema(ledger)

    current = plan(ledger)
    assert not current.destructive
    assert ("additive", "rename_table", "entity_specimen") in actions(current)
    assert ("additive", "update_data", "entity_specimen") in actions(current)

    apply_clean(ledger)
    assert ledger.driver.query("SELECT id, kind FROM entity") == [
        {"id": "E1", "kind": "specimen"}
    ]
    assert ledger.driver.query("SELECT label FROM entity_specimen") == [
        {"label": "kept"}
    ]
    assert "entity_sample" not in table_names(ledger)
    assert "v_entity_sample" not in table_names(ledger)
    assert "v_entity_specimen" in table_names(ledger)

    insert_entity(ledger, "specimen", "E2", label="new", seq="TGCA")
    assert ledger.driver.query("SELECT kind FROM entity WHERE id = 'E2'") == [
        {"kind": "specimen"}
    ]


def test_stock_kind_rename_moves_the_extension_table(
    ledger: Ledger, kinds: dict[str, Any], preset: PresetWriter
) -> None:
    kinds["bacterial"]["aliquot_fields"] = {
        "volume_ul": {"type": "decimal", "precision": 10, "scale": 1}
    }
    preset.write(kinds)
    reload_schema(ledger)
    apply_clean(ledger)

    insert_entity(ledger, "sample", "E1", label="kept", seq="ACGT")
    insert_stock(ledger, "bacterial", "S1", "E1", strain="MG1655")
    ledger.driver.execute(
        "INSERT INTO `aliquot` (`id`, `stock_id`, `seq`, `status`) "
        "VALUES ('S1.1', 'S1', 1, 'stored')"
    )
    ledger.driver.execute(
        "INSERT INTO `aliquot_bacterial` (`id`, `volume_ul`) VALUES ('S1.1', 7.5)"
    )

    culture = {
        **kinds.pop("bacterial"),
        "renamed_from": "bacterial",
    }
    kinds["culture"] = culture
    preset.write(kinds)
    reload_schema(ledger)

    current = plan(ledger)
    assert not current.destructive
    assert ("additive", "rename_table", "stock_culture") in actions(current)
    assert ("additive", "rename_table", "aliquot_culture") in actions(current)

    apply_clean(ledger)
    assert ledger.driver.query("SELECT id, kind FROM stock") == [
        {"id": "S1", "kind": "culture"}
    ]
    assert "stock_bacterial" not in table_names(ledger)
    assert "aliquot_bacterial" not in table_names(ledger)
    row = ledger.driver.query(
        "SELECT volume_ul FROM aliquot_culture WHERE id = 'S1.1'"
    )[0]
    assert Decimal(str(row["volume_ul"])) == Decimal("7.5")


# ---------------------------------------------------------------------------
# Explicit rename map (migration steps, ticket 21): strict old-name checks
# ---------------------------------------------------------------------------


def rename_map_kinds(
    kinds: dict[str, Any], *, label_to_title: bool
) -> dict[str, Any]:
    """``sample`` -> ``specimen`` (optionally ``label`` -> ``title``)."""
    specimen = {**kinds.pop("sample")}
    fields = specimen["fields"]
    if label_to_title:
        fields["title"] = fields.pop("label")
    kinds["specimen"] = specimen
    kinds["bacterial"] = {**kinds["bacterial"], "entity_kinds": ["specimen"]}
    return kinds


def test_rename_map_applies_and_a_rerun_skips_cleanly(
    ledger: Ledger, kinds: dict[str, Any], preset: PresetWriter
) -> None:
    """The strict checks must not break a valid map, nor its idempotent re-run."""
    insert_entity(ledger, "sample", "E1", label="kept", seq="ACGT")
    preset.write(rename_map_kinds(kinds, label_to_title=True))
    reload_schema(ledger)

    renames = RenameMap.from_dict(
        {
            "entity": {"sample": "specimen"},
            "fields": {"entity.specimen": {"label": "title"}},
        }
    )
    current = plan(ledger, renames=renames)
    assert not current.destructive
    assert ("additive", "rename_table", "entity_specimen") in actions(current)
    assert ("additive", "rename_column", "entity_specimen.title") in actions(current)

    result = apply(ledger, schema_plan=current)
    assert result.applied is True
    assert_clean(ledger)
    assert ledger.driver.query("SELECT id, kind FROM entity") == [
        {"id": "E1", "kind": "specimen"}
    ]
    assert ledger.driver.query("SELECT title FROM entity_specimen") == [
        {"title": "kept"}
    ]

    # A second run (the old names are gone, the targets exist) plans nothing
    # and raises nothing: the entry is satisfied, not broken.
    rerun = plan(ledger, renames=renames)
    assert rerun.is_empty, actions(rerun)
    assert not rerun.meta_stale


def test_rename_map_typo_in_old_kind_raises(
    ledger: Ledger, kinds: dict[str, Any], preset: PresetWriter
) -> None:
    """A kind the map names must exist in the ledger, or nothing is renamed."""
    insert_entity(ledger, "sample", "E1", label="kept")
    preset.write(rename_map_kinds(kinds, label_to_title=False))
    reload_schema(ledger)
    before = commit_count(ledger)

    with pytest.raises(UsageError) as excinfo:
        plan(ledger, renames=RenameMap.from_dict({"entity": {"sampl": "specimen"}}))
    message = str(excinfo.value)
    assert "renames.entity" in message
    assert "'sampl'" in message and "'specimen'" in message
    assert "expected the old table 'entity_sampl'" in message
    assert "entity_sample" in message  # the live-tables signature
    assert commit_count(ledger) == before  # a failed plan writes nothing


def test_rename_map_typo_in_old_column_raises(
    ledger: Ledger, kinds: dict[str, Any], preset: PresetWriter
) -> None:
    """A column the map names must exist, or the entry matches nothing.

    The kind table stays live under its own name here, so the failure is the
    column entry, not a missing table.
    """
    insert_entity(ledger, "sample", "E1", label="kept")
    fields = kinds["sample"]["fields"]
    fields["title"] = fields.pop("label")
    preset.write(kinds)
    reload_schema(ledger)

    with pytest.raises(UsageError) as excinfo:
        plan(
            ledger,
            renames=RenameMap.from_dict(
                {"fields": {"entity.sample": {"lable": "title"}}}
            ),
        )
    message = str(excinfo.value)
    assert "renames.fields['entity.sample']" in message
    assert "'lable'" in message and "'title'" in message
    assert "the old column 'lable'" in message
    assert "label" in message  # the live-columns signature


def test_rename_map_field_scope_on_a_missing_table_raises(
    ledger: Ledger, kinds: dict[str, Any], preset: PresetWriter
) -> None:
    """A fields entry for a table the schema is about to create is a no-op."""
    insert_entity(ledger, "sample", "E1", label="kept")
    extra = {**copy.deepcopy(kinds["sample"])}
    extra["fields"] = {"note": {"type": "string"}}
    kinds["new_kind"] = extra
    preset.write(kinds)
    reload_schema(ledger)

    with pytest.raises(UsageError) as excinfo:
        plan(
            ledger,
            renames=RenameMap.from_dict(
                {"fields": {"entity.new_kind": {"label": "note"}}}
            ),
        )
    message = str(excinfo.value)
    assert "renames.fields['entity.new_kind']" in message
    assert "entity_new_kind" in message
    assert "does not exist in the ledger" in message


def test_rename_map_moves_the_aliquot_extension_table(
    ledger: Ledger, kinds: dict[str, Any], preset: PresetWriter
) -> None:
    """A mapped stock kind rename reuses the ``aliquot_<kind>`` rename."""
    kinds["bacterial"]["aliquot_fields"] = {
        "volume_ul": {"type": "decimal", "precision": 10, "scale": 1}
    }
    preset.write(kinds)
    reload_schema(ledger)
    apply_clean(ledger)

    insert_entity(ledger, "sample", "E1", label="kept")
    insert_stock(ledger, "bacterial", "S1", "E1", strain="MG1655")
    ledger.driver.execute(
        "INSERT INTO `aliquot` (`id`, `stock_id`, `seq`, `status`) "
        "VALUES ('S1.1', 'S1', 1, 'stored')"
    )
    ledger.driver.execute(
        "INSERT INTO `aliquot_bacterial` (`id`, `volume_ul`) VALUES ('S1.1', 7.5)"
    )

    kinds["culture"] = {**kinds.pop("bacterial")}
    preset.write(kinds)
    reload_schema(ledger)

    current = plan(ledger, renames=RenameMap.from_dict({"stock": {"bacterial": "culture"}}))
    assert not current.destructive
    assert ("additive", "rename_table", "stock_culture") in actions(current)
    assert ("additive", "rename_table", "aliquot_culture") in actions(current)

    result = apply(ledger, schema_plan=current)
    assert result.applied is True
    assert_clean(ledger)
    assert "stock_bacterial" not in table_names(ledger)
    assert "aliquot_bacterial" not in table_names(ledger)
    assert ledger.driver.query("SELECT kind FROM stock") == [{"kind": "culture"}]
    volume = ledger.driver.query(
        "SELECT volume_ul FROM aliquot_culture WHERE id = 'S1.1'"
    )[0]["volume_ul"]
    assert Decimal(str(volume)) == Decimal("7.5")


def test_rename_map_missing_old_extension_raises(
    ledger: Ledger, kinds: dict[str, Any], preset: PresetWriter
) -> None:
    """The extension half of a stock kind rename obeys the same strict rule."""
    # The live ``bacterial`` kind has no aliquot_fields, so there is no
    # ``aliquot_bacterial`` table to rename even though the new kind adds one.
    culture = {**kinds.pop("bacterial")}
    culture["aliquot_fields"] = {
        "volume_ul": {"type": "decimal", "precision": 10, "scale": 1}
    }
    kinds["culture"] = culture
    preset.write(kinds)
    reload_schema(ledger)

    with pytest.raises(UsageError) as excinfo:
        plan(ledger, renames=RenameMap.from_dict({"stock": {"bacterial": "culture"}}))
    message = str(excinfo.value)
    assert "renames.stock" in message
    assert "expected the old extension table 'aliquot_bacterial'" in message
    assert "separate step" in message
    assert "aliquot_event" in message  # the live aliquot-tables signature


def test_rename_map_skips_a_target_that_already_exists(
    ledger: Ledger, kinds: dict[str, Any], preset: PresetWriter
) -> None:
    """A half-applied rename (target table present) must plan, not fail."""
    insert_entity(ledger, "sample", "E1", label="kept", seq="ACGT")
    preset.write(rename_map_kinds(kinds, label_to_title=True))
    reload_schema(ledger)

    # Simulate a step that renamed the kind table and then failed before the
    # column rename.
    ledger.driver.execute("ALTER TABLE `entity_sample` RENAME TO `entity_specimen`")

    renames = RenameMap.from_dict(
        {
            "entity": {"sample": "specimen"},
            "fields": {"entity.specimen": {"label": "title"}},
        }
    )
    current = plan(ledger, renames=renames)
    assert ("additive", "rename_table", "entity_specimen") not in actions(current)
    assert ("additive", "rename_column", "entity_specimen.title") in actions(current)


def test_computed_expression_change_is_a_generated_rebuild(
    ledger: Ledger, kinds: dict[str, Any], preset: PresetWriter
) -> None:
    insert_entity(
        ledger, "sample", "E1", label="kept", code="ABC", color="red", seq="ACGT"
    )
    kinds["sample"]["fields"]["display"] = {
        "type": "computed",
        "expr": "UPPER(CONCAT(code, ':', color))",
        "sql_type": "string",
    }
    preset.write(kinds)
    reload_schema(ledger)

    current = plan(ledger)
    assert not current.destructive
    assert action_steps(current, "drop_generated_column")
    assert action_steps(current, "add_generated_column")

    apply_clean(ledger)
    assert ledger.driver.query("SELECT display FROM entity_sample") == [
        {"display": "ABC:RED"}
    ]


def test_type_change_rebuilds_dependent_generated_columns(
    ledger: Ledger, kinds: dict[str, Any], preset: PresetWriter
) -> None:
    insert_entity(ledger, "sample", "E1", label="kept", code="ABC", color="red", seq="ACGT")
    kinds["sample"]["fields"]["code"] = {"type": "string", "length": 20}
    preset.write(kinds)
    reload_schema(ledger)

    current = plan(ledger)
    assert not current.is_empty
    assert [step.name for step in action_steps(current, "modify_column")] == [
        "entity_sample.code"
    ]
    assert action_steps(current, "modify_column")[0].tag == DESTRUCTIVE
    dropped = {step.name for step in action_steps(current, "drop_generated_column")}
    assert "entity_sample.display" in dropped

    apply_clean(ledger, allow_destructive=True)
    assert ledger.driver.query("SELECT code, display FROM entity_sample") == [
        {"code": "ABC", "display": "ABC-red"}
    ]


def test_new_ref_field_adds_a_foreign_key(
    ledger: Ledger, kinds: dict[str, Any], preset: PresetWriter
) -> None:
    insert_entity(ledger, "sample", "E1", label="kept", seq="ACGT")
    kinds["sample"]["fields"]["parent"] = {"type": "ref", "to": "sample"}
    preset.write(kinds)
    reload_schema(ledger)

    current = plan(ledger)
    assert not current.destructive
    assert action_steps(current, "add_column")
    assert action_steps(current, "add_foreign_key")

    apply_clean(ledger)
    ledger.driver.execute(
        "UPDATE `entity_sample` SET `parent` = 'E1' WHERE `id` = 'E1'"
    )
    with pytest.raises(LedgerError, match="(?i)foreign key"):
        ledger.driver.execute(
            "UPDATE `entity_sample` SET `parent` = 'missing' WHERE `id` = 'E1'"
        )


def test_retargeting_a_ref_rebuilds_the_foreign_key(
    ledger: Ledger, kinds: dict[str, Any], preset: PresetWriter
) -> None:
    kinds["sample"]["fields"]["parent"] = {"type": "ref", "to": "sample"}
    preset.write(kinds)
    reload_schema(ledger)
    apply_clean(ledger)
    insert_entity(ledger, "sample", "E1", label="kept", seq="ACGT")
    insert_stock(ledger, "bacterial", "S1", "E1", strain="MG1655")

    kinds["sample"]["fields"]["parent"] = {"type": "ref", "to": "stock"}
    preset.write(kinds)
    reload_schema(ledger)
    current = plan(ledger)
    assert not current.destructive
    assert action_steps(current, "drop_foreign_key")
    assert any(
        step.name == "entity_sample.fk_entity_sample_parent"
        for step in action_steps(current, "add_foreign_key")
    )

    apply_clean(ledger)
    ledger.driver.execute(
        "UPDATE `entity_sample` SET `parent` = 'S1' WHERE `id` = 'E1'"
    )
    with pytest.raises(LedgerError, match="(?i)foreign key"):
        ledger.driver.execute(
            "UPDATE `entity_sample` SET `parent` = 'missing' WHERE `id` = 'E1'"
        )


def test_new_sequence_field_creates_hash_and_unique(
    ledger: Ledger, kinds: dict[str, Any], preset: PresetWriter
) -> None:
    insert_entity(ledger, "sample", "E1", label="kept", seq="ACGT")
    kinds["sample"]["fields"]["dna"] = {"type": "sequence", "unique": True}
    preset.write(kinds)
    reload_schema(ledger)

    current = plan(ledger)
    assert action_steps(current, "add_column", tag=ADDITIVE)
    assert action_steps(current, "add_generated_column", tag=ADDITIVE)
    assert action_steps(current, "add_constraint", tag=DESTRUCTIVE)

    apply_clean(ledger, allow_destructive=True)
    ledger.driver.execute(
        "UPDATE `entity_sample` SET `dna` = 'TTTT' WHERE `id` = 'E1'"
    )
    row = ledger.driver.query(
        "SELECT dna, dna_sha256 FROM entity_sample WHERE id = 'E1'"
    )[0]
    assert row["dna"] == "TTTT"
    assert len(row["dna_sha256"]) == 64


def test_adding_aliquot_fields_creates_the_extension_table(
    ledger: Ledger, kinds: dict[str, Any], preset: PresetWriter
) -> None:
    insert_entity(ledger, "sample", "E1", label="kept", seq="ACGT")
    insert_stock(ledger, "bacterial", "S1", "E1", strain="MG1655")
    kinds["bacterial"]["aliquot_fields"] = {
        "volume_ul": {"type": "decimal", "precision": 10, "scale": 1}
    }
    preset.write(kinds)
    reload_schema(ledger)

    current = plan(ledger)
    assert not current.destructive
    assert ("additive", "create_table", "aliquot_bacterial") in actions(current)
    assert any(
        step.name == "aliquot_bacterial.fk_aliquot_bacterial_id"
        for step in action_steps(current, "add_foreign_key")
    )

    apply_clean(ledger)
    ledger.driver.execute(
        "INSERT INTO `aliquot` (`id`, `stock_id`, `seq`, `status`) "
        "VALUES ('S1.1', 'S1', 1, 'stored')"
    )
    ledger.driver.execute(
        "INSERT INTO `aliquot_bacterial` (`id`, `volume_ul`) VALUES ('S1.1', 5.5)"
    )
    row = ledger.driver.query(
        "SELECT volume_ul FROM aliquot_bacterial WHERE id = 'S1.1'"
    )[0]
    assert Decimal(str(row["volume_ul"])) == Decimal("5.5")
    assert ledger.driver.query("SELECT strain FROM stock_bacterial") == [
        {"strain": "MG1655"}
    ]


def test_status_vocabulary_addition_is_additive(
    ledger: Ledger, kinds: dict[str, Any], preset: PresetWriter
) -> None:
    config = yaml.safe_load((ledger.path / "ledger.yaml").read_text(encoding="utf-8"))
    config["statuses"] = ["pending", "stored", "missing", "consumed", "quarantined"]
    (ledger.path / "ledger.yaml").write_text(
        yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
    )
    reload_schema(ledger)

    current = plan(ledger)
    assert not current.destructive
    assert action_steps(current, "drop_constraint")
    assert action_steps(current, "add_constraint")

    apply_clean(ledger)
    insert_entity(ledger, "sample", "E1", label="kept", seq="ACGT")
    insert_stock(ledger, "bacterial", "S1", "E1", strain="MG1655")
    ledger.driver.execute(
        "INSERT INTO `aliquot` (`id`, `stock_id`, `seq`, `status`) "
        "VALUES ('S1.1', 'S1', 1, 'quarantined')"
    )
    assert ledger.driver.query("SELECT status FROM aliquot") == [
        {"status": "quarantined"}
    ]


# ---------------------------------------------------------------------------
# Destructive changes
# ---------------------------------------------------------------------------


def test_new_required_field_without_default_is_destructive(
    ledger: Ledger, kinds: dict[str, Any], preset: PresetWriter
) -> None:
    insert_entity(ledger, "sample", "E1", label="kept", seq="ACGT")
    kinds["sample"]["fields"]["must"] = {"type": "string", "required": True}
    preset.write(kinds)
    reload_schema(ledger)

    current = plan(ledger)
    step = action_steps(current, "add_column")[0]
    assert step.tag == DESTRUCTIVE
    with pytest.raises(LedgerError, match="destructive"):
        apply(ledger, schema_plan=current)
    assert "must" not in column_names(ledger, "entity_sample")

    apply_clean(ledger, allow_destructive=True)
    assert "must" in column_names(ledger, "entity_sample")


def test_tighten_not_null_is_destructive(
    ledger: Ledger, kinds: dict[str, Any], preset: PresetWriter
) -> None:
    kinds["sample"]["fields"]["label"] = {"type": "string", "unique": True}
    preset.write(kinds)
    reload_schema(ledger)
    apply_clean(ledger)  # make it nullable first
    insert_entity(ledger, "sample", "E1", label="kept", seq="ACGT")

    kinds["sample"]["fields"]["label"] = {
        "type": "string",
        "required": True,
        "unique": True,
    }
    preset.write(kinds)
    reload_schema(ledger)

    current = plan(ledger)
    modifies = action_steps(current, "modify_column")
    assert [(step.tag, step.name) for step in modifies] == [
        (DESTRUCTIVE, "entity_sample.label")
    ]
    with pytest.raises(LedgerError, match="destructive"):
        apply(ledger, schema_plan=current)


def test_enum_value_removal_is_destructive(
    ledger: Ledger, kinds: dict[str, Any], preset: PresetWriter
) -> None:
    insert_entity(ledger, "sample", "E1", label="kept", color="green", seq="ACGT")
    kinds["sample"]["fields"]["color"] = {
        "type": "enum",
        "values": ["red"],
    }
    preset.write(kinds)
    reload_schema(ledger)

    current = plan(ledger)
    assert current.destructive
    with pytest.raises(LedgerError, match="destructive"):
        apply(ledger, schema_plan=current)


def test_dropping_a_field_is_destructive(
    ledger: Ledger, kinds: dict[str, Any], preset: PresetWriter
) -> None:
    insert_entity(ledger, "sample", "E1", label="kept", code="ABC", seq="ACGT")
    fields = kinds["sample"]["fields"]
    fields.pop("code")
    fields.pop("display")  # the computed column depends on code
    preset.write(kinds)
    reload_schema(ledger)

    current = plan(ledger)
    drops = action_steps(current, "drop_column")
    assert [(step.tag, step.name) for step in drops] == [
        (DESTRUCTIVE, "entity_sample.code")
    ]
    with pytest.raises(LedgerError, match="destructive"):
        apply(ledger, schema_plan=current)

    apply_clean(ledger, allow_destructive=True)
    assert "code" not in column_names(ledger, "entity_sample")
    assert ledger.driver.query("SELECT label FROM entity_sample") == [
        {"label": "kept"}
    ]


def test_dropping_a_kind_is_destructive(
    ledger: Ledger, kinds: dict[str, Any], preset: PresetWriter
) -> None:
    insert_entity(ledger, "sample", "E1", label="kept", seq="ACGT")
    insert_stock(ledger, "bacterial", "S1", "E1", strain="MG1655")
    kinds.pop("bacterial")
    preset.write(kinds)
    reload_schema(ledger)

    current = plan(ledger)
    assert ("destructive", "drop_table", "stock_bacterial") in actions(current)
    with pytest.raises(LedgerError, match="destructive"):
        apply(ledger, schema_plan=current)

    apply_clean(ledger, allow_destructive=True)
    assert "stock_bacterial" not in table_names(ledger)
    assert "v_stock_bacterial" not in table_names(ledger)
    assert ledger.driver.query("SELECT label FROM entity_sample") == [
        {"label": "kept"}
    ]


def test_removing_aliquot_fields_drops_the_extension_table(
    ledger: Ledger, kinds: dict[str, Any], preset: PresetWriter
) -> None:
    kinds["bacterial"]["aliquot_fields"] = {"volume_ul": {"type": "decimal"}}
    preset.write(kinds)
    reload_schema(ledger)
    apply_clean(ledger)
    insert_entity(ledger, "sample", "E1", label="kept", seq="ACGT")
    insert_stock(ledger, "bacterial", "S1", "E1", strain="MG1655")

    kinds["bacterial"]["aliquot_fields"] = {}
    preset.write(kinds)
    reload_schema(ledger)
    current = plan(ledger)
    assert ("destructive", "drop_table", "aliquot_bacterial") in actions(current)
    with pytest.raises(LedgerError, match="destructive"):
        apply(ledger, schema_plan=current)

    apply_clean(ledger, allow_destructive=True)
    assert "aliquot_bacterial" not in table_names(ledger)
    assert ledger.driver.query("SELECT strain FROM stock_bacterial") == [
        {"strain": "MG1655"}
    ]


# ---------------------------------------------------------------------------
# Metadata, commits and idempotency
# ---------------------------------------------------------------------------


def test_apply_makes_one_commit_and_refreshes_ledger_meta(
    ledger: Ledger, kinds: dict[str, Any], preset: PresetWriter
) -> None:
    before = commit_count(ledger)
    kinds["sample"]["fields"]["note"] = {"type": "string"}
    preset.write(kinds)
    reload_schema(ledger)

    current = plan(ledger)
    result = apply(ledger, schema_plan=current)
    assert result.applied and result.commit_hash
    assert commit_count(ledger) == before + 1
    message = latest_commit(ledger)
    assert message["message"] == "apply(schema): 3 step(s)"
    assert message["author_email"] == "factory@tetramark.invalid"

    ledger.read_meta()
    assert ledger.meta["fingerprint"] == ledger.schema.fingerprint()
    assert ledger.check_fingerprint(warn=False) is True

    # Nothing left to do: a second apply does not create a commit.
    again = apply(ledger)
    assert again.applied is False
    assert commit_count(ledger) == before + 1


def test_apply_dry_run_writes_nothing(
    ledger: Ledger, kinds: dict[str, Any], preset: PresetWriter
) -> None:
    before = commit_count(ledger)
    kinds["sample"]["fields"]["note"] = {"type": "string"}
    preset.write(kinds)
    reload_schema(ledger)

    current = plan(ledger)
    result = apply(ledger, schema_plan=current, dry_run=True)
    assert result.applied is False
    assert "note" not in column_names(ledger, "entity_sample")
    assert commit_count(ledger) == before
    assert plan(ledger).is_empty is False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@pytest.fixture
def cli_isolated(
    monkeypatch: pytest.MonkeyPatch, dolt_env: dict[str, str], tmp_path: Path
) -> None:
    for key in (
        "DOLT_ROOT_PATH",
        "HOME",
        "XDG_CONFIG_HOME",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_SYSTEM",
    ):
        monkeypatch.setenv(key, dolt_env[key])
    monkeypatch.delenv("TETRAMARK_WORKSPACE", raising=False)
    monkeypatch.chdir(tmp_path)


def test_cli_schema_plan_text_and_json(
    cli_isolated: None,
    ledger: Ledger,
    kinds: dict[str, Any],
    preset: PresetWriter,
) -> None:
    from typer.testing import CliRunner

    from tetramark.cli.app import app

    kinds["sample"]["fields"]["note"] = {"type": "string"}
    preset.write(kinds)
    runner = CliRunner()
    common = [
        "--workspace",
        str(ledger.workspace.data_dir),
        "--ledger",
        "evolving",
        "schema",
        "plan",
    ]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        text = runner.invoke(app, common)
        as_json = runner.invoke(app, [*common, "--json"])
    assert text.exit_code == 0, text.output
    assert "[additive] add_column entity_sample.note" in text.output
    assert "ALTER TABLE `entity_sample` ADD COLUMN" in text.output
    assert "0 destructive" in text.output

    assert as_json.exit_code == 0, as_json.output
    payload = json.loads(as_json.output)
    assert payload["empty"] is False
    assert payload["fingerprint"]["stale"] is True
    added = next(
        step for step in payload["steps"] if step["action"] == "add_column"
    )
    assert added["tag"] == ADDITIVE
    assert added["sql"].startswith("ALTER TABLE")


def test_cli_schema_plan_reports_meta_refresh_for_missing_version(
    cli_isolated: None, ledger: Ledger
) -> None:
    from typer.testing import CliRunner

    from tetramark.cli.app import app

    ledger.driver.execute(
        "DELETE FROM `ledger_meta` WHERE `key` = 'tetramark_version'"
    )
    runner = CliRunner()
    common = [
        "--workspace",
        str(ledger.workspace.data_dir),
        "--ledger",
        "evolving",
        "schema",
    ]
    # An older ledger without the key still opens; plan reports a refresh, not
    # an error.
    planned = runner.invoke(app, [*common, "plan"])
    assert planned.exit_code == 0, planned.output
    assert "schema is up to date" in planned.output
    assert "meta refresh" in planned.output

    applied = runner.invoke(app, [*common, "apply"])
    assert applied.exit_code == 0, applied.output
    ledger.read_meta()
    assert ledger.meta["tetramark_version"] == __version__


def test_cli_schema_apply_refuses_destructive(
    cli_isolated: None,
    ledger: Ledger,
    kinds: dict[str, Any],
    preset: PresetWriter,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from tetramark.cli.app import main

    kinds["sample"]["fields"]["must"] = {"type": "string", "required": True}
    preset.write(kinds)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "tm",
            "--workspace",
            str(ledger.workspace.data_dir),
            "--ledger",
            "evolving",
            "schema",
            "apply",
        ],
    )
    with pytest.raises(SystemExit) as excinfo:
        main()
    assert excinfo.value.code == EXIT_ERROR
    captured = capsys.readouterr()
    assert "destructive" in captured.err
    assert "destructive" in captured.out
    assert "must" not in column_names(ledger, "entity_sample")


def test_cli_schema_apply_commits_and_plan_becomes_empty(
    cli_isolated: None,
    ledger: Ledger,
    kinds: dict[str, Any],
    preset: PresetWriter,
) -> None:
    from typer.testing import CliRunner

    from tetramark.cli.app import app

    before = commit_count(ledger)
    kinds["sample"]["fields"]["note"] = {"type": "string"}
    preset.write(kinds)
    runner = CliRunner()
    common = [
        "--workspace",
        str(ledger.workspace.data_dir),
        "--ledger",
        "evolving",
    ]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        applied = runner.invoke(app, [*common, "schema", "apply"])
        again = runner.invoke(app, [*common, "schema", "plan", "--json"])
    assert applied.exit_code == 0, applied.output
    assert "Applied 3 step(s)" in applied.output
    assert commit_count(ledger) == before + 1
    assert latest_commit(ledger)["message"].startswith("apply(schema):")

    payload = json.loads(again.output)
    assert payload["empty"] is True
    assert payload["fingerprint"]["stale"] is False


def test_cli_schema_apply_honours_dry_run(
    cli_isolated: None,
    ledger: Ledger,
    kinds: dict[str, Any],
    preset: PresetWriter,
) -> None:
    from typer.testing import CliRunner

    from tetramark.cli.app import app

    kinds["sample"]["fields"]["note"] = {"type": "string"}
    preset.write(kinds)
    before = commit_count(ledger)
    runner = CliRunner()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        result = runner.invoke(
            app,
            [
                "--workspace",
                str(ledger.workspace.data_dir),
                "--ledger",
                "evolving",
                "--dry-run",
                "schema",
                "apply",
            ],
        )
    assert result.exit_code == 0, result.output
    assert "Would apply 3 step(s)" in result.output
    assert "ADD COLUMN" in result.output
    assert commit_count(ledger) == before
    assert "note" not in column_names(ledger, "entity_sample")


def test_cli_schema_apply_allows_destructive_with_the_flag(
    cli_isolated: None,
    ledger: Ledger,
    kinds: dict[str, Any],
    preset: PresetWriter,
) -> None:
    from typer.testing import CliRunner

    from tetramark.cli.app import app

    kinds["sample"]["fields"]["must"] = {"type": "string", "required": True}
    preset.write(kinds)
    runner = CliRunner()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        result = runner.invoke(
            app,
            [
                "--workspace",
                str(ledger.workspace.data_dir),
                "--ledger",
                "evolving",
                "schema",
                "apply",
                "--allow-destructive",
            ],
        )
    assert result.exit_code == 0, result.output
    assert "Applied" in result.output
    assert "must" in column_names(ledger, "entity_sample")
    reload_schema(ledger)
    assert_clean(ledger)
