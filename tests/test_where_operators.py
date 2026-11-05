"""``--where`` operators: ``!=``, ``!~`` and the ordered comparisons.

Tetramark-d0x.1.  The shared filter backend also accepts ``field!=value``,
``field!~pattern`` and the ordered comparisons ``field<value`` / ``<=`` / ``>``
/ ``>=``.  Three-valued logic makes the negated operators the complements of
their positive ones:

* ``field!=value`` also matches ``NULL`` rows, so for every row of a kind that
  declares the field exactly one of ``=`` and ``!=`` matches;
* ``field!~pattern`` also matches ``NULL`` rows, for the same reason;
* ``field!=`` (an empty value, "the field has a value") is the complement of
  ``field=`` ("the field is empty"): it excludes the ``NULL`` and the ``''``
  rows, never coerces a value and never resolves an alias.

An ordered comparison is typed: an int or a decimal compares numerically, a
date or a datetime chronologically, and a string in the database's collation
order (the order ``=`` uses, pinned here as a case-sensitive one: uppercase
before lowercase, ``''`` first).  A bool, an enum, a ref, a sequence and a computed
field have no order, so an ordered comparison on one is a usage error, as is a
non-ISO value for the core ``created_at`` column.  A computed field still
accepts ``!~`` (like ``~``) and still refuses ``!=`` (like ``=``).

Everything runs against the real ``dolt`` binary, like the rest of the suite.
The ``everything`` kind of the full preset carries one field of every type
(``legacy`` is its string field without a pattern, so it can hold ``''``).
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from tetramark.cli.app import app
from tetramark.core import objects as objects_module
from tetramark.core.ledger import Ledger
from tetramark.errors import UsageError

from conftest import PRESETS

FULL = PRESETS / "full"


@pytest.fixture
def full_ledger(ledger_factory):
    """A ledger with the full preset (every field type)."""
    return ledger_factory([FULL], name="where-operators", id_prefix="77")


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, dolt_env: dict[str, str]) -> None:
    """Run the CLI with the same isolated Dolt/Git environment as the ledger."""
    for key, value in dolt_env.items():
        monkeypatch.setenv(key, value)


def ids(rows) -> set[str]:
    """The ids of the listed rows."""
    return {str(row["id"]) for row in rows}


def ls(ledger: Ledger, *filters: str) -> set[str]:
    """The ids of the entities matching the filters."""
    return ids(ledger.entities.ls(where=list(filters)))


def assert_complement(
    positive: set[str], negative: set[str], population: set[str]
) -> None:
    """Exactly one of the two filters matches every row of the population."""
    assert positive.isdisjoint(negative)
    assert positive | negative == population


def build_rows(ledger: Ledger) -> dict[str, str]:
    """Create the entity rows the operator tests read; return name -> id.

    The rows cover a valued field, a ``NULL`` field and an empty string on the
    string columns (``legacy`` on the kind, ``notes`` on the core row), a
    ``NULL`` and a set ref, and the values the ordered comparisons sort.  The
    ``item`` kind does not declare the kind fields, so it is on neither side of
    their filters.
    """
    alpha = ledger.entities.add(
        "everything",
        name="alpha",
        values={
            "code": "AAA",
            "blurb": "hello",
            "count": 1,
            "ratio": "1.500",
            "born": "2020-01-02",
            "seen_at": "2024-01-01 12:00:00",
            "legacy": "L1",
            "seq": "MKT1",
        },
    )
    beta = ledger.entities.add("everything", name="beta")
    gamma = ledger.entities.add(
        "everything",
        name="gamma",
        values={
            "code": "BBB",
            "blurb": "",
            "count": 0,
            "ratio": "0.500",
            "born": "2021-06-01",
            "seen_at": "2023-01-01 00:00:00",
            "legacy": "",
            "notes": "",
        },
    )
    delta = ledger.entities.add(
        "everything",
        name="delta",
        values={
            "code": "CCC",
            "blurb": "zzz",
            "count": 10,
            "ratio": "10.250",
            "born": "2019-12-31",
            "seen_at": "2025-05-06 07:08:09",
            "legacy": "L2",
            "notes": "note",
        },
    )
    child = ledger.entities.add(
        "everything",
        name="child",
        values={"code": "DDD", "count": 2, "parent": alpha["id"]},
    )
    # The rows that pin the string order: the database's binary collation puts
    # uppercase before lowercase and '' before everything.
    banana = ledger.entities.add(
        "everything", name="Banana", values={"code": "BNN", "legacy": "Banana"}
    )
    apple = ledger.entities.add(
        "everything", name="apple", values={"code": "APL", "legacy": "apple"}
    )
    cherry = ledger.entities.add(
        "everything", name="cherry", values={"code": "CHR", "legacy": "cherry"}
    )
    item = ledger.entities.add("item", name="item-row", values={"category": "lab"})
    created = {
        "alpha": alpha,
        "beta": beta,
        "gamma": gamma,
        "delta": delta,
        "child": child,
        "banana": banana,
        "apple": apple,
        "cherry": cherry,
        "item": item,
    }
    return {name: str(row["id"]) for name, row in created.items()}


def run_cli(ledger: Ledger, *args: str):
    """Invoke the CLI against the ledger."""
    return CliRunner().invoke(app, ["--ledger", str(ledger.path), *args])


def cli_json(ledger: Ledger, *args: str):
    """Invoke the CLI with ``--json`` and return the parsed payload."""
    result = run_cli(ledger, *args, "--json")
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


# ---------------------------------------------------------------------------
# Complementarity: != vs =, !~ vs ~, != (empty) vs = (empty)
# ---------------------------------------------------------------------------


def test_not_equals_is_the_complement_of_equals(full_ledger) -> None:
    """Every row matches exactly one of ``=`` and ``!=``, NULL rows included."""
    ledger = full_ledger
    rows = build_rows(ledger)
    population = ids(ledger.entities.ls("everything"))
    all_population = ids(ledger.entities.ls())

    # A kind string field, a kind ref field and a core text column.
    cases = [
        ("legacy", "L1", {rows["alpha"]}, population),
        ("parent", rows["alpha"], {rows["child"]}, population),
        ("notes", "note", {rows["delta"]}, all_population),
    ]
    for field, value, expected, rows_of_kind in cases:
        positive = ls(ledger, f"{field}={value}")
        negative = ls(ledger, f"{field}!={value}")
        assert positive == expected, field
        assert_complement(positive, negative, rows_of_kind)

    # The complement reaches the NULL and the '' rows of the string field.
    legacy_negative = ls(ledger, "legacy!=L1")
    assert rows["beta"] in legacy_negative  # NULL
    assert rows["gamma"] in legacy_negative  # ''
    # A ref column is NULL on every row but the child's.
    assert rows["beta"] in ls(ledger, f"parent!={rows['alpha']}")
    # A kind that does not declare the field is on neither side of it.
    for field, value in (("legacy", "L1"), ("parent", rows["alpha"])):
        assert rows["item"] not in ls(ledger, f"{field}={value}")
        assert rows["item"] not in ls(ledger, f"{field}!={value}")


def test_not_regexp_is_the_complement_of_regexp(full_ledger) -> None:
    """Every row matches exactly one of ``~`` and ``!~``, NULL rows included."""
    ledger = full_ledger
    rows = build_rows(ledger)
    population = ids(ledger.entities.ls("everything"))
    all_population = ids(ledger.entities.ls())

    cases = [
        ("legacy", "^L", {rows["alpha"], rows["delta"]}, population),
        ("parent", rows["alpha"], {rows["child"]}, population),
        ("notes", "^note", {rows["delta"]}, all_population),
        # A computed field accepts a pattern and its negation like a column.
        ("display", ".", population - {rows["beta"]}, population),
    ]
    for field, pattern, expected, rows_of_kind in cases:
        positive = ls(ledger, f"{field}~{pattern}")
        negative = ls(ledger, f"{field}!~{pattern}")
        assert positive == expected, field
        assert_complement(positive, negative, rows_of_kind)

    # A NULL never matches a pattern, not even ``^$``: it is on the negative
    # side even when the pattern matches the stored empty string.
    assert ls(ledger, "legacy~^$") == {rows["gamma"]}
    assert rows["beta"] in ls(ledger, "legacy!~^$")
    assert_complement(
        ls(ledger, "legacy~^$"), ls(ledger, "legacy!~^$"), population
    )


def test_an_empty_not_equals_is_the_complement_of_an_empty_equals(
    full_ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``field!=`` matches only the rows that have a value: not NULL, not ''."""
    ledger = full_ledger
    rows = build_rows(ledger)
    population = ids(ledger.entities.ls("everything"))
    all_population = ids(ledger.entities.ls())

    # The string field has two NULL rows (beta and child) and an '' row
    # (gamma); the ref and the core column cover NULL and a value too.
    cases = [
        ("legacy", {rows["beta"], rows["gamma"], rows["child"]}, population),
        ("parent", population - {rows["child"]}, population),
        ("notes", all_population - {rows["delta"]}, all_population),
    ]
    for field, expected_empty, rows_of_kind in cases:
        empty = ls(ledger, f"{field}=")
        valued = ls(ledger, f"{field}!=")
        assert empty == expected_empty, field
        assert_complement(empty, valued, rows_of_kind)

    # "Has a value" excludes the NULL row and the '' row explicitly.
    valued_legacy = ls(ledger, "legacy!=")
    assert valued_legacy == {
        rows["alpha"],
        rows["delta"],
        rows["banana"],
        rows["apple"],
        rows["cherry"],
    }
    assert rows["beta"] not in valued_legacy
    assert rows["gamma"] not in valued_legacy

    # A non-text field is empty only when it is NULL; a stored 0 is a value.
    assert ls(ledger, "count=") == set()
    assert ls(ledger, "count!=") == population

    # The empty negated filter never coerces and never resolves an alias: the
    # ref is compared as it is stored, so no alias lookup happens.
    calls: list[tuple] = []

    def no_resolve(*args: object) -> str:
        calls.append(args)
        raise AssertionError("an empty ref filter must not be resolved")

    monkeypatch.setattr(objects_module, "_resolve_ref", no_resolve)
    assert ls(ledger, "parent!=") == {rows["child"]}
    assert calls == []


