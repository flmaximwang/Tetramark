"""Unit tests for the schema models (no database, no loader)."""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from tetramark.schema.loader import load_schema
from tetramark.schema.tests_gen import generate_tests
from tetramark.schema.model import (
    BOX_NAME_MAX_LENGTH,
    BOX_VIEW_PREFIX,
    CORE_VIEW_NAMES,
    SPEC_VERSION,
    UNSET,
    EffectiveSchema,
    Field,
    FieldType,
    Geometry,
    IdStrategyConfig,
    Kind,
    LedgerConfig,
    ObjectType,
    Preset,
    Rule,
    RuleExpect,
    SchemaError,
    SqlType,
    Status,
    StatusSet,
    box_name_problem,
    parse_labels,
)

#: No database and no ``dolt`` binary: safe for the fast lane (``-m fast``).
pytestmark = pytest.mark.fast

# ---------------------------------------------------------------------------
# Fields
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "type_"),
    [
        ({"type": "string"}, FieldType.string),
        ({"type": "text"}, FieldType.text),
        ({"type": "int"}, FieldType.int),
        ({"type": "decimal"}, FieldType.decimal),
        ({"type": "bool"}, FieldType.bool),
        ({"type": "date"}, FieldType.date),
        ({"type": "datetime"}, FieldType.datetime),
        ({"type": "enum", "values": ["a", "b"]}, FieldType.enum),
        ({"type": "sequence"}, FieldType.sequence),
        ({"type": "ref", "to": "entity"}, FieldType.ref),
        (
            {"type": "computed", "expr": "CHAR_LENGTH(name)", "sql_type": "int"},
            FieldType.computed,
        ),
    ],
)
def test_every_field_type_validates(payload: dict, type_: FieldType) -> None:
    field = Field(**payload)
    assert field.type is type_
    assert field.normalised()["type"] == type_.value
    assert str(field.type) == type_.value


def test_long_defaults_to_false_and_is_never_normalised() -> None:
    field = Field(type="text")
    assert field.long is False
    assert "long" not in field.normalised()


@pytest.mark.parametrize("type_", ["string", "text", "sequence"])
def test_long_is_valid_on_string_text_and_sequence(type_: str) -> None:
    field = Field(type=type_, long=True)
    assert field.long is True
    # Read-only form metadata: never part of the fingerprint input.
    assert "long" not in field.normalised()


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "int"},
        {"type": "decimal"},
        {"type": "bool"},
        {"type": "date"},
        {"type": "datetime"},
        {"type": "enum", "values": ["a", "b"]},
        {"type": "ref", "to": "entity"},
        {"type": "computed", "expr": "1", "sql_type": "int"},
    ],
)
def test_long_is_rejected_on_other_field_types(payload: dict) -> None:
    with pytest.raises(
        ValidationError, match="'long' is only valid for string, text and sequence"
    ):
        Field(**payload, long=True)


def test_field_modifiers_and_resolved_defaults() -> None:
    field = Field(
        type="string",
        required=True,
        unique=True,
        length=40,
        pattern="^[A-Z]+$",
        description="a code",
        renamed_from="old_code",
    )
    assert field.string_length == 40
    assert field.normalised() == {
        "type": "string",
        "required": True,
        "unique": True,
        "pattern": "^[A-Z]+$",
        "renamed_from": "old_code",
        "length": 40,
    }


def test_string_and_decimal_defaults_are_255_and_18_6() -> None:
    string = Field(type="string")
    assert string.length == 255
    assert string.string_length == 255
    decimal = Field(type="decimal")
    assert (decimal.precision, decimal.scale) == (18, 6)
    assert (decimal.decimal_precision, decimal.decimal_scale) == (18, 6)
    assert decimal.normalised()["precision"] == 18
    assert decimal.normalised()["scale"] == 6


