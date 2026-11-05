"""The shared filter backend: the operators of ``--where``.

The parser and the SQL builder are pure, so the whole module runs in the fast
lane: no Dolt, no ledger (Tetramark-6ms.1).  The database side of a pattern
(what ``REGEXP`` means) is pinned by ``tests/test_where_regex.py``, and the
behaviour of the operators against real rows by
``tests/test_where_operators.py`` (Tetramark-d0x.1).
"""

from __future__ import annotations

from datetime import datetime

import pytest
from typer.testing import CliRunner

from tetramark.cli.app import app
from tetramark.core.filters import (
    EQUALS,
    REGEXP,
    NOT_EQUALS,
    NOT_REGEXP,
    LESS,
    LESS_EQUAL,
    GREATER,
    GREATER_EQUAL,
    Filter,
    build_any_condition,
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
    with pytest.raises(UsageError, match="expected field=value, field~pattern"):
        parse_filters(["name"])
    with pytest.raises(UsageError, match="expected field=value, field~pattern"):
        parse_filters(["=x"])
    with pytest.raises(UsageError, match="expected field=value, field~pattern"):
        parse_filters([" =x"])
    with pytest.raises(UsageError, match="expected field=value, field~pattern"):
        parse_filters(["f x=1"])
    with pytest.raises(UsageError, match="expected field=value, field~pattern"):
        parse_filters(["f!x"])


def test_a_field_name_must_be_an_identifier() -> None:
    # A mapping key is a field name, never an operator.
    with pytest.raises(UsageError, match="not a valid field name"):
        parse_filters({"name~x": "y"})
    with pytest.raises(UsageError, match="not a valid field name"):
        parse_filters({"na me": "x"})
    # A list filter names its field with the identifier it starts with.
    with pytest.raises(UsageError, match="expected field=value, field~pattern"):
        parse_filters(["na me=x"])
    with pytest.raises(UsageError, match="expected field=value, field~pattern"):
        parse_filters(["name;drop=x"])
    with pytest.raises(UsageError, match="the field name is empty"):
        parse_filters({"": "x"})


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


# ---------------------------------------------------------------------------
# The operators (Tetramark-d0x.1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "field", "operator", "value"),
    [
        ("name=a<b", "name", EQUALS, "a<b"),
        ("name=a!b", "name", EQUALS, "a!b"),
        ("name==x", "name", EQUALS, "=x"),
        ("name~a~b", "name", REGEXP, "a~b"),
        ("f!=x", "f", NOT_EQUALS, "x"),
        ("f!~GFP", "f", NOT_REGEXP, "GFP"),
        ("count<10", "count", LESS, "10"),
        ("count<=10", "count", LESS_EQUAL, "10"),
        ("count>10", "count", GREATER, "10"),
        ("length_bp>=1000", "length_bp", GREATER_EQUAL, "1000"),
        (" name = x", "name", EQUALS, " x"),
        ("name= x", "name", EQUALS, " x"),
    ],
)
def test_the_operator_is_the_one_after_the_field_name(
    text: str, field: str, operator: str, value: str
) -> None:
    """The value may hold any operator character; the name comes first."""
    assert parse_filters([text]) == (Filter(field, operator, value),)


def test_equals_cannot_be_mixed_with_any_other_operator() -> None:
    with pytest.raises(UsageError, match="both '=' and '!='"):
        parse_filters(["name=a", "name!=b"])
    with pytest.raises(UsageError, match="both '=' and '!='"):
        parse_filters(["name!=b", "name=a"])
    with pytest.raises(UsageError, match="both '=' and '>='"):
        parse_filters(["name=a", "name>=b"])
    with pytest.raises(UsageError, match="both '=' and '>='"):
        parse_filters(["name>=b", "name=a"])
    # The message of a pattern is the one the filter backend always used.
    with pytest.raises(UsageError, match="both '=' and '~'"):
        parse_filters(["name~b", "name=a"])


def test_the_other_operators_may_be_repeated_and_are_anded() -> None:
    filters = parse_filters(
        ["length_bp>=1000", "length_bp<5000", "name~^p", "name!~GFP"]
    )
    assert filters == (
        Filter("length_bp", GREATER_EQUAL, "1000"),
        Filter("length_bp", LESS, "5000"),
        Filter("name", REGEXP, "^p"),
        Filter("name", NOT_REGEXP, "GFP"),
    )


def test_an_empty_pattern_or_comparison_value_is_a_usage_error() -> None:
    with pytest.raises(UsageError, match="must not be empty"):
        parse_filters(["name~"])
    with pytest.raises(UsageError, match="must not be empty"):
        parse_filters(["name!~"])
    with pytest.raises(UsageError, match="must not be empty"):
        parse_filters(["count<"])
    with pytest.raises(UsageError, match="must not be empty"):
        parse_filters(["count<="])
    with pytest.raises(UsageError, match="must not be empty"):
        parse_filters(["count>"])
    with pytest.raises(UsageError, match="must not be empty"):
        parse_filters(["count>="])