def test_not_equals_resolves_an_alias_like_equals(full_ledger) -> None:
    """``parent!=alias`` compares the id the alias resolves to, like ``=``."""
    ledger = full_ledger
    rows = build_rows(ledger)
    population = ids(ledger.entities.ls("everything"))
    ledger.aliases.add("entity", rows["alpha"], "ALIAS-A")

    assert ls(ledger, "parent=ALIAS-A") == {rows["child"]}
    assert_complement(
        ls(ledger, "parent=ALIAS-A"),
        ls(ledger, "parent!=ALIAS-A"),
        population,
    )
    # A pattern is a search over the text form: the ref column stores the id,
    # so the alias is not what ``~`` or ``!~`` see.
    assert ls(ledger, "parent~ALIAS-A") == set()
    assert ls(ledger, "parent!~ALIAS-A") == population


# ---------------------------------------------------------------------------
# Ordered comparisons
# ---------------------------------------------------------------------------


def test_ordered_comparisons_are_typed(full_ledger) -> None:
    """Ints and decimals compare numerically, dates and datetimes in time order."""
    ledger = full_ledger
    rows = build_rows(ledger)
    population = ids(ledger.entities.ls("everything"))

    # An int compares numerically: 10 is greater than 2 although the text
    # "10" sorts before "2".  The rows with the default 0 stay out.
    assert ls(ledger, "count>2") == {rows["delta"]}
    assert ls(ledger, "count>=2") == {rows["delta"], rows["child"]}
    assert ls(ledger, "count<2") == {
        rows["alpha"],
        rows["beta"],
        rows["gamma"],
        rows["banana"],
        rows["apple"],
        rows["cherry"],
    }
    assert ls(ledger, "count<0") == set()

    # A decimal compares numerically too: 10.250 is greater than 2.
    assert ls(ledger, "ratio>2") == {rows["delta"]}
    assert ls(ledger, "ratio<1") == {rows["gamma"]}
    assert ls(ledger, "ratio>=1.500") == {rows["alpha"], rows["delta"]}

    # A text field uses the same order as a string one.
    assert ls(ledger, "blurb>hello") == {rows["delta"]}
    assert ls(ledger, "blurb<=hello") == {rows["alpha"], rows["gamma"]}

    # A date compares chronologically.  ``born`` has a schema default
    # (2024-01-31), so the rows that do not set it explicitly sort after 2020.
    assert ls(ledger, "born>=2020-01-01") == population - {rows["delta"]}
    assert ls(ledger, "born<2020-01-01") == {rows["delta"]}
    assert ls(ledger, "born<=2020-01-02") == {rows["alpha"], rows["delta"]}
    assert ls(ledger, "born>=2030-01-01") == set()

    # A datetime compares chronologically as well.
    assert ls(ledger, "seen_at>=2024-01-01") == {rows["alpha"], rows["delta"]}
    assert ls(ledger, "seen_at<2024-01-01") == {rows["gamma"]}
    assert ls(ledger, "seen_at>2024-01-01 12:00:00") == {rows["delta"]}

    # The core created_at compares chronologically and takes an ISO date or
    # datetime; every row of the ledger was created after 2000.
    all_population = ids(ledger.entities.ls())
    assert ls(ledger, "created_at>=2000-01-01") == all_population
    assert ls(ledger, "created_at<2000-01-01") == set()
    assert ls(ledger, "created_at>2000-01-01T00:00:00") == all_population