def test_unset_default_differs_from_explicit_null() -> None:
    absent = Field(type="string")
    explicit_null = Field(type="string", default=None)
    assert absent.default is UNSET
    assert not absent.has_default
    assert explicit_null.has_default
    assert "default" not in absent.normalised()
    assert explicit_null.normalised()["default"] is None


def test_enum_values_and_default_are_kept() -> None:
    field = Field(type="enum", values=["amp", "kan"], default="kan")
    assert field.normalised()["values"] == ["amp", "kan"]
    assert field.normalised()["default"] == "kan"


def test_computed_field_normalisation() -> None:
    field = Field(type="computed", expr="CHAR_LENGTH(sequence)", sql_type="int")
    assert field.normalised() == {
        "type": "computed",
        "expr": "CHAR_LENGTH(sequence)",
        "sql_type": SqlType.int.value,
    }


def test_computed_field_can_carry_a_concrete_width() -> None:
    text = Field(type="computed", expr="CONCAT(name, '-', code)", sql_type="string", length=120)
    assert text.normalised()["length"] == 120
    decimal = Field(
        type="computed", expr="a / b", sql_type="decimal", precision=9, scale=3
    )
    assert decimal.normalised()["precision"] == 9
    assert decimal.normalised()["scale"] == 3


def test_date_default_from_yaml_like_object() -> None:
    field = Field(type="date", default=dt.date(2024, 1, 31))
    assert field.normalised()["default"] == "2024-01-31"


