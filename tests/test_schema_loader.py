"""Loader tests: includes, mixins, local kinds/rules, cross-refs and errors."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tetramark.schema.loader import (
    list_presets,
    load_ledger_config,
    load_ledger_schema,
    load_preset,
    load_schema,
)
from tetramark.schema.model import (
    FIXED_STATUS_SET,
    FieldType,
    LedgerConfig,
    ObjectType,
    SchemaError,
    StatusModel,
    StatusSet,
)

DATA = Path(__file__).parent / "data"
PRESETS = DATA / "presets"
LEDGERS = DATA / "ledgers"
INVALID = PRESETS / "invalid"
REPO_PRESETS = Path(__file__).resolve().parents[1] / "presets"

#: Presets are read from disk; no database and no ``dolt`` binary, so these
#: loaders run in the fast lane (``-m fast``).
pytestmark = pytest.mark.fast


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


def test_load_preset_repo_yaml_file() -> None:
    preset = load_preset(REPO_PRESETS / "base.yaml")
    assert preset.preset == "base"
    assert "item" in preset.kinds


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
    # The base fixture defines the status sets; the stock kinds name them.
    assert set(schema.status_sets) == {"tube", "cryo"}
    assert schema.statuses_for("lot") == [
        "pending",
        "stored",
        "missing",
        "consumed",
    ]
    assert schema.statuses_for("bacterial") == schema.statuses_for("lot")
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


def test_local_kinds_dir_rules_dir(tmp_path: Path) -> None:
    path = LEDGERS / "local" / "ledger.yaml"
    config = load_ledger_config(path)
    assert config.name == "local"
    assert config.presets == ["../../presets/chain"]
    assert config.kinds_dir == "kinds/"
    assert config.rules_dir == "rules/"
    assert not hasattr(config, "statuses")
    assert config.id_config(ObjectType.stock).prefix == "77"

    ledger = tmp_path / "local"
    shutil.copytree(LEDGERS / "local", ledger)
    (ledger / "presets").mkdir()
    shutil.copytree(PRESETS / "chain", ledger / "presets" / "chain")
    shutil.copytree(PRESETS / "base", ledger / "presets" / "base")
    _, schema = load_ledger_schema(ledger)
    assert "custom_entity" in schema.kinds
    assert schema.kinds["custom_entity"].fields["label"].required
    assert [rule.id for rule in schema.rules] == ["CH01", "CH02", "LOCAL01"]
    assert schema.statuses_for("bacterial") == [
        "pending",
        "stored",
        "missing",
        "consumed",
    ]
    assert isinstance(schema.status_set("bacterial"), StatusSet)


def test_presets_resolve_only_from_the_ledger_copy(tmp_path: Path) -> None:
    """A ledger never falls back to a search path for its presets (spec §8)."""
    ledger = tmp_path / "ledger"
    ledger.mkdir()
    (ledger / "ledger.yaml").write_text(
        "name: searched\npresets: [chain]\n", encoding="utf-8"
    )
    with pytest.raises(SchemaError) as caught:
        load_ledger_schema(ledger, search_paths=[PRESETS])
    assert "presets/chain" in str(caught.value)
    assert caught.value.path == "presets"

    # With the ledger's own copy it loads, and the search path is not needed.
    (ledger / "presets").mkdir()
    shutil.copytree(PRESETS / "chain", ledger / "presets" / "chain")
    shutil.copytree(PRESETS / "base", ledger / "presets" / "base")
    config, schema = load_ledger_schema(ledger, search_paths=[PRESETS])
    assert config.name == "searched"
    assert config.presets == ["chain"]
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
    statuses: tube
    fields:
      measured_at: {type: datetime}
      amount: {type: decimal, precision: 10, scale: 2, default: 0}
      strain: {type: string, required: true}
      medium: {type: enum, values: [lb, tb, soc]}
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
    (ledger / "presets").mkdir()
    shutil.copytree(PRESETS / "base", ledger / "presets" / "base")
    _, schema = load_ledger_schema(ledger)
    assert set(schema.kinds["myitem"].fields) == {"name", "description"}


def test_load_ledger_config_directly() -> None:
    config = load_ledger_config(LEDGERS / "local")
    assert config.author == "Fixture <fixture@example.invalid>"
    assert config.ids["stock"].strategy == "datecode"


def test_list_presets_reads_a_presets_directory(tmp_path: Path) -> None:
    assert list_presets(REPO_PRESETS) == [
        "angenet",
        "bacteria",
        "base",
        "biosamples",
        "c_elegans",
        "cell_culture",
        "cellbio",
        "drosophila",
        "immuno",
        "library",
        "molbio",
        "mouse",
        "nucleic_acids",
        "proteins",
        "viral_vectors",
        "zebrafish",
    ]
    assert list_presets(PRESETS) == ["base", "chain", "extras", "fixed", "full"]
    with pytest.raises(SchemaError, match="presets directory not found"):
        list_presets(tmp_path / "absent")


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
    ("aliquot_on_entity", "migration/spec/2-to-3/README.md"),
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


def test_aliquot_fields_is_rejected_with_the_migration_pointer() -> None:
    """Spec 3 removed the key: an old preset fails to load, with the step named."""
    config = LedgerConfig(
        name="bad", presets=[str(INVALID / "aliquot_on_entity")]
    )
    with pytest.raises(SchemaError) as caught:
        load_schema(config, base_dir=Path.cwd())
    error = caught.value
    message = str(error)
    assert "'aliquot_fields' was removed in Tetramark 0.2.0" in message
    assert "migration/spec/2-to-3/README.md" in message
    # The error names the document and the exact dotted path of the old key.
    assert error.path == "kinds.item.aliquot_fields"
    assert "aliquot_on_entity" in (error.file or "")


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
        ("bad_config_status", "migration/spec/3-to-4"),
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


# ---------------------------------------------------------------------------
# Status sets (spec 4)
# ---------------------------------------------------------------------------


def _write_preset(directory: Path, name: str, text: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_stock_kind_without_statuses_uses_the_fixed_model(tmp_path: Path) -> None:
    """Spec 6: no status declaration anywhere means the fixed status set."""
    path = _write_preset(
        tmp_path,
        "nostatus",
        """preset: nostatus
