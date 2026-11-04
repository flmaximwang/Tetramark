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
    filter_fields,
    parse_filters,
)
from tetramark.errors import UsageError

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


@pytest.mark.parametrize("group", ["entity", "stock", "box"])
def test_the_where_help_mentions_the_pattern_operator(group: str) -> None:
    """The help says how a pattern is written and that it is case-sensitive."""
    result = CliRunner().invoke(app, [group, "list", "--help"])

    assert result.exit_code == 0, result.output
    assert "field~pattern" in result.output
    assert "(?i)" in result.output