def test_a_string_compares_in_the_database_collation_order(full_ledger) -> None:
    """Strings use the order ``=`` uses: case-sensitive, upper before lower."""
    ledger = full_ledger
    rows = build_rows(ledger)
    all_population = ids(ledger.entities.ls())

    # A kind string field: '' sorts first, then uppercase, then lowercase
    # (a case-insensitive collation would order ``apple`` before ``Banana``).
    assert ls(ledger, "legacy>=apple") == {rows["apple"], rows["cherry"]}
    assert ls(ledger, "legacy<apple") == {
        rows["gamma"],
        rows["banana"],
        rows["alpha"],
        rows["delta"],
    }
    assert ls(ledger, "legacy<=Banana") == {rows["gamma"], rows["banana"]}
    assert ls(ledger, "legacy>Banana") == {
        rows["alpha"],
        rows["delta"],
        rows["apple"],
        rows["cherry"],
    }

    # A core text column uses the same collation: "Banana" (B) sorts before
    # every lowercase name, and ``=`` is case-sensitive.
    assert ls(ledger, "name<alpha") == {rows["banana"]}
    assert ls(ledger, "name>=alpha") == all_population - {rows["banana"]}
    assert ls(ledger, "name=Banana") == {rows["banana"]}
    assert ls(ledger, "name=banana") == set()


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("filter_text", "message"),
    [
        (
            "active<1",
            "invalid --where 'active<1': the field 'active' is of type bool, "
            "which has no order",
        ),
        (
            "color>red",
            "invalid --where 'color>red': the field 'color' is of type enum, "
            "which has no order",
        ),
        (
            "parent>x",
            "invalid --where 'parent>x': the field 'parent' is of type ref, "
            "which has no order",
        ),
        (
            "seq<MKT2",
            "invalid --where 'seq<MKT2': the field 'seq' is of type sequence, "
            "which has no order",
        ),
        (
            "display>a",
            "invalid --where 'display>a': the field 'display' is computed and "
            "cannot be compared",
        ),
        (
            "created_at>yesterday",
            "invalid --where 'created_at>yesterday': the value 'yesterday' is "
            "not an ISO date or datetime",
        ),
    ],
)
def test_an_ordered_comparison_refuses_a_field_without_an_order(
    cli_env: None, full_ledger, filter_text: str, message: str
) -> None:
    """A bool/enum/ref/sequence/computed field has no order, nor does a
    non-ISO created_at value: both the facade and the CLI refuse it."""
    ledger = full_ledger
    build_rows(ledger)

    with pytest.raises(UsageError) as error:
        ledger.entities.ls(where=[filter_text])
    assert str(error.value) == message

    result = run_cli(ledger, "entity", "list", "--where", filter_text)
    assert isinstance(result.exception, UsageError)
    assert result.exception.exit_code == 2
    assert str(result.exception) == message