def test_date_default_object_and_iso_string_fingerprint_the_same() -> None:
    a = Field(type="date", default=dt.date(2024, 1, 31))
    b = Field(type="date", default="2024-01-31")
    assert a.normalised()["default"] == b.normalised()["default"] == "2024-01-31"


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"type": "enum"}, "non-empty 'values'"),
        ({"type": "enum", "values": []}, "non-empty 'values'"),
        ({"type": "enum", "values": ["a", "a"]}, "duplicates"),
        ({"type": "enum", "values": ["a"], "default": "b"}, "not one of the enum values"),
        ({"type": "ref"}, "requires 'to'"),
        ({"type": "ref", "to": "x", "values": ["a"]}, "only valid for type 'enum'"),
        ({"type": "computed", "expr": "1"}, "requires 'sql_type'"),
        ({"type": "computed", "sql_type": "int"}, "requires a non-empty 'expr'"),
        ({"type": "computed", "expr": "1", "sql_type": "int", "required": True}, "not valid"),
        ({"type": "computed", "expr": "1", "sql_type": "int", "unique": True}, "not valid"),
        ({"type": "computed", "expr": "1", "sql_type": "int", "default": 0}, "not valid"),
        ({"type": "computed", "expr": "1", "sql_type": "int", "length": 4}, "computed string"),
        ({"type": "int", "length": 10}, "only valid for type 'string'"),
        ({"type": "int", "precision": 4}, "only valid for type 'decimal'"),
        ({"type": "string", "length": 0}, "positive integer"),
        ({"type": "decimal", "precision": 4, "scale": 5}, "between 0 and precision"),
        ({"type": "decimal", "precision": 0}, "positive integer"),
        ({"type": "string", "pattern": "["}, "not a valid regular expression"),
        ({"type": "int", "pattern": "\\d+"}, "only valid for string"),
        ({"type": "sequence", "default": "ACGT"}, "not valid for type 'sequence'"),
        ({"type": "int", "default": "x"}, "must be an integer"),
        ({"type": "int", "default": True}, "must be an integer"),
        ({"type": "bool", "default": "yes"}, "must be true or false"),
        ({"type": "string", "default": 3}, "must be a string"),
        ({"type": "date", "default": "not-a-date"}, "ISO date"),
        (
            {"type": "date", "default": dt.datetime(2024, 1, 1, 12, 0)},
            "not a datetime",
        ),
        ({"type": "datetime", "default": dt.date(2024, 1, 1)}, "must include a time"),
        ({"type": "string", "values": ["a"]}, "only valid for type 'enum'"),
        ({"type": "ref", "to": "x", "expr": "1"}, "only valid for type 'computed'"),
    ],
)
def test_field_rejects_invalid_combinations(payload: dict, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        Field(**payload)


def test_field_rejects_unknown_keys() -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        Field(type="string", requireed=True)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Geometry / range parser
# ---------------------------------------------------------------------------


def test_parse_labels_range_with_exclusion() -> None:
    labels = parse_labels("A-J!I")
    assert labels == ["A", "B", "C", "D", "E", "F", "G", "H", "J"]
    assert len(labels) == 9


def test_parse_labels_numeric_range() -> None:
    assert parse_labels("1-12") == [str(n) for n in range(1, 13)]


def test_parse_labels_explicit_list() -> None:
    assert parse_labels(["A", "B", "C"]) == ["A", "B", "C"]
    assert parse_labels(["1", "2", "10"]) == ["1", "2", "10"]
    assert parse_labels([1, 2, 10]) == ["1", "2", "10"]


def test_parse_labels_combination_and_multiple_exclusions() -> None:
    assert parse_labels("A-C,E") == ["A", "B", "C", "E"]
    assert parse_labels("1-6!2,4") == ["1", "3", "5", "6"]


def test_parse_labels_exclusion_must_exist() -> None:
    with pytest.raises(ValueError, match="not present"):
        parse_labels("A-C!Z")


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        ("", "must not be empty"),
        ("J-A", "descending"),
        ("A-3", "mixes letters and digits"),
        ("A-B-C", "invalid range"),
        ("1-0", "descending"),
        (["A", "A"], "duplicate"),
        (["", "A"], "non-empty"),
        ("A-C!", "empty exclusion"),
        ("A-C!xy", "invalid exclusion"),
    ],
)
def test_parse_labels_errors(spec, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_labels(spec)


def test_grid_geometry_materialises_rows_by_cols() -> None:
    geometry = Geometry(type="grid", rows="A-J!I", cols="1-9")
    labels = geometry.labels
    assert len(labels) == 81
    assert len(geometry.row_labels) == 9
    assert geometry.row_labels == ["A", "B", "C", "D", "E", "F", "G", "H", "J"]
    assert geometry.col_labels == [str(n) for n in range(1, 10)]
    assert labels[0] == "A1"
    assert labels[8] == "A9"
    assert labels[-1] == "J9"
    assert labels[9] == "B1"


def test_grid_geometry_custom_label_template() -> None:
    geometry = Geometry(
        type="grid", rows="A-B", cols="1-2", label="{row_index:02}{col}"
    )
    assert geometry.labels == ["011", "012", "021", "022"]


def test_named_geometry_uses_slots() -> None:
    geometry = Geometry(type="named", slots=["top", "middle", "bottom"])
    assert geometry.labels == ["top", "middle", "bottom"]
    assert geometry.is_slotted
    assert Geometry(type="named", slots=[1, 2]).labels == ["1", "2"]


def test_slotless_geometry_has_no_labels() -> None:
    geometry = Geometry(type="slotless", capacity=5)
    assert geometry.labels == []
    assert not geometry.is_slotted
    assert geometry.normalised() == {"type": "slotless", "labels": [], "capacity": 5}


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"type": "grid", "rows": "A-B"}, "requires 'rows' and 'cols'"),
        ({"type": "grid", "rows": "A-B", "cols": "1-2", "capacity": 2}, "only valid for slotless"),
        ({"type": "grid", "rows": "A-B", "cols": "1-2", "label": "{x}"}, "unknown field"),
        (
            {"type": "grid", "rows": "A-B", "cols": "1-2", "label": "{row!r}"},
            "invalid label template",
        ),
        ({"type": "named"}, "requires a non-empty 'slots'"),
        ({"type": "named", "slots": []}, "requires a non-empty 'slots'"),
        ({"type": "named", "slots": ["a", "a"]}, "duplicates"),
        ({"type": "named", "slots": ["a"], "rows": "A"}, "only valid for grid"),
        ({"type": "named", "slots": ["a"], "capacity": 2}, "only valid for slotless"),
        ({"type": "named", "slots": ["a"], "label": "{row}"}, "not allowed"),
        ({"type": "slotless", "rows": "A-B"}, "takes no"),
        ({"type": "slotless", "capacity": 0}, "positive integer"),
        ({"type": "slotless", "label": "{row}"}, "not allowed"),
        ({"type": "blob"}, "Input should be"),
    ],
)
def test_invalid_geometry(payload: dict, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        Geometry(**payload)


def test_grid_label_template_duplicate_detection() -> None:
    with pytest.raises(ValidationError, match="duplicate slot labels"):
        Geometry(type="grid", rows="A-B", cols="1-2", label="same")


# ---------------------------------------------------------------------------
# Kind / rules / preset / config
# ---------------------------------------------------------------------------


def test_kind_resolves_object_specific_rules() -> None:
    kind = Kind(
        object="stock",
        entity_kinds=["thing"],
        statuses=_status_set(),
        fields={"glycerol_pct": Field(type="decimal", default=15)},
    )
    assert kind.object is ObjectType.stock
    assert kind.slot_capacity == 1


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"object": "box"}, "require a 'geometry'"),
        ({"object": "box", "geometry": {"type": "slotless"}, "entity_kinds": ["x"]}, "only valid on stock"),
        ({"object": "stock", "geometry": {"type": "slotless"}}, "only valid on box"),
        ({"object": "entity", "geometry": {"type": "slotless"}}, "only valid on box"),
        ({"object": "entity", "slot_capacity": 2}, "only valid on box"),
        ({"object": "entity", "entity_kinds": ["x"]}, "only valid on stock"),
        ({"object": "box", "geometry": {"type": "grid", "rows": "A", "cols": "1"}, "slot_capacity": 0}, "positive integer"),
        ({"object": "aliquot"}, "Input should be"),
    ],
)
def test_invalid_kind_shapes(payload: dict, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        Kind(**payload)


def test_rule_normalisation() -> None:
    rule = Rule(
        id="MB01",
        description="has sequence",
        sql="SELECT COUNT(*) FROM entity_plasmid",
        expect={"single_value": 0},
    )
    assert rule.severity.value == "fail"
    assert rule.normalised() == {
        "id": "MB01",
        "severity": "fail",
        "sql": "SELECT COUNT(*) FROM entity_plasmid",
        "expect": {"single_value": 0},
    }


@pytest.mark.parametrize(
    "expect",
    [{"rows": 0}, {"rows_at_most": 10}, {"single_value": 3}],
)
def test_rule_expect_kinds(expect: dict) -> None:
    rule = Rule(id="X", description="d", sql="SELECT 1", expect=expect)
    assert rule.expect.kind == next(iter(expect))
    assert rule.expect.value == next(iter(expect.values()))


@pytest.mark.parametrize(
    ("expect", "message"),
    [
        ({}, "exactly one"),
        ({"rows": 0, "single_value": 0}, "exactly one"),
        ({"rows_at_most": -1}, "non-negative"),
    ],
)
def test_rule_expect_errors(expect: dict, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        RuleExpect(**expect)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"id": "1BAD", "description": "d", "sql": "SELECT 1", "expect": {"rows": 0}}, "rule id"),
        ({"id": "X", "description": " ", "sql": "SELECT 1", "expect": {"rows": 0}}, "description"),
        ({"id": "X", "description": "d", "sql": "DELETE FROM x", "expect": {"rows": 0}}, "read-only"),
        ({"id": "X", "description": "d", "sql": "SELECT 1; SELECT 2", "expect": {"rows": 0}}, "single statement"),
        ({"id": "X", "description": "d", "sql": "SELECT 1", "expect": {"rows": 0}, "extra": 1}, "Extra inputs"),
    ],
)
def test_invalid_rules(payload: dict, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        Rule(**payload)


def test_rule_allows_leading_comments() -> None:
    rule = Rule(
        id="X",
        description="d",
        sql="-- explain\nSELECT 1",
        expect={"rows": 0},
    )
    assert rule.id == "X"


def test_preset_defaults_and_validation() -> None:
    preset = Preset(preset="p")
    assert (preset.version, preset.include, preset.kinds, preset.rules) == (1, [], {}, [])
    with pytest.raises(ValidationError, match="positive integer"):
        Preset(version=0)


def test_ledger_config_defaults_and_ids() -> None:
    config = LedgerConfig(name="demo")
    assert config.id_config("entity") == IdStrategyConfig(strategy="uuid")
    assert config.id_config("stock").strategy == "datecode"

    config = LedgerConfig(
        name="demo",
        presets=["base"],
        kinds_dir="kinds/",
        rules_dir="rules/",
        author="A <a@b.c>",
        ids={"entity": {"strategy": "uuid"}, "stock": {"strategy": "sequence", "prefix": "S"}},
    )
    assert config.id_config(ObjectType.stock).prefix == "S"


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"name": "x", "statuses": ["pending"]}, "Extra inputs"),
        ({"name": "x", "ids": {"box": {"strategy": "uuid"}}}, "unknown id strategy"),
        ({"name": "x", "ids": {"stock": {"prefix": "12"}}}, "Field required"),
        ({"name": "x", "ids": {"entity": {"strategy": "random"}}}, "strategy must be one of"),
        ({"name": " "}, "must not be empty"),
        ({"name": "x", "extra": 1}, "Extra inputs"),
        ({"name": "x", "ids": {"stock": {"strategy": "datecode", "width": 0}}}, "positive integer"),
    ],
)
def test_invalid_ledger_config(payload: dict, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        LedgerConfig(**payload)


# ---------------------------------------------------------------------------
# EffectiveSchema / fingerprint
# ---------------------------------------------------------------------------


def _status_set(*, consumed_to: tuple[str, ...] = ()) -> StatusSet:
    return StatusSet(
        {
            "stored": Status(in_stock=True, to=["consumed"]),
            "consumed": Status(in_stock=False, to=list(consumed_to)),
        }
    )


def _schema(
    *,
    fields: dict[str, Field] | None = None,
    description: str | None = None,
    rules: list[Rule] | None = None,
    status_sets: dict[str, StatusSet] | None = None,
    preset_versions: dict[str, int] | None = None,
    kind_mixins: list[str] | None = None,
) -> EffectiveSchema:
    kind = Kind(
        object="entity",
        description=description,
        fields=fields
        or {
            "name": Field(type="string", required=True),
            "size": Field(type="int"),
        },
        mixins=kind_mixins or [],
    )
    return EffectiveSchema(
        kinds={"thing": kind},
        rules=rules or [],
        status_sets=status_sets or {},
        preset_versions=preset_versions or {},
    )


def test_fingerprint_is_sha256_hex() -> None:
    fingerprint = _schema().fingerprint()
    assert len(fingerprint) == 64
    assert all(char in "0123456789abcdef" for char in fingerprint)


def test_fingerprint_ignores_field_and_kind_order() -> None:
    a = _schema(fields={"name": Field(type="string"), "size": Field(type="int")})
    b = _schema(fields={"size": Field(type="int"), "name": Field(type="string")})
    assert a.fingerprint() == b.fingerprint()


def test_fingerprint_ignores_descriptions_mixins_and_preset_versions() -> None:
    a = _schema(description=None, kind_mixins=["m"], preset_versions={"p": 1})
    b = _schema(description="human text", kind_mixins=[], preset_versions={"p": 9})
    assert a.fingerprint() == b.fingerprint()


def test_fingerprint_ignores_the_long_field_flag() -> None:
    plain = _schema(fields={"notes": Field(type="text")})
    marked = _schema(fields={"notes": Field(type="text", long=True)})
    assert plain.kinds["thing"].fields["notes"].long is False
    assert marked.kinds["thing"].fields["notes"].long is True
    assert plain.fingerprint() == marked.fingerprint()


def test_fingerprint_ignores_enum_value_order() -> None:
    a = _schema(fields={"color": Field(type="enum", values=["red", "blue"])})
    b = _schema(fields={"color": Field(type="enum", values=["blue", "red"])})
    assert a.fingerprint() == b.fingerprint()


def test_fingerprint_ignores_rule_order_and_status_order() -> None:
    rule_a = Rule(id="A", description="a", sql="SELECT 1", expect={"rows": 0})
    rule_b = Rule(id="B", description="b", sql="SELECT 2", expect={"rows": 0})
    a = _schema(rules=[rule_a, rule_b], status_sets={"tube": _status_set()})
    b = _schema(rules=[rule_b, rule_a], status_sets={"tube": _status_set()})
    assert a.fingerprint() == b.fingerprint()


def test_status_set_validates_transitions() -> None:
    status_set = StatusSet(
        {
            "stored": Status(in_stock=True, to=["consumed", "missing"]),
            "missing": Status(in_stock=False, to=["stored"]),
            "consumed": Status(in_stock=False),
        }
    )
    assert status_set.names == ["stored", "missing", "consumed"]
    assert status_set.in_stock_names == ["stored"]
    assert status_set.final_names == ["consumed"]
    assert status_set.allows("stored", "consumed")
    assert not status_set.allows("consumed", "stored")

    with pytest.raises(ValidationError, match="not a status of this set"):
        StatusSet({"stored": {"in_stock": True, "to": ["nowhere"]}})
    with pytest.raises(ValidationError, match="at least one status"):
        StatusSet({})
    with pytest.raises(ValidationError, match="must match"):
        StatusSet({"Stored": {"in_stock": True}})
    with pytest.raises(ValidationError, match="reserved SQL word"):
        StatusSet({"set": {"in_stock": True}})
    with pytest.raises(ValidationError, match="duplicates"):
        StatusSet({"stored": {"in_stock": True, "to": ["a", "a"]}, "a": {"in_stock": False}})


def test_statuses_are_optional_on_stock_kinds_and_invalid_elsewhere() -> None:
    # Spec 6: a stock kind may omit ``statuses`` (the fixed model); the model
    # itself stays model-agnostic and the loader decides which model applies.
    fixed_kind = Kind(object="stock")
    assert fixed_kind.status_set() is None
    stock = Kind(object="stock", statuses=_status_set())
    assert stock.status_set() is not None
    with pytest.raises(ValidationError, match="only valid on stock kinds"):
        Kind(object="entity", statuses=_status_set())
    with pytest.raises(ValidationError, match="only valid on stock kinds"):
        Kind(object="box", geometry={"type": "slotless"}, statuses="tube")


def test_fingerprint_changes_with_semantics() -> None:
    base = _schema()
    changed_default = _schema(
        fields={
            "name": Field(type="string", required=True),
            "size": Field(type="int", default=1),
        }
    )
    changed_rule = _schema(
        rules=[Rule(id="A", description="a", sql="SELECT 1", expect={"rows": 0})]
    )
    changed_status = _schema(status_sets={"tube": _status_set(consumed_to=("stored",))})
    fingerprints = {
        base.fingerprint(),
        changed_default.fingerprint(),
        changed_rule.fingerprint(),
        changed_status.fingerprint(),
    }
    assert len(fingerprints) == 4


def test_schema_lookup_helpers() -> None:
    entity = Kind(object="entity")
    stock = Kind(object="stock", statuses=_status_set())
    box = Kind(object="box", geometry={"type": "slotless"})
    schema = EffectiveSchema(kinds={"e": entity, "s": stock, "b": box})
    assert list(schema.entity_kinds) == ["e"]
    assert list(schema.stock_kinds) == ["s"]
    assert list(schema.box_kinds) == ["b"]
    assert schema.kind("e") is entity
    assert schema.has_kind("s")
    assert list(schema.kinds_for(ObjectType.box)) == ["b"]
    grouped = schema.kinds_by_object
    assert list(grouped) == ["entity", "stock", "box"]
    assert list(grouped["box"]) == ["b"]
    with pytest.raises(SchemaError, match="unknown kind"):
        schema.kind("nope")


def test_schema_error_renders_file_and_path() -> None:
    error = SchemaError("boom", file="molbio/preset.yaml", path="kinds.plasmid.fields.x")
    assert str(error) == "molbio/preset.yaml: kinds.plasmid.fields.x: boom"
    assert error.file == "molbio/preset.yaml"
    assert error.path == "kinds.plasmid.fields.x"
    assert error.exit_code == 1
    assert str(SchemaError("plain")) == "plain"


# ---------------------------------------------------------------------------
# Spec 5: box names must work as view names
# ---------------------------------------------------------------------------


def test_spec_version_is_8() -> None:
    assert SPEC_VERSION == 8


def test_core_views_reserve_their_v_box_names() -> None:
    assert "v_box_slots" in CORE_VIEW_NAMES
    assert BOX_VIEW_PREFIX == "v_box_"
    assert BOX_NAME_MAX_LENGTH == 58


def test_box_name_problem_accepts_ordinary_names() -> None:
    assert box_name_problem("WFL-10-01", []) is None
    assert box_name_problem("G", []) is None
    # An exact duplicate is not the naming rule's job: ``tm box add`` has its
    # own duplicate check and CSV import updates the existing box.
    assert box_name_problem("tube", ["tube"]) is None
    assert box_name_problem("Rack-2", ["Rack-1"]) is None
    assert box_name_problem("x" * BOX_NAME_MAX_LENGTH, []) is None


def test_box_name_problem_refuses_a_core_view_collision() -> None:
    problem = box_name_problem("slots", [])
    assert problem is not None
    assert "core view" in problem and "v_box_slots" in problem
    # Dolt stores view names in lowercase, so the collision is case-insensitive.
    assert box_name_problem("SLOTS", []) is not None
    # The other core views are not v_box_*: a box may be named after them.
    assert box_name_problem("aliquot_location", []) is None
    assert box_name_problem("stock_status", []) is None


def test_box_name_problem_refuses_a_name_longer_than_58() -> None:
    problem = box_name_problem("x" * (BOX_NAME_MAX_LENGTH + 1), [])
    assert problem is not None
    assert "59" in problem and "58" in problem


def test_box_name_problem_refuses_a_case_only_duplicate() -> None:
    problem = box_name_problem("Tube", ["TUBE"])
    assert problem is not None
    assert "differs only in case" in problem and "TUBE" in problem
    assert box_name_problem("TUBE", ["tube", "other"]) is not None
    assert box_name_problem("wfl-10-01", ["WFL-10-01"]) is not None
    # ... but a genuinely different name is fine.
    assert box_name_problem("Tube-2", ["TUBE-1"]) is None


# ---------------------------------------------------------------------------
# long: true — display in tm schema show, intent in the bundled presets
# ---------------------------------------------------------------------------

REPO_PRESETS = Path(__file__).resolve().parents[1] / "presets"


def test_schema_show_payload_adds_long_only_when_set() -> None:
    from tetramark.cli.schema import _kind_payload

    kind = Kind(
        object="entity",
        fields={
            "notes": Field(type="text", long=True),
            "name": Field(type="string"),
        },
    )
    payload = _kind_payload(EffectiveSchema(kinds={"thing": kind}), "thing")
    assert payload["fields"]["notes"]["long"] is True
    assert "long" not in payload["fields"]["name"]
    # The payload addition must not leak into the fingerprint input.
    assert "long" not in kind.fields["notes"].normalised()


def test_bundled_presets_mark_every_text_and_sequence_field_long() -> None:
    missing: list[str] = []
    for path in sorted(REPO_PRESETS.glob("*.yaml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        field_groups = list((document.get("mixins") or {}).values())
        field_groups += [
            kind.get("fields") or {}
            for kind in (document.get("kinds") or {}).values()
        ]
        for fields in field_groups:
            for field_name, spec in fields.items():
                if spec.get("type") not in ("text", "sequence"):
                    continue
                if spec.get("long") is not True:
                    missing.append(f"{path.name}:{field_name}")
    assert missing == []


# ---------------------------------------------------------------------------
# Pattern tests: the generated Dolt test is a full match, like re.fullmatch
# ---------------------------------------------------------------------------

#: ``REGEXP`` in Dolt (as in MySQL) *searches* the value, so the generated test
#: has to anchor the preset's regex itself; otherwise an unanchored pattern
#: accepts an invalid value that merely contains a valid run (``ACGTZ``).  The
#: app checks the same pattern with ``re.fullmatch`` (core/objects.py).
PATTERN_SQL = (
    "REGEXP '^(?:[ACGTURYKMSWBDHVN]+)$'",
    "REGEXP '^(?:[ACDEFGHIKLMNPQRSTVWYXBZUO*]+)$'",
)


def _generated_pattern_queries(preset: str) -> dict[str, str]:
    schema = load_schema(
        LedgerConfig(name="pattern", presets=[str(REPO_PRESETS / f"{preset}.yaml")])
    )
    return {
        test.name: test.query
        for test in generate_tests(schema)
        if "_pattern_" in test.name
    }


def test_generated_pattern_test_anchors_the_preset_regex() -> None:
    """Ticket rq9: wrapping makes the SQL test agree with the app's fullmatch."""
    proteins = _generated_pattern_queries("proteins")
    nucleic = _generated_pattern_queries("nucleic_acids")
    library = _generated_pattern_queries("library")

    # proteins 4 splits the shared mixin: the construct's ``sequence`` is amino
    # acids (the same alphabet as ``mature_sequence``), while ``plasmid`` and
    # the oligo keep the nucleotide pattern.
    assert PATTERN_SQL[1] in proteins["schema_protein_construct_pattern_sequence"]
    assert PATTERN_SQL[1] in proteins["schema_protein_construct_pattern_mature_sequence"]
    assert PATTERN_SQL[0] in nucleic["schema_plasmid_pattern_sequence"]
    assert PATTERN_SQL[0] in nucleic["schema_oligo_pattern_sequence"]
    # An already-anchored preset pattern (library's ISBN) embeds unchanged.
    assert "REGEXP '^(?:^[0-9Xx][0-9Xx-]{8,16}$)$'" in library[
        "schema_book_pattern_isbn"
    ]

    # Dolt/MySQL REGEXP is a search: the raw regex would accept ``ACGTZ`` on its
    # ``ACGT`` prefix, and the anchored form the generator emits rejects it.
    raw = re.compile("[ACGTURYKMSWBDHVN]+")
    wrapped = re.compile("^(?:[ACGTURYKMSWBDHVN]+)$")
    assert raw.search("ACGTZ") is not None and wrapped.fullmatch("ACGTZ") is None
    assert wrapped.fullmatch("ACGTN") is not None
    protein_wrapped = re.compile("^(?:[ACDEFGHIKLMNPQRSTVWYXBZUO*]+)$")
    assert protein_wrapped.fullmatch("MKWV*") is not None
    assert protein_wrapped.fullmatch("MKW.") is None
