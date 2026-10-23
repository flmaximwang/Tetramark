"""Loader tests: includes, mixins, local kinds/rules, cross-refs and errors."""

from __future__ import annotations

from pathlib import Path

import pytest

from tetramark.schema.loader import (
    list_bundled_presets,
    load_ledger_config,
    load_ledger_schema,
    load_preset,
    load_schema,
)
from tetramark.schema.model import (
    DEFAULT_STATUSES,
    FieldType,
    LedgerConfig,
    ObjectType,
    SchemaError,
)

DATA = Path(__file__).parent / "data"
PRESETS = DATA / "presets"
LEDGERS = DATA / "ledgers"
INVALID = PRESETS / "invalid"


# ---------------------------------------------------------------------------
# Positive paths
# ---------------------------------------------------------------------------


def test_load_preset_resolves_include_chain() -> None:
    preset = load_preset(PRESETS / "full")
    assert preset.preset == "full"
    assert preset.version == 1
    assert preset.include == []
    # From base, chain and full respectively.
    assert {"item", "grid96"} <= set(preset.kinds)
    assert {"plasmid", "bacterial"} <= set(preset.kinds)
    assert "everything" in preset.kinds
    assert [rule.id for rule in preset.rules] == ["CH01", "CH02", "FULL01"]
    # Mixins are resolved by load_schema, not by load_preset.
    assert "named_fields" in preset.mixins
    assert "sequence" in preset.kinds["plasmid"].fields


def test_load_preset_single_yaml_file_with_search_paths() -> None:
    preset = load_preset(PRESETS / "full" / "preset.yaml", search_paths=[PRESETS])
    assert "item" in preset.kinds and "everything" in preset.kinds


def test_load_schema_resolves_mixins_and_cross_refs() -> None:
    config = LedgerConfig(name="demo", presets=[str(PRESETS / "full")])
    schema = load_schema(config, base_dir=Path.cwd())

    assert set(schema.entity_kinds) == {"item", "plasmid", "everything"}
    assert set(schema.stock_kinds) == {"lot", "bacterial", "prep"}
    assert set(schema.box_kinds) == {
        "grid96",
        "rack",
        "shelf",
        "grid9x9",
        "namedbox",
        "slotlessbox",
    }

    # Mixin fields are inlined, with the kind's own fields appended.
    assert set(schema.kinds["item"].fields) == {"name", "description", "category"}
    assert not schema.kinds["item"].fields["category"].has_default
    assert schema.kinds["lot"].fields["supplier"].default == "unknown"
    assert set(schema.kinds["bacterial"].fields) == {
        "measured_at",
        "amount",
        "strain",
        "medium",
    }
    assert schema.kinds["bacterial"].entity_kinds == ["item", "plasmid"]
    assert set(schema.kinds["bacterial"].aliquot_fields) == {"glycerol_pct"}

    everything = schema.kinds["everything"]
    assert everything.fields["code"].pattern == "^[A-Z]{3}$"
    assert everything.fields["count"].type is FieldType.int
    assert everything.fields["ratio"].decimal_precision == 8
    assert everything.fields["born"].has_default
    assert everything.fields["parent"].type is FieldType.ref
    assert everything.fields["display"].type is FieldType.computed
    assert everything.fields["legacy"].renamed_from == "old_legacy"

    assert schema.kinds["grid9x9"].geometry is not None
    assert len(schema.kinds["grid9x9"].geometry.labels) == 81
    assert schema.kinds["grid9x9"].slot_capacity == 2
    assert schema.kinds["slotlessbox"].geometry is not None
    assert schema.kinds["slotlessbox"].geometry.labels == []

    assert [rule.id for rule in schema.rules] == ["CH01", "CH02", "FULL01"]
    assert [rule.severity.value for rule in schema.rules] == ["fail", "info", "fail"]
    assert schema.statuses == list(DEFAULT_STATUSES)
    assert schema.preset_versions == {"base": 2, "chain": 3, "full": 1}


def test_diamond_includes_are_merged_once() -> None:
    config = LedgerConfig(
        name="demo",
        presets=[str(PRESETS / "chain"), str(PRESETS / "full")],
    )
    schema = load_schema(config, base_dir=Path.cwd())
    assert set(schema.kinds) == set(
        load_schema(
            LedgerConfig(name="demo", presets=[str(PRESETS / "full")]),
            base_dir=Path.cwd(),
        ).kinds
    )


def test_preset_directory_kinds_and_rules_subdirectories() -> None:
    config = LedgerConfig(name="demo", presets=[str(PRESETS / "extras")])
    schema = load_schema(config, base_dir=Path.cwd())
    assert {"extra_box", "local_entity"} <= set(schema.kinds)
    assert [rule.id for rule in schema.rules] == ["EX01"]
    assert schema.kinds["extra_box"].geometry.labels == ["a", "b"]