def test_an_ordered_comparison_needs_a_value(cli_env: None, full_ledger) -> None:
    """``count<`` has no value to compare with: a usage error, exit code 2."""
    ledger = full_ledger
    build_rows(ledger)

    for filter_text in ("count<", "count<=", "ratio>", "born>="):
        with pytest.raises(UsageError, match="must not be empty"):
            ledger.entities.ls(where=[filter_text])

    result = run_cli(ledger, "entity", "list", "--where", "count<")
    assert isinstance(result.exception, UsageError)
    assert result.exception.exit_code == 2
    assert "must not be empty" in str(result.exception)


def test_a_computed_field_still_refuses_not_equals_and_accepts_not_regexp(
    cli_env: None, full_ledger
) -> None:
    """``!=`` is refused like ``=``; ``!~`` is accepted like ``~``."""
    ledger = full_ledger
    rows = build_rows(ledger)

    with pytest.raises(UsageError, match="field 'display' is computed"):
        ledger.entities.ls(where=["display!="])

    result = run_cli(ledger, "entity", "list", "--where", "display!=")
    assert isinstance(result.exception, UsageError)
    assert result.exception.exit_code == 2
    assert "is computed and cannot be set" in str(result.exception)

    # The computed field of the row without a code is NULL, so ``!~`` keeps it.
    assert ls(ledger, "display!~.") == {rows["beta"]}


def test_an_unknown_field_is_still_a_usage_error(full_ledger) -> None:
    ledger = full_ledger
    build_rows(ledger)

    with pytest.raises(
        UsageError, match=r"unknown field\(s\) 'bogus' in --where; valid fields: "
    ):
        ledger.entities.ls(where=["bogus>=1"])