kinds:
  lot:
    object: stock
""",
    )
    schema = load_schema(LedgerConfig(name="x", presets=[str(path)]))
    assert schema.status_model is StatusModel.fixed
    assert schema.status_sets == {}
    assert schema.kinds["lot"].status_set() is None
    assert schema.statuses_for("lot") == [
        "pending",
        "stored",
        "missing",
        "consumed",
    ]
    assert schema.status_set("lot") is FIXED_STATUS_SET


def test_a_closure_mixing_legacy_sets_and_fixed_kinds_is_a_load_error(
    tmp_path: Path,
) -> None:
    """A ledger must use one status model; the error names the file and key."""
    legacy = _write_preset(
        tmp_path,
        "legacy",
        """preset: legacy
status_sets:
  tube:
    stored: {in_stock: true}
kinds:
  lot:
    object: stock
    statuses: tube
""",
    )
    fixed = _write_preset(
        tmp_path,
        "fixedpart",
        """preset: fixedpart
kinds:
  prep:
    object: stock
""",
    )
    with pytest.raises(SchemaError, match="mix the legacy per-kind status sets") as caught:
        load_schema(
            LedgerConfig(name="x", presets=[str(legacy), str(fixed)]),
            base_dir=tmp_path,
        )
    assert caught.value.path == "kinds.prep.statuses"
    assert caught.value.file == str(fixed)
    assert "status_sets" in caught.value.message
    assert "migration/spec/5-to-6" in caught.value.message


def test_entity_kind_with_statuses_fails_to_load(tmp_path: Path) -> None:
    path = _write_preset(
        tmp_path,
        "entitystatus",
        """preset: entitystatus
status_sets:
  tube:
    stored: {in_stock: true}
kinds:
  thing:
    object: entity
    statuses: tube
""",
    )
    with pytest.raises(SchemaError, match="only valid on stock kinds") as caught:
        load_schema(LedgerConfig(name="x", presets=[str(path)]))
    assert caught.value.path == "kinds.thing"


def test_status_to_target_must_exist_in_the_set(tmp_path: Path) -> None:
    path = _write_preset(
        tmp_path,
        "badto",
        """preset: badto
status_sets:
  tube:
    stored: {in_stock: true, to: [nowhere]}
kinds:
  lot:
    object: stock
    statuses: tube
""",
    )
    with pytest.raises(SchemaError, match="not a status of this set"):
        load_schema(LedgerConfig(name="x", presets=[str(path)]))


def test_duplicate_status_set_name_is_a_load_error(tmp_path: Path) -> None:
    first = _write_preset(
        tmp_path,
        "one",
        """preset: one
status_sets:
  tube:
    stored: {in_stock: true}
kinds:
  a:
    object: entity
""",
    )
    second = _write_preset(
        tmp_path,
        "two",
        """preset: two
status_sets:
  tube:
    stored: {in_stock: true}
kinds:
  b:
    object: entity
""",
    )
    with pytest.raises(SchemaError, match="duplicate status set 'tube'") as caught:
        load_schema(LedgerConfig(name="x", presets=[str(first), str(second)]))
    assert caught.value.path == "status_sets.tube"
    assert "duplicate status set" in caught.value.message


def test_unknown_status_set_name_is_a_load_error(tmp_path: Path) -> None:
    path = _write_preset(
        tmp_path,
        "unknown",
        """preset: unknown
kinds:
  lot:
    object: stock
    statuses: tube
""",
    )
    with pytest.raises(SchemaError, match="unknown status set 'tube'") as caught:
        load_schema(LedgerConfig(name="x", presets=[str(path)]))
    assert caught.value.path == "kinds.lot.statuses"


def test_legacy_ledger_statuses_key_points_at_the_migration() -> None:
    """Spec 4 refuses a ledger.yaml that still holds the spec-3 list."""
    with pytest.raises(SchemaError, match="migration/spec/3-to-4") as caught:
        load_ledger_config(INVALID / "bad_config_status" / "ledger.yaml")
    assert caught.value.path == "statuses"

    # The spec-3 -> 4 step reads the old shape through the explicit opt-in.
    config = load_ledger_config(
        INVALID / "bad_config_status" / "ledger.yaml", allow_legacy_statuses=True
    )
    assert config.name == "bad"
