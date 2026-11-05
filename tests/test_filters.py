"""The shared filter backend: parsing ``field=value`` / ``field~pattern``.

The parser and the SQL builder are pure, so the whole module runs in the fast
lane: no Dolt, no ledger (Tetramark-6ms.1).  The database side of a pattern
(what ``REGEXP`` means) is pinned by ``tests/test_where_regex.py``.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from tetramark.cli.app import app
from tetramark.core.filters import (
    EQUALS,
    REGEXP,
    Filter,
    build_conditions,
    check_fields,
    empty_condition,
    filter_fields,
    parse_filters,
)
from tetramark.errors import UsageError
from tetramark.schema.model import Field, FieldType, Kind, ObjectType, SqlType

pytestmark = pytest.mark.fast


def test_a_filter_splits_at_the_first_operator() -> None:
    """The value may contain the other operator (and its own)."""
    assert parse_filters(["name=pUC=19"]) == (
        Filter("name", EQUALS, "pUC=19"),
    )
    assert parse_filters(["name~a~b"]) == (Filter("name", REGEXP, "a~b"),)
    assert parse_filters(["name=a~b"]) == (Filter("name", EQUALS, "a~b"),)


def test_a_mapping_is_equality_only() -> None:
    """A mapping has no operator: its value is the value, never a pattern."""
    assert parse_filters({"name": "pUC19"}) == (Filter("name", EQUALS, "pUC19"),)
    # A key that tries to be a pattern is not an identifier.
    with pytest.raises(UsageError, match="not a valid field name"):
        parse_filters({"name~x": "y"})


def test_a_mapping_none_is_the_empty_filter_not_the_text_none() -> None:
    """``{"f": None}`` parses as ``f=``, the bug fix this release ships."""
    assert parse_filters({"name": None}) == (Filter("name", EQUALS, ""),)
    assert parse_filters({"name": ""}) == (Filter("name", EQUALS, ""),)
    assert parse_filters({"name": None}) == parse_filters(["name="])
    # The filter text used in error messages is the empty form, not "name=None".
    assert parse_filters({"name": None})[0].text == "name="


def test_a_malformed_filter_is_a_usage_error() -> None:
    with pytest.raises(UsageError, match="expected field=value or field~pattern"):
        parse_filters(["name"])
    with pytest.raises(UsageError, match="the field name is empty"):
        parse_filters(["=x"])


def test_a_field_name_must_be_an_identifier() -> None:
    with pytest.raises(UsageError, match="not a valid field name"):
        parse_filters(["na me=x"])
    with pytest.raises(UsageError, match="not a valid field name"):
        parse_filters(["name;drop=x"])


def test_a_field_is_filtered_by_equals_at_most_once() -> None:
    with pytest.raises(UsageError, match="appears twice"):
        parse_filters(["name=a", "name=b"])


def test_a_field_cannot_mix_the_two_operators() -> None:
    with pytest.raises(UsageError, match="both"):
        parse_filters(["name=a", "name~b"])
    with pytest.raises(UsageError, match="both"):
        parse_filters(["name~b", "name=a"])


def test_a_field_takes_several_patterns() -> None:
    filters = parse_filters(["name~a", "name~b", "kind=x"])
    assert filters == (
        Filter("name", REGEXP, "a"),
        Filter("name", REGEXP, "b"),
        Filter("kind", EQUALS, "x"),
    )


def test_an_empty_pattern_is_a_usage_error() -> None:
    with pytest.raises(UsageError, match="must not be empty"):
        parse_filters(["name~"])


def test_an_unknown_field_lists_the_valid_ones() -> None:
    with pytest.raises(UsageError, match="unknown field"):
        check_fields(parse_filters(["bogus=1"]), ["name", "kind"])
    with pytest.raises(UsageError, match="valid fields: kind, name"):
        check_fields(parse_filters(["bogus=1"]), ["name", "kind"])
    # The fields of the filters are reported, whatever operator they use.
    assert filter_fields(parse_filters(["name~a", "kind=b"])) == {"name", "kind"}


def test_the_conditions_are_one_per_filter() -> None:
    """An ``=`` filter is a comparison, a ``~`` filter a ``REGEXP``."""
    coerced: list[tuple[str, str]] = []

    def coerce(field: str, raw: str) -> str:
        coerced.append((field, raw))
        return raw.upper()

    conditions, params = build_conditions(
        parse_filters(["name=a", "kind~^p", "code~x$"]),
        expressions={"name": "`name`", "kind": "`kind`", "code": "k.`code`"},
        coerce=coerce,
    )

    assert conditions == ["`name` = %s", "`kind` REGEXP %s", "k.`code` REGEXP %s"]
    assert params == ["A", "^p", "x$"]
    # Only the exact filter is coerced; a pattern is matched as it is.
    assert coerced == [("name", "a")]


# ---------------------------------------------------------------------------
# The empty value (``field=``)
# ---------------------------------------------------------------------------


def _kind_with(**fields: Field) -> Kind:
    """An entity kind carrying the given fields (enough for empty_condition)."""
    return Kind(object=ObjectType.entity, fields=fields)


@pytest.mark.parametrize(
    "field",
    [
        Field(type=FieldType.string),
        Field(type=FieldType.text),
        Field(type=FieldType.enum, values=["a", "b"]),
        Field(type=FieldType.sequence),
        Field(type=FieldType.ref, to="item"),
    ],
)
def test_an_empty_condition_of_a_text_like_field_matches_null_or_empty(
    field: Field,
) -> None:
    kind = _kind_with(value=field)

    assert empty_condition(kind, "value", "`value`") == (
        "(`value` IS NULL OR `value` = '')"
    )


@pytest.mark.parametrize(
    "field",
    [
        Field(type=FieldType.int),
        Field(type=FieldType.decimal),
        Field(type=FieldType.bool),
        Field(type=FieldType.date),
        Field(type=FieldType.datetime),
    ],
)
def test_an_empty_condition_of_a_non_text_field_matches_null_only(
    field: Field,
) -> None:
    """A stored ``0`` (or false, or a date) must never look empty."""
    kind = _kind_with(value=field)

    assert empty_condition(kind, "value", "`value`") == "`value` IS NULL"


def test_an_empty_condition_of_a_core_column_is_text_like_except_created_at() -> None:
    kind = _kind_with()

    assert empty_condition(kind, "notes", "`notes`") == (
        "(`notes` IS NULL OR `notes` = '')"
    )
    assert empty_condition(kind, "created_at", "`created_at`") == (
        "`created_at` IS NULL"
    )


def test_an_empty_condition_of_a_computed_field_keeps_the_usage_error() -> None:
    kind = _kind_with(
        display=Field(
            type=FieldType.computed, expr="UPPER(name)", sql_type=SqlType.string
        )
    )

    with pytest.raises(UsageError, match="field 'display' is computed"):
        empty_condition(kind, "display", "`display`")


def test_an_empty_exact_filter_skips_coerce_and_uses_the_empty_callback() -> None:
    """``field=`` is not a value: no coercion, no parameter, type-aware SQL."""
    kind = _kind_with(
        label=Field(type=FieldType.string), count=Field(type=FieldType.int)
    )
    coerced: list[tuple[str, str]] = []

    def coerce(field: str, raw: str) -> str:
        coerced.append((field, raw))
        return raw

    conditions, params = build_conditions(
        parse_filters(["label=", "count=", "note~^x"]),
        expressions={"label": "`label`", "count": "`count`", "note": "`note`"},
        coerce=coerce,
        empty=lambda field, expression: empty_condition(kind, field, expression),
    )

    assert conditions == [
        "(`label` IS NULL OR `label` = '')",
        "`count` IS NULL",
        "`note` REGEXP %s",
    ]
    assert params == ["^x"]
    assert coerced == []


def test_an_empty_exact_filter_defaults_to_null_or_empty_string() -> None:
    """A caller without a schema matches the column as text."""
    conditions, params = build_conditions(
        parse_filters(["name="]), expressions={"name": "`name`"}
    )

    assert conditions == ["(`name` IS NULL OR `name` = '')"]
    assert params == []


def test_a_non_empty_value_still_coerces_and_compares() -> None:
    coerced: list[tuple[str, str]] = []

    def coerce(field: str, raw: str) -> int:
        coerced.append((field, raw))
        return int(raw)

    conditions, params = build_conditions(
        parse_filters(["count=0"]),
        expressions={"count": "`count`"},
        coerce=coerce,
        empty=lambda field, expression: "not used",
    )

    assert conditions == ["`count` = %s"]
    assert params == [0]
    assert coerced == [("count", "0")]


@pytest.mark.parametrize("group", ["entity", "stock", "box"])
def test_the_where_help_mentions_the_pattern_operator(group: str) -> None:
    """The help says how a pattern is written and that it is case-sensitive."""
    result = CliRunner().invoke(app, [group, "list", "--help"])

    assert result.exit_code == 0, result.output
    assert "field~pattern" in result.output
    assert "(?i)" in result.output