def test_the_conditions_of_every_operator() -> None:
    """Every operator is one condition and one parameter."""
    conditions, params = build_conditions(
        parse_filters(
            [
                "name=a",
                "kind!=b",
                "note~^p",
                "code!~x$",
                "count>=10",
                "size<5",
            ]
        ),
        expressions={
            "name": "`name`",
            "kind": "`kind`",
            "note": "`note`",
            "code": "`code`",
            "count": "`count`",
            "size": "`size`",
        },
    )

    assert conditions == [
        "`name` = %s",
        "(`kind` IS NULL OR `kind` <> %s)",
        "`note` REGEXP %s",
        "(`code` IS NULL OR NOT (`code` REGEXP %s))",
        "`count` >= %s",
        "`size` < %s",
    ]
    assert params == ["a", "b", "^p", "x$", "10", "5"]


def test_an_empty_not_equals_filter_is_the_complement_of_the_empty_filter() -> None:
    """``field!=`` means "the field has a value"; it excludes NULL and ''."""
    kind = _kind_with(
        label=Field(type=FieldType.string), count=Field(type=FieldType.int)
    )

    conditions, params = build_conditions(
        parse_filters(["label!=", "count!="]),
        expressions={"label": "`label`", "count": "`count`"},
        empty=lambda field, expression: empty_condition(kind, field, expression),
    )

    assert conditions == [
        "NOT ((`label` IS NULL OR `label` = ''))",
        "NOT (`count` IS NULL)",
    ]
    assert params == []


def test_an_empty_not_equals_filter_without_a_schema_matches_the_column_as_text() -> None:
    conditions, params = build_conditions(
        parse_filters(["name!="]), expressions={"name": "`name`"}
    )

    assert conditions == ["NOT ((`name` IS NULL OR `name` = ''))"]
    assert params == []


@pytest.mark.parametrize(
    "field",
    [
        Field(type=FieldType.bool),
        Field(type=FieldType.enum, values=["a", "b"]),
        Field(type=FieldType.sequence),
        Field(type=FieldType.ref, to="item"),
    ],
)
def test_an_ordered_comparison_on_a_field_without_an_order_is_a_usage_error(
    field: Field,
) -> None:
    kind = _kind_with(value=field)

    with pytest.raises(UsageError, match="has no order"):
        build_conditions(
            parse_filters(["value>1"]),
            expressions={"value": "`value`"},
            kind=kind,
        )


def test_an_ordered_comparison_on_a_computed_field_is_a_usage_error() -> None:
    kind = _kind_with(
        display=Field(
            type=FieldType.computed, expr="UPPER(name)", sql_type=SqlType.string
        )
    )

    with pytest.raises(UsageError, match="is computed"):
        build_conditions(
            parse_filters(["display>a"]),
            expressions={"display": "`display`"},
            kind=kind,
        )


def test_an_ordered_comparison_on_created_at_needs_an_iso_value() -> None:
    kind = _kind_with()

    conditions, params = build_conditions(
        parse_filters(["created_at>2024-01-01"]),
        expressions={"created_at": "`created_at`"},
        kind=kind,
    )

    assert conditions == ["`created_at` > %s"]
    # A date means its midnight: the column is a DATETIME, so the value is
    # compared as a datetime, never as an arbitrary string.
    assert params == [datetime(2024, 1, 1)]

    conditions, params = build_conditions(
        parse_filters(["created_at<=2024-01-31T12:30:00"]),
        expressions={"created_at": "`created_at`"},
        kind=kind,
    )

    assert conditions == ["`created_at` <= %s"]
    assert params == [datetime(2024, 1, 31, 12, 30)]

    with pytest.raises(UsageError, match="is not an ISO date or datetime"):
        build_conditions(
            parse_filters(["created_at>yesterday"]),
            expressions={"created_at": "`created_at`"},
            kind=kind,
        )


def test_the_where_help_names_every_operator() -> None:
    """The help says which operators exist (all of them single-quoted)."""
    result = CliRunner().invoke(app, ["entity", "list", "--help"])

    assert result.exit_code == 0, result.output
    for operator in ("field=value", "field~pattern", "field!=value", "field!~pattern"):
        assert operator in result.output
    assert "field<value" in result.output


# ---------------------------------------------------------------------------
# The one OR group (Tetramark-d0x.2)
# ---------------------------------------------------------------------------


def test_the_or_group_is_parsed_with_its_own_option_name() -> None:
    """``--where-any`` takes every operator and the same list/mapping forms."""
    filters = parse_filters(
        ["resistance=kan", "count>=10", "name!~^pUC"],
        option="--where-any",
        allow_repeats=True,
    )

    assert filters == (
        Filter("resistance", EQUALS, "kan"),
        Filter("count", GREATER_EQUAL, "10"),
        Filter("name", NOT_REGEXP, "^pUC"),
    )
    assert parse_filters(
        {"resistance": "kan"}, option="--where-any", allow_repeats=True
    ) == (Filter("resistance", EQUALS, "kan"),)
    assert parse_filters(
        {"name": None}, option="--where-any", allow_repeats=True
    ) == (Filter("name", EQUALS, ""),)


