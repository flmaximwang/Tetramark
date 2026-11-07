"""Unit tests for the embedded driver's routed-integer normaliser.

These are the Dolt-free half of Tetramark-2gz: a live ``dolt sql-server`` makes
Dolt route ``dolt sql`` through it and its JSON result sets render integer
columns as strings.  The driver restores ``int`` from the same-process
``SHOW COLUMNS`` ``Type``, and this file pins the two halves of that rule with
synthetic JSON-shaped data:

* the type-string parser accepts the whole SQL integer family (display width,
  ``unsigned``) and rejects every other type string, and
* only a string of ASCII signed digits in an integer-typed column is converted;
  ``None``/absent, booleans, floats and numeric-looking text stay exactly as
  they are.

The real routed/local comparison is ``tests/test_driver_routed_parity.py``.
"""

from __future__ import annotations

import pytest

from tetramark.driver.embedded import (
    _INTEGER_TEXT,
    _is_integer_type,
    _normalise_integers,
)

#: Every integer type string Dolt's ``SHOW COLUMNS`` emits, plus the display
#: width/unsigned spellings the parser must accept.
INTEGER_TYPES = [
    "tinyint",
    "tinyint(1)",
    "tinyint(3) unsigned",
    "smallint",
    "smallint(5) unsigned",
    "mediumint",
    "mediumint(9) unsigned",
    "int",
    "int(11)",
    "int unsigned",
    "int(10) unsigned",
    "integer",
    "bigint",
    "bigint(20)",
    "bigint unsigned",
    "bigint(20) unsigned",
]

#: Every non-integer type string the probe can print; a numeric-looking value
#: in one of these columns must never be converted.
NON_INTEGER_TYPES = [
    "decimal(10,2)",
    "decimal",
    "float",
    "double",
    "bit(8)",
    "year",
    "date",
    "time",
    "datetime",
    "timestamp",
    "json",
    "varchar(10)",
    "char(4)",
    "text",
    "mediumtext",
    "enum('1','2')",
    "set('1','2')",
    "blob",
    "",
    "int8",  # not a MySQL/Tetramark type spelling
    " bigint",  # whitespace is never trimmed into a match
    None,
    1,
]


@pytest.mark.fast
@pytest.mark.parametrize("sql_type", INTEGER_TYPES)
def test_integer_type_parser_accepts_the_integer_family(sql_type: str) -> None:
    assert _is_integer_type(sql_type) is True


@pytest.mark.fast
@pytest.mark.parametrize("sql_type", NON_INTEGER_TYPES)
def test_integer_type_parser_rejects_everything_else(sql_type: object) -> None:
    assert _is_integer_type(sql_type) is False


@pytest.mark.fast
@pytest.mark.parametrize("value", ["0", "-1", "1", "-42", "007", "18446744073709551615"])
def test_signed_digit_strings_convert_to_int(value: str) -> None:
    rows = [{"v": value}]
    _normalise_integers(rows, [("v", "bigint unsigned")])
    assert rows == [{"v": int(value)}]
    assert type(rows[0]["v"]) is int


@pytest.mark.fast
@pytest.mark.parametrize(
    "value",
    [
        "+1",  # the settled rule accepts no explicit plus sign
        " 1",  # whitespace is not trimmed
        "1 ",  # whitespace is not trimmed
        "1.0",
        "1e3",
        "0x10",
        "--1",
        "-",  # a lone sign is not an integer
        "",
        "١٢٣",  # Arabic-Indic digits are not ASCII
        "１２３",  # full-width digits are not ASCII
        "NaN",
    ],
)
def test_non_integer_looking_strings_are_never_converted(value: str) -> None:
    rows = [{"v": value}]
    _normalise_integers(rows, [("v", "int")])
    assert rows == [{"v": value}]


@pytest.mark.fast
def test_integer_regex_is_full_match_only() -> None:
    assert _INTEGER_TEXT.match("-12") is not None
    for value in ("12-", "1 2", "+12", "1.2"):
        assert _INTEGER_TEXT.match(value) is None


@pytest.mark.fast
def test_non_integer_columns_keep_numeric_looking_strings() -> None:
    rows = [{"dec": "1.50", "txt": "123", "vc": "007", "json": "1"}]
    _normalise_integers(
        rows,
        [
            ("dec", "decimal(10,2)"),
            ("txt", "text"),
            ("vc", "varchar(10)"),
            ("json", "json"),
        ],
    )
    assert rows == [{"dec": "1.50", "txt": "123", "vc": "007", "json": "1"}]


@pytest.mark.fast
def test_boolean_none_float_and_int_values_are_untouched() -> None:
    rows = [{"flag": True, "missing": None, "ratio": 1.5, "count": 7, "big": 2**63}]
    _normalise_integers(
        rows,
        [
            ("flag", "tinyint(1)"),
            ("missing", "int"),
            ("ratio", "double"),
            ("count", "bigint"),
            ("big", "bigint unsigned"),
        ],
    )
    assert rows[0]["flag"] is True  # a bool is never rewritten to 1
    assert rows[0]["missing"] is None
    assert rows[0]["ratio"] == 1.5 and type(rows[0]["ratio"]) is float
    assert rows[0]["count"] == 7 and type(rows[0]["count"]) is int
    assert rows[0]["big"] == 2**63


@pytest.mark.fast
def test_absent_null_column_is_not_created_by_normalisation() -> None:
    rows: list[dict[str, object]] = [{}]
    _normalise_integers(rows, [("id", "int")])
    assert rows == [{}]


@pytest.mark.fast
def test_a_row_with_no_integer_columns_is_untouched() -> None:
    rows = [{"name": "1", "note": "2"}]
    _normalise_integers(rows, [("name", "varchar(10)"), ("note", "text")])
    assert rows == [{"name": "1", "note": "2"}]
