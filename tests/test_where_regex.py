"""``--where`` regex filters: ``field~pattern`` on the entity/stock/box lists.

Tetramark-6ms.1.  A pattern is a search over the column's *text form*, run by
the database's own regex engine (Dolt's ICU dialect): it is unanchored,
case-sensitive (``(?i)`` makes it case-insensitive), it never matches ``NULL``
and it matches a ``ref`` field's stored id.  Every test runs against the real
``dolt`` binary, like the rest of the suite.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from support.read_asserts import assert_single_read_process
from tetramark.cli.app import app
from tetramark.driver.embedded import EmbeddedDriver
from tetramark.errors import UsageError

from conftest import PRESETS
from support.read_asserts import assert_single_read_process

BASE = PRESETS / "base"
FULL = PRESETS / "full"


@pytest.fixture
def base_ledger(ledger_factory):
    """A ledger with the small base preset (item, lot, three box kinds)."""
    return ledger_factory([BASE], name="where-base", id_prefix="77")


@pytest.fixture
def full_ledger(ledger_factory):
    """A ledger with the full preset (every field type)."""
    return ledger_factory([FULL], name="where-full", id_prefix="77")


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, dolt_env: dict[str, str]) -> None:
    """Run the CLI with the same isolated Dolt/Git environment as the ledger."""
    for key, value in dolt_env.items():
        monkeypatch.setenv(key, value)


def names(rows) -> list[str]:
    """The names of the listed rows."""
    return [str(row["name"]) for row in rows]


# ---------------------------------------------------------------------------
# The three listings
# ---------------------------------------------------------------------------


def test_a_pattern_filters_the_entity_list(base_ledger) -> None:
    ledger = base_ledger
    plasmid = ledger.entities.add("item", name="pUC19")
    ledger.entities.add("item", name="alpha")

    rows = ledger.entities.ls(where=["name~^pUC"])

    assert [row["id"] for row in rows] == [plasmid["id"]]
    # The kind narrows the listing as well.
    assert [
        row["id"] for row in ledger.entities.ls("item", where=["name~^pUC"])
    ] == [plasmid["id"]]
    # A pattern that matches nothing is an empty listing, not an error.
    assert ledger.entities.ls(where=["name~^zzz"]) == []


def test_a_pattern_filters_the_stock_list(base_ledger) -> None:
    ledger = base_ledger
    item = ledger.entities.add("item", name="alpha")
    lot = ledger.stocks.add("lot", entity=item["id"], values={"supplier": "Acme"})
    ledger.stocks.add("lot", entity=item["id"], values={"supplier": "other"})

    assert [row["id"] for row in ledger.stocks.ls(where=["supplier~^Ac"])] == [
        lot["id"]
    ]
    # A pattern is case-sensitive; ``(?i)`` makes it case-insensitive.
    assert ledger.stocks.ls(where=["supplier~^ac"]) == []
    assert [
        row["id"] for row in ledger.stocks.ls(where=["supplier~(?i)^ac"])
    ] == [lot["id"]]


def test_a_pattern_filters_the_box_list(base_ledger) -> None:
    ledger = base_ledger
    grid = ledger.boxes.add("grid96", "BOX-1")
    ledger.boxes.add("shelf", "SHELF-1")

    # The box rows are the core row joined with its kind row (spec 5), so a
    # pattern reaches the core columns of every box kind.
    assert [row["id"] for row in ledger.boxes.ls(where=["name~^BOX"])] == [
        grid["id"]
    ]
    assert [row["id"] for row in ledger.boxes.ls(where=["kind~^grid"])] == [
        grid["id"]
    ]
    assert [row["id"] for row in ledger.boxes.ls("grid96", where=["name~BOX"])] == [
        grid["id"]
    ]


# ---------------------------------------------------------------------------
# The text form of a column
# ---------------------------------------------------------------------------


def test_a_pattern_matches_the_text_form_of_a_column(full_ledger) -> None:
    ledger = full_ledger
    child = ledger.entities.add(
        "everything",
        name="child",
        values={
            "code": "BBB",
            "count": 7,
            "born": "2020-02-29",
            "seen_at": "2024-01-01 12:34:56",
        },
    )
    ledger.entities.add("everything", name="other", values={"code": "CCC"})

    # An int, a date and a datetime are matched as their text form.
    assert [
        row["id"] for row in ledger.entities.ls(where=["count~^7$"])
    ] == [child["id"]]
    assert [
        row["id"] for row in ledger.entities.ls(where=["born~2020-02-29"])
    ] == [child["id"]]
    assert [
        row["id"] for row in ledger.entities.ls(where=["seen_at~2024-01-01 12:34:56"])
    ] == [child["id"]]
    # A core column works like any other.
    assert len(ledger.entities.ls(where=["created_at~^\\d{4}-"])) == 2
    assert ledger.entities.ls(where=["created_at~^1999-"]) == []


def test_a_pattern_uses_the_database_regex_dialect(full_ledger) -> None:
    ledger = full_ledger
    child = ledger.entities.add(
        "everything", name="pUC19", values={"code": "BBB", "count": 7}
    )

    # A digit class, an inline case-insensitive flag, an anchor and an
    # unanchored search are the database's own regular expressions.
    assert [row["id"] for row in ledger.entities.ls(where=["count~\\d"])] == [
        child["id"]
    ]
    assert [
        row["id"] for row in ledger.entities.ls(where=["name~(?i)puc"])
    ] == [child["id"]]
    assert [row["id"] for row in ledger.entities.ls(where=["name~^pUC19$"])] == [
        child["id"]
    ]
    # An unanchored pattern is a search: it matches anywhere in the text.
    assert [row["id"] for row in ledger.entities.ls(where=["name~UC"])] == [
        child["id"]
    ]
    assert ledger.entities.ls(where=["name~^UC"]) == []


def test_null_never_matches(base_ledger) -> None:
    ledger = base_ledger
    described = ledger.entities.add(
        "item", name="alpha", values={"description": "hello"}
    )
    ledger.entities.add("item", name="beta")

    # The row without a description has NULL there, and NULL never matches.
    assert [
        row["id"] for row in ledger.entities.ls(where=["description~."])
    ] == [described["id"]]


def test_a_ref_field_matches_the_stored_id(full_ledger) -> None:
    ledger = full_ledger
    parent = ledger.entities.add("everything", name="parent", values={"code": "AAA"})
    child = ledger.entities.add(
        "everything", name="child", values={"code": "BBB", "parent": parent["id"]}
    )
    ledger.aliases.add("entity", parent["id"], "PARENT-ALIAS")

    # The ref column stores the id, so the id is what a pattern matches.
    assert [
        row["id"] for row in ledger.entities.ls(where=[f"parent~{parent['id']}"])
    ] == [child["id"]]
    # An alias is not what the column stores.
    assert ledger.entities.ls(where=["parent~PARENT-ALIAS"]) == []


# ---------------------------------------------------------------------------
# The rules of the syntax
# ---------------------------------------------------------------------------


def test_two_patterns_on_one_field_are_anded(base_ledger) -> None:
    ledger = base_ledger
    plasmid = ledger.entities.add("item", name="pUC19")
    ledger.entities.add("item", name="pUC18")
    ledger.entities.add("item", name="other")

    rows = ledger.entities.ls(where=["name~^pUC", "name~19$"])

    assert [row["id"] for row in rows] == [plasmid["id"]]


def test_a_field_cannot_be_filtered_twice_with_equals(base_ledger) -> None:
    ledger = base_ledger
    ledger.entities.add("item", name="alpha")

    with pytest.raises(UsageError, match="appears twice"):
        ledger.entities.ls(where=["name=alpha", "name=beta"])


def test_a_field_cannot_mix_the_two_operators(base_ledger) -> None:
    ledger = base_ledger
    ledger.entities.add("item", name="alpha")

    with pytest.raises(UsageError, match="both"):
        ledger.entities.ls(where=["name=alpha", "name~beta"])
    with pytest.raises(UsageError, match="both"):
        ledger.entities.ls(where=["name~beta", "name=alpha"])


def test_an_empty_pattern_is_a_usage_error(base_ledger) -> None:
    ledger = base_ledger
    ledger.entities.add("item", name="alpha")

    with pytest.raises(UsageError, match="must not be empty"):
        ledger.entities.ls(where=["name~"])


def test_an_invalid_pattern_is_a_usage_error_naming_the_filter(base_ledger) -> None:
    ledger = base_ledger
    ledger.entities.add("item", name="alpha")

    with pytest.raises(UsageError) as error:
        ledger.entities.ls(where=["name~["])

    message = str(error.value)
    # The usage error names the filter to fix, not the raw driver error, and
    # says it once.
    assert "invalid regular expression" in message
    assert "name~[" in message
    assert "--where" in message
    assert message.count("regular expression") == 1


def test_an_unknown_field_still_lists_the_valid_ones(base_ledger) -> None:
    ledger = base_ledger
    ledger.entities.add("item", name="alpha")

    with pytest.raises(UsageError, match="valid fields"):
        ledger.entities.ls(where=["bogus~x"])


def test_a_cross_kind_combination_still_validates_the_pattern(full_ledger) -> None:
    """No kind carries the filters, so the pattern must be checked on its own."""
    ledger = full_ledger
    ledger.entities.add("item", name="alpha", values={"category": "lab"})
    ledger.entities.add("everything", name="thing", values={"code": "ABC"})

    # ``category`` is an item field and ``code`` an everything field: no kind
    # can express both, so the listing visits no kind and is empty ...
    assert ledger.entities.ls(where=["category=lab", "code=ABC"]) == []
    assert ledger.entities.ls(where=["category~^l", "code~^A"]) == []
    # ... but an invalid pattern is still a usage error naming the filter.
    with pytest.raises(UsageError) as error:
        ledger.entities.ls(where=["category~[", "code~x"])
    assert "category~[" in str(error.value)


def test_a_cross_kind_stock_combination_still_validates_the_pattern(
    full_ledger,
) -> None:
    """The stock listing validates the pattern when no stock kind carries it."""
    ledger = full_ledger
    item = ledger.entities.add("item", name="alpha")
    everything = ledger.entities.add(
        "everything", name="thing", values={"code": "ABC"}
    )
    ledger.stocks.add("lot", entity=item["id"], values={"supplier": "Acme"})
    ledger.stocks.add("prep", entity=everything["id"], values={"batch": "B1"})

    # ``supplier`` is a lot field and ``batch`` a prep field.
    assert ledger.stocks.ls(where=["supplier=Acme", "batch=B1"]) == []
    assert ledger.stocks.ls(where=["supplier~^Ac", "batch~^B"]) == []
    with pytest.raises(UsageError) as error:
        ledger.stocks.ls(where=["supplier~[", "batch~x"])
    assert "supplier~[" in str(error.value)


def test_the_mapping_api_is_equality_only(base_ledger) -> None:
    ledger = base_ledger
    plasmid = ledger.entities.add("item", name="pUC19")
    ledger.entities.add("item", name="alpha")

    assert [row["id"] for row in ledger.entities.ls(where={"name": "pUC19"})] == [
        plasmid["id"]
    ]
    # A mapping never searches: the value is the value.
    assert ledger.entities.ls(where={"name": "pUC1"}) == []
    # And it cannot carry a pattern.
    with pytest.raises(UsageError):
        ledger.entities.ls(where={"name~x": "pUC19"})


# ---------------------------------------------------------------------------
# The CLI
# ---------------------------------------------------------------------------


def test_the_cli_filters_with_a_pattern(cli_env: None, base_ledger) -> None:
    ledger = base_ledger
    plasmid = ledger.entities.add("item", name="pUC19")
    ledger.entities.add("item", name="alpha")

    result = CliRunner().invoke(
        app,
        [
            "--ledger",
            str(ledger.path),
            "entity",
            "list",
            "--where",
            "name~^pUC",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert [row["id"] for row in json.loads(result.output)] == [plasmid["id"]]


def test_the_cli_reports_an_invalid_pattern_as_a_usage_error(
    cli_env: None, base_ledger
) -> None:
    ledger = base_ledger
    ledger.entities.add("item", name="alpha")

    result = CliRunner().invoke(
        app,
        ["--ledger", str(ledger.path), "entity", "list", "--where", "name~["],
    )

    # The usage error reaches the caller with the exit code of a usage error
    # (main() turns it into exit code 2).
    assert isinstance(result.exception, UsageError)
    assert result.exception.exit_code == 2
    assert "invalid regular expression" in str(result.exception)


# ---------------------------------------------------------------------------
# The read budget
# ---------------------------------------------------------------------------


@pytest.mark.embedded_driver
def test_a_pattern_keeps_one_read_batch(
    base_ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pattern costs the listing the same single batch as an equality."""
    ledger = base_ledger
    ledger.entities.add("item", name="pUC19")

    calls: list[str] = []
    original = EmbeddedDriver._invoke

    def counting(self: EmbeddedDriver, sql: str, result_format: str) -> str:
        calls.append(result_format)
        return original(self, sql, result_format)

    monkeypatch.setattr(EmbeddedDriver, "_invoke", counting)

    rows = ledger.entities.ls(where=["name~^pUC"])

    assert names(rows) == ["pUC19"]
    # One JSON batch for the rows, one CSV pass for the column names.
    assert_single_read_process(calls)