def test_the_or_group_has_no_per_field_repetition_rules() -> None:
    """Repeating a field is the canonical ``--where-any`` use."""
    repeated = parse_filters(
        ["resistance=kan", "resistance=amp", "name~^pUC", "name=a"],
        option="--where-any",
        allow_repeats=True,
    )

    assert repeated == (
        Filter("resistance", EQUALS, "kan"),
        Filter("resistance", EQUALS, "amp"),
        Filter("name", REGEXP, "^pUC"),
        Filter("name", EQUALS, "a"),
    )
    # The AND group keeps both rules; ``allow_repeats`` is opt-in.
    with pytest.raises(UsageError, match="appears twice"):
        parse_filters(["resistance=kan", "resistance=amp"])
    with pytest.raises(UsageError, match="both"):
        parse_filters(["name=a", "name~^pUC"])


def test_the_empty_errors_of_the_or_group_name_the_option() -> None:
    """Only the repetition rules are dropped, not the empty-value ones."""
    with pytest.raises(UsageError, match="--where-any"):
        parse_filters(["name~"], option="--where-any", allow_repeats=True)
    with pytest.raises(UsageError, match="--where-any"):
        parse_filters(["name!~"], option="--where-any", allow_repeats=True)
    with pytest.raises(UsageError, match="--where-any"):
        parse_filters(["count<"], option="--where-any", allow_repeats=True)
    with pytest.raises(UsageError, match="the field name is empty"):
        parse_filters({"": "x"}, option="--where-any", allow_repeats=True)


def test_the_or_group_is_one_parenthesised_condition() -> None:
    """The alternatives are ORed, in order, with their parameters in order."""
    condition, params = build_any_condition(
        parse_filters(
            ["name=a", "kind~^p", "count>=10"],
            option="--where-any",
            allow_repeats=True,
        ),
        expressions={"name": "`name`", "kind": "`kind`", "count": "`count`"},
    )

    assert condition == "(`name` = %s OR `kind` REGEXP %s OR `count` >= %s)"
    assert params == ["a", "^p", "10"]


def test_an_alternative_whose_field_a_kind_lacks_is_dropped() -> None:
    """Per-kind scoping: a field the kind does not declare is false for it."""
    filters = parse_filters(
        ["supplier=acme", "batch=B1"], option="--where-any", allow_repeats=True
    )

    condition, params = build_any_condition(
        filters, expressions={"supplier": "`supplier`"}
    )
    assert condition == "(`supplier` = %s)"
    assert params == ["acme"]

    condition, params = build_any_condition(
        filters, expressions={"batch": "`batch`"}
    )
    assert condition == "(`batch` = %s)"
    assert params == ["B1"]

    # Every alternative dropped: the kind contributes nothing.
    condition, params = build_any_condition(
        filters, expressions={"kind": "`kind`"}
    )
    assert condition is None
    assert params == []


def test_no_alternative_at_all_is_no_condition() -> None:
    """``--where-any`` absent (or empty) adds nothing to a kind's query."""
    assert build_any_condition([], expressions={"name": "`name`"}) == (None, [])


def test_the_or_group_shares_the_coercion_and_empty_rules() -> None:
    """An alternative is built exactly like one ``--where`` filter."""
    kind = _kind_with(
        label=Field(type=FieldType.string), count=Field(type=FieldType.int)
    )
    coerced: list[tuple[str, str]] = []

    def coerce(field: str, raw: str) -> str:
        coerced.append((field, raw))
        return raw.upper()

    condition, params = build_any_condition(
        parse_filters(
            ["label=", "count=a"], option="--where-any", allow_repeats=True
        ),
        expressions={"label": "`label`", "count": "`count`"},
        coerce=coerce,
        empty=lambda field, expression: empty_condition(kind, field, expression),
    )

    assert condition == "((`label` IS NULL OR `label` = '') OR `count` = %s)"
    assert params == ["A"]
    assert coerced == [("count", "a")]


def test_the_or_group_reports_its_own_option_for_a_typed_error() -> None:
    """An ordered comparison on a field without an order names the group."""
    kind = _kind_with(value=Field(type=FieldType.bool))

    with pytest.raises(UsageError, match="--where-any"):
        build_any_condition(
            parse_filters(
                ["value>1"], option="--where-any", allow_repeats=True
            ),
            expressions={"value": "`value`"},
            kind=kind,
        )


@pytest.mark.parametrize(
    "command",
    [["entity", "list"], ["stock", "list"], ["box", "list"], ["find"]],
    ids=["entity", "stock", "box", "find"],
)
def test_the_help_documents_the_or_group(command: list[str]) -> None:
    """The four commands that filter advertise ``--where-any``."""
    result = CliRunner().invoke(app, [*command, "--help"])

    assert result.exit_code == 0, result.output
    assert "--where-any" in result.output
    assert "OR group" in result.output
    # The examples are single-quoted: the shell would read <, > and !.
    assert "'resistance=kan'" in result.output
    assert "'resistance=amp'" in result.output