# ---------------------------------------------------------------------------
# Patterns of !~
# ---------------------------------------------------------------------------


def test_an_invalid_negated_pattern_names_the_filter(
    cli_env: None, full_ledger
) -> None:
    """``!~`` reports an invalid pattern exactly like ``~`` does."""
    ledger = full_ledger
    build_rows(ledger)
    expected = "invalid regular expression in the --where filter 'name!~['"

    # A kind query carries the filter, so the database compiles the pattern.
    with pytest.raises(UsageError) as error:
        ledger.entities.ls(where=["name!~["])
    assert str(error.value) == expected

    result = run_cli(ledger, "entity", "list", "--where", "name!~[")
    assert isinstance(result.exception, UsageError)
    assert result.exception.exit_code == 2
    assert str(result.exception) == expected


def test_an_invalid_negated_pattern_is_checked_without_a_kind(
    full_ledger,
) -> None:
    """No kind expresses the combination, so the no-kind path validates it."""
    ledger = full_ledger
    build_rows(ledger)

    # ``category`` is an item field and ``code`` an everything field, so no
    # kind can carry both and the listing visits no kind: the pattern must
    # still be checked instead of silently returning no rows.
    assert ls(ledger, "category=lab", "code=AAA") == set()
    with pytest.raises(UsageError) as error:
        ledger.entities.ls(where=["category!~[", "code=AAA"])
    assert (
        str(error.value)
        == "invalid regular expression in the --where filter 'category!~['"
    )


# ---------------------------------------------------------------------------
# The three listings, through the facade and the CLI
# ---------------------------------------------------------------------------


def test_the_three_listings_filter_with_the_new_operators(
    cli_env: None, full_ledger
) -> None:
    """``!=``, ``!~`` and an ordered comparison on entity, stock and box."""
    ledger = full_ledger
    rows = build_rows(ledger)
    population = ids(ledger.entities.ls("everything"))
    # ``prep`` only accepts ``everything`` entities as its host.
    host = rows["alpha"]
    first = ledger.stocks.add("prep", entity=host, values={"batch": "B1"})
    second = ledger.stocks.add("prep", entity=host, values={"batch": "B2"})
    empty = ledger.stocks.add("prep", entity=host, values={"batch": ""})
    null = ledger.stocks.add("prep", entity=host)
    box_a = ledger.boxes.add("shelf", "BOX-A")
    box_b = ledger.boxes.add("shelf", "BOX-B", values={"notes": "note"})
    box_c = ledger.boxes.add("shelf", "BOX-C", values={"notes": ""})

    # The entity list: kind string field, ordered int, computed regex.
    assert ids(ledger.entities.ls(where=["legacy!=L1"])) == population - {
        rows["alpha"]
    }
    assert ids(ledger.entities.ls(where=["count>2"])) == {rows["delta"]}
    assert ids(ledger.entities.ls(where=["display!~^a"])) == population - {
        rows["alpha"],
        rows["apple"],
    }
    assert ids(cli_json(ledger, "entity", "list", "--where", "legacy!=L1")) == (
        population - {rows["alpha"]}
    )
    assert ids(cli_json(ledger, "entity", "list", "--where", "count>2")) == {
        rows["delta"]
    }

    # The stock list: a kind string field with a value, '' and NULL rows.
    assert ids(ledger.stocks.ls(where=["batch>B1"])) == {second["id"]}
    assert ids(ledger.stocks.ls(where=["batch!=B1"])) == {
        second["id"],
        empty["id"],
        null["id"],
    }
    assert ids(ledger.stocks.ls(where=["batch!~^B"])) == {
        empty["id"],
        null["id"],
    }
    assert ids(ledger.stocks.ls(where=["batch="])) == {
        empty["id"],
        null["id"],
    }
    assert ids(ledger.stocks.ls(where=["batch!="])) == {
        first["id"],
        second["id"],
    }
    assert ids(cli_json(ledger, "stock", "list", "--where", "batch>B1")) == {
        second["id"]
    }

    # The box list: core text columns.
    assert ids(ledger.boxes.ls(where=["name<BOX-B"])) == {box_a["id"]}
    assert ids(ledger.boxes.ls(where=["name!~^BOX-A$"])) == {
        box_b["id"],
        box_c["id"],
    }
    assert ids(ledger.boxes.ls(where=["notes="])) == {box_a["id"], box_c["id"]}
    assert ids(ledger.boxes.ls(where=["notes!="])) == {box_b["id"]}
    assert ids(cli_json(ledger, "box", "list", "--where", "name<BOX-B")) == {
        box_a["id"]
    }