def test_local_kinds_dir_rules_dir_and_statuses() -> None:
    path = LEDGERS / "local" / "ledger.yaml"
    config = load_ledger_config(path)
    assert config.name == "local"
    assert config.presets == ["../../presets/chain"]
    assert config.kinds_dir == "kinds/"
    assert config.rules_dir == "rules/"
    assert config.statuses == [
        "pending",
        "stored",
        "missing",
        "consumed",
        "quarantined",
    ]
    assert config.id_config(ObjectType.stock).prefix == "77"

    _, schema = load_ledger_schema(LEDGERS / "local")
    assert "custom_entity" in schema.kinds
    assert schema.kinds["custom_entity"].fields["label"].required
    assert [rule.id for rule in schema.rules] == ["CH01", "CH02", "LOCAL01"]
    assert schema.statuses[-1] == "quarantined"


def test_presets_can_be_found_through_search_paths(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger"
    ledger.mkdir()
    (ledger / "ledger.yaml").write_text(
        "name: searched\npresets: [chain]\n", encoding="utf-8"
    )
    config, schema = load_ledger_schema(ledger, search_paths=[PRESETS])
    assert config.name == "searched"
    assert {"plasmid", "item"} <= set(schema.kinds)
    assert schema.preset_versions["chain"] == 3


def test_fingerprint_is_stable_across_reloads() -> None:
    config = LedgerConfig(name="demo", presets=[str(PRESETS / "full")])
    first = load_schema(config, base_dir=Path.cwd()).fingerprint()
    second = load_schema(config, base_dir=Path.cwd()).fingerprint()
    assert first == second
    assert len(first) == 64


def test_fingerprint_ignores_mixin_inlining(tmp_path: Path) -> None:
    inline = tmp_path / "inline"
    inline.mkdir()
    (inline / "preset.yaml").write_text(
        """preset: chain_inline
version: 3
include: [base]
kinds:
  plasmid:
    object: entity
    fields:
      name: {type: string, required: true, unique: true}
      description: {type: text}
      sequence: {type: sequence, unique: true}
      length_bp: {type: computed, expr: "CHAR_LENGTH(sequence)", sql_type: int}
      backbone: {type: enum, values: [puc, pbr, custom], default: puc}
  bacterial:
    object: stock
    entity_kinds: [item, plasmid]
    fields:
      measured_at: {type: datetime}
      amount: {type: decimal, precision: 10, scale: 2, default: 0}
      strain: {type: string, required: true}
      medium: {type: enum, values: [lb, tb, soc]}
    aliquot_fields:
      glycerol_pct: {type: decimal, default: 15}
rules:
  - id: CH01
    description: plasmids should have a sequence
    severity: fail
    sql: "SELECT COUNT(*) FROM entity_plasmid WHERE sequence IS NULL"
    expect: {single_value: 0}
  - id: CH02
    description: informational row count
    severity: info
    sql: "SELECT id FROM entity_plasmid"
    expect: {rows_at_most: 100}
""",
        encoding="utf-8",
    )
    mixin_schema = load_schema(
        LedgerConfig(name="a", presets=[str(PRESETS / "chain")]),
        base_dir=Path.cwd(),
    )
    inline_schema = load_schema(
        LedgerConfig(name="b", presets=[str(inline)]),
        base_dir=Path.cwd(),
        search_paths=[PRESETS],
    )
    assert mixin_schema.fingerprint() == inline_schema.fingerprint()


def test_local_kind_can_use_a_preset_mixin(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger"
    (ledger / "kinds").mkdir(parents=True)
    (ledger / "ledger.yaml").write_text(
        "name: mixed\npresets: [base]\nkinds_dir: kinds/\n", encoding="utf-8"
    )
    (ledger / "kinds" / "extra.yaml").write_text(
        "myitem:\n  object: entity\n  mixins: [named_fields]\n", encoding="utf-8"
    )
    _, schema = load_ledger_schema(ledger, search_paths=[PRESETS])
    assert set(schema.kinds["myitem"].fields) == {"name", "description"}


def test_load_ledger_config_directly() -> None:
    config = load_ledger_config(LEDGERS / "local")
    assert config.author == "Fixture <fixture@example.invalid>"
    assert config.ids["stock"].strategy == "datecode"


def test_list_bundled_presets_returns_a_list() -> None:
    assert isinstance(list_bundled_presets(), list)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

INVALID_CASES = [
    ("missing_include", "not found"),
    ("cycle_a", "include cycle"),
    ("cycle_b", "include cycle"),
    ("bad_kind_name", "identifier"),
    ("reserved_field", "reserved SQL word"),
    ("reserved_id", "generated core column"),
    ("unknown_field_key", "Extra inputs are not permitted"),
    ("unknown_type", "Input should be"),
    ("enum_no_values", "requires a non-empty 'values'"),
    ("enum_bad_default", "not one of the enum values"),
    ("computed_incomplete", "requires 'sql_type'"),
    ("computed_required", "'required'/'unique'"),
    ("decimal_bad_scale", "between 0 and precision"),
    ("ref_no_to", "requires 'to'"),
    ("ref_unknown", "neither a kind nor a core object"),
    ("box_without_geometry", "require a 'geometry'"),
    ("geometry_on_stock", "'geometry' is only valid on box"),
    ("aliquot_on_entity", "'aliquot_fields' is only valid on stock"),
    ("entity_kinds_on_entity", "'entity_kinds' is only valid on stock"),
    ("entity_kinds_unknown", "not an entity kind"),
    ("entity_kinds_not_entity", "not an entity kind"),
    ("slot_capacity_on_entity", "'slot_capacity' is only valid on box"),
    ("bad_range", "descending"),
    ("bad_label_template", "unknown field"),
    ("duplicate_named_slots", "duplicates"),
    ("missing_mixin", "unknown mixin"),
    ("mixin_collision", "more than one mixin"),
    ("mixin_bad_name", "identifier"),
    ("aliquot_reserved_id", "generated core column"),
    ("string_bad_length", "positive integer"),
    ("pattern_invalid", "not a valid regular expression"),
    ("bool_bad_default", "must be true or false"),
    ("rule_duplicate", "duplicate rule id"),
    ("rule_bad_expect", "exactly one"),
    ("rule_not_select", "read-only SELECT"),
    ("broken_yaml", "invalid YAML"),
]


@pytest.mark.parametrize(("case", "message"), INVALID_CASES)
def test_invalid_presets_raise_with_location(case: str, message: str) -> None:
    config = LedgerConfig(name="bad", presets=[str(INVALID / case)])
    with pytest.raises(SchemaError, match=message) as caught:
        load_schema(config, base_dir=Path.cwd())
    error = caught.value
    assert error.file, "errors must carry the YAML file"
    assert "preset.yaml" in error.file or "invalid" in error.file


def test_ref_error_points_at_the_to_key() -> None:
    config = LedgerConfig(name="bad", presets=[str(INVALID / "ref_unknown")])
    with pytest.raises(SchemaError) as caught:
        load_schema(config, base_dir=Path.cwd())
    error = caught.value
    assert error.file is not None and error.file.endswith("preset.yaml")
    assert error.path == "kinds.item.fields.other.to"
    assert "ghost" in str(error)


def test_missing_preset_reports_presets_path() -> None:
    config = LedgerConfig(name="bad", presets=["definitely_not_a_preset"])
    with pytest.raises(SchemaError, match="not found") as caught:
        load_schema(config, base_dir=Path.cwd())
    assert caught.value.path == "presets"


def test_duplicate_kind_across_presets() -> None:
    config = LedgerConfig(
        name="bad",
        presets=[
            str(INVALID / "kind_collision_a"),
            str(INVALID / "kind_collision_b"),
        ],
    )
    with pytest.raises(SchemaError, match="duplicate kind 'dup'") as caught:
        load_schema(config, base_dir=Path.cwd())
    assert caught.value.path == "kinds.dup"


def test_duplicate_mixin_across_presets() -> None:
    config = LedgerConfig(
        name="bad",
        presets=[str(INVALID / "mixin_dup_a"), str(INVALID / "mixin_dup_b")],
    )
    with pytest.raises(SchemaError, match="duplicate mixin 'shared'") as caught:
        load_schema(config, base_dir=Path.cwd())
    assert caught.value.path == "mixins.shared"


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("bad_config_status", "must match"),
        ("bad_config_ids", "unknown id strategy"),
        ("bad_config_extra", "Extra inputs are not permitted"),
    ],
)
def test_invalid_ledger_config(case: str, message: str) -> None:
    with pytest.raises(SchemaError, match=message) as caught:
        load_ledger_config(INVALID / case / "ledger.yaml")
    assert caught.value.file is not None
    assert caught.value.file.endswith("ledger.yaml")


def test_missing_ledger_config(tmp_path: Path) -> None:
    with pytest.raises(SchemaError, match="ledger.yaml not found"):
        load_ledger_config(tmp_path / "missing" / "ledger.yaml")


def test_missing_local_kinds_dir(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger"
    ledger.mkdir()
    (ledger / "ledger.yaml").write_text(
        "name: x\nkinds_dir: absent/\n", encoding="utf-8"
    )
    config = load_ledger_config(ledger / "ledger.yaml")
    with pytest.raises(SchemaError, match="directory not found"):
        load_schema(config, base_dir=ledger)


def test_bad_local_rules_document(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger"
    (ledger / "rules").mkdir(parents=True)
    (ledger / "ledger.yaml").write_text(
        "name: x\nrules_dir: rules/\n", encoding="utf-8"
    )
    (ledger / "rules" / "bad.yaml").write_text("id: no_sql\n", encoding="utf-8")
    config = load_ledger_config(ledger / "ledger.yaml")
    with pytest.raises(SchemaError, match="rules document"):
        load_schema(config, base_dir=ledger)
