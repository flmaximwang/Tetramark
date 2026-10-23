"""Unit tests for SQL literal rendering and output parsing (no Dolt process)."""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal

import pytest

from tetramark.driver.base import (
    commit_statements,
    ensure_ascii,
    is_nothing_to_commit,
    literal,
    looks_like_error,
    render_sql,
)
from tetramark.driver.embedded import _check_output, _parse_result_sets
from tetramark.errors import LedgerError


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "NULL"),
        (True, "TRUE"),
        (False, "FALSE"),
        (0, "0"),
        (42, "42"),
        (-7, "-7"),
        (1.5, "1.5"),
        (Decimal("1.250000"), "1.250000"),
        (Decimal("0.0000000001"), "0.0000000001"),
        (date(2026, 6, 24), "'2026-06-24'"),
        (datetime(2026, 6, 24, 8, 5, 3), "'2026-06-24 08:05:03'"),
        (time(8, 5, 3), "'08:05:03'"),
        ("plain", "'plain'"),
        ("O'Brien", "'O''Brien'"),
        ("back\\slash", "'back\\\\slash'"),
        ("台账 管", "'台账 管'"),
        ("line\nbreak", "'line\\nbreak'"),
        ("carriage\rreturn", "'carriage\\rreturn'"),
        ("nul\x00byte", "'nul\\0byte'"),
        ("ctrl\x1az", "'ctrl\\Zz'"),
        ("100%", "'100%'"),
        (b"\x00\xff", "X'00ff'"),
    ],
)
def test_literal_round_trips(value: object, expected: str) -> None:
    assert literal(value) == expected


def test_literal_refuses_non_finite_numbers() -> None:
    with pytest.raises(LedgerError):
        literal(float("nan"))
    with pytest.raises(LedgerError):
        literal(float("inf"))
    with pytest.raises(LedgerError):
        literal(Decimal("NaN"))


def test_literal_refuses_unknown_types() -> None:
    with pytest.raises(LedgerError, match="cannot bind"):
        literal(object())


def test_render_sql_binds_positional_parameters() -> None:
    sql = "SELECT * FROM t WHERE a = %s AND b = %s AND c = %s"
    assert render_sql(sql, (1, "x", None)) == "SELECT * FROM t WHERE a = 1 AND b = 'x' AND c = NULL"


def test_render_sql_without_parameters_is_unchanged() -> None:
    sql = "SELECT '%s' -- %s\n"
    assert render_sql(sql, None) == sql


def test_render_sql_ignores_placeholders_inside_strings_and_identifiers() -> None:
    sql = "SELECT '%s' AS s, \"%s\" AS d, `%s` FROM t WHERE v = %s"
    rendered = render_sql(sql, ("bound",))
    assert rendered == "SELECT '%s' AS s, \"%s\" AS d, `%s` FROM t WHERE v = 'bound'"


def test_render_sql_ignores_placeholders_inside_comments() -> None:
    sql = "SELECT 1 -- %s\n/* %s */ # %s\n, %s AS x"
    assert render_sql(sql, (7,)).endswith(", 7 AS x")


def test_render_sql_handles_doubled_quotes_and_escaped_quotes() -> None:
    sql = "SELECT 'it''s %s', 'it\\'s %s', %s"
    assert render_sql(sql, ("v",)).endswith(", 'v'")


def test_render_sql_collapses_double_percent() -> None:
    assert render_sql("SELECT %s LIKE 'a%%'", ("b",)) == "SELECT 'b' LIKE 'a%'"
    assert render_sql("SELECT %%", ()) == "SELECT %"


def test_render_sql_keeps_percent_in_parameter_values() -> None:
    assert render_sql("SELECT %s", ("100%% done",)) == "SELECT '100%% done'"


def test_render_sql_checks_parameter_count() -> None:
    with pytest.raises(LedgerError, match="too few"):
        render_sql("SELECT %s, %s", (1,))
    with pytest.raises(LedgerError, match="too many"):
        render_sql("SELECT %s", (1, 2))


def test_ensure_ascii() -> None:
    ensure_ascii("add(entity): plasmid p1")
    with pytest.raises(LedgerError, match="ASCII"):
        ensure_ascii("加了 p1")
    with pytest.raises(LedgerError, match="ASCII"):
        commit_statements("加了 p1")


def test_commit_statements_shape() -> None:
    assert commit_statements("x") == [
        "CALL DOLT_ADD('-A')",
        "CALL DOLT_COMMIT('-m', 'x')",
    ]
    assert commit_statements("x", "A <a@b.c>") == [
        "CALL DOLT_ADD('-A')",
        "CALL DOLT_COMMIT('-m', 'x', '--author', 'A <a@b.c>')",
    ]


def test_commit_statements_escape_the_message() -> None:
    statements = commit_statements("it's %s")
    assert statements[1] == "CALL DOLT_COMMIT('-m', 'it''s %s')"


@pytest.mark.parametrize(
    "text",
    [
        "error on line 1 for query SELECT * FROM nope: table not found: nope",
        "Error parsing SQL: syntax error",
        "FATAL: something",
        "panic: boom",
    ],
)
def test_looks_like_error_recognises_dolt_errors(text: str) -> None:
    assert looks_like_error(text)


@pytest.mark.parametrize(
    "text",
    ["", "warning: you are on an old version", "rows: 3", "  info only"],
)
def test_looks_like_error_ignores_non_errors(text: str) -> None:
    assert not looks_like_error(text)


def test_nothing_to_commit_detection() -> None:
    assert is_nothing_to_commit("error on line 1: nothing to commit")
    assert is_nothing_to_commit("Nothing to commit")
    assert not is_nothing_to_commit("syntax error")


def test_parse_output_accepts_newline_delimited_result_sets() -> None:
    stdout = '{"rows": [{"a":1}]}\n\n{"rows": [{"b":2}]}\n'
    assert _parse_result_sets(stdout) == [[{"a": 1}], [{"b": 2}]]


def test_parse_output_skips_empty_result_objects() -> None:
    assert _parse_result_sets("{}\n\n{\"rows\": [{\"a\": 1}]}\n") == [[{"a": 1}]]
    assert _parse_result_sets("") == []


def test_check_output_detects_error_with_exit_code_zero() -> None:
    # Dolt can exit 0 while printing an error; the driver must still raise.
    with pytest.raises(LedgerError, match="nothing to commit"):
        _check_output("", "error on line 1 for query CALL DOLT_COMMIT: nothing to commit", 0)


def test_check_output_detects_error_on_stdout_with_exit_code_zero() -> None:
    with pytest.raises(LedgerError, match="table not found"):
        _check_output("error on line 1 for query SELECT: table not found", "", 0)


def test_check_output_detects_error_from_exit_code() -> None:
    with pytest.raises(LedgerError, match="syntax error"):
        _check_output("", "Error parsing SQL: syntax error", 1)


def test_check_output_accepts_empty_output() -> None:
    _check_output("", "", 0)
    _check_output("", "warning: you are on an old version", 0)


def test_parse_output_rejects_non_json_output() -> None:
    with pytest.raises(LedgerError, match="unexpected output"):
        _parse_result_sets("not json at all")


def test_parse_output_rejects_unexpected_json_shape() -> None:
    with pytest.raises(LedgerError, match="unexpected output"):
        _parse_result_sets('{"status": "ok"}')
