"""``tm aliquot list`` on the shared filter backend (Tetramark-d0x.3).

The aliquot listing used to parse ``--where`` itself (exact ``FIELD=VALUE``
only).  It now rides the shared :mod:`tetramark.core.filters` backend, so the
same eight operators and the one ``--where-any`` OR group serve it too:

* ``=`` and ``!=`` still bind the raw text (no input accepted before is
  rejected), and an empty value keeps the aliquot meaning: ``NULL`` or ``''``
  on a text column, ``NULL`` only on ``seq``/``quantity``/``created_at``;
* an ordered comparison is typed: ``seq`` compares numerically, ``quantity``
  as a decimal, ``created_at`` as an ISO date/datetime and every other column
  in the database's collation order;
* per-field repetition follows the shared rules (a second ``=`` is an error,
  the other operators repeat), while ``--where-any`` may repeat freely;
* ``status`` stays refused in either group with the ``--status`` hint, and the
  scope, record shape, ordering and one batched read are unchanged.

Everything runs against the real ``dolt`` binary, like the rest of the suite.
The rows are tuned so a text comparison would give a different answer than the
typed one (``seq`` 2 vs 10, ``quantity`` 9 vs 10 vs 1.5) and the string
collation is pinned (``''`` first, uppercase before lowercase).
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from typer.testing import CliRunner

from tetramark.cli.app import app
from tetramark.driver.embedded import EmbeddedDriver
from tetramark.errors import UsageError

from conftest import PRESETS
from support.read_asserts import assert_single_read_process

BASE = PRESETS / "base"

#: Every key of one listed aliquot record (the same shape ``aliquot show`` has
#: minus ``events`` and plus ``object``).
RECORD_KEYS = {
    "id",
    "stock_id",
    "seq",
    "status",
    "quantity",
    "unit",
    "label",
    "notes",
    "created_at",
    "aliases",
    "stock_kind",
    "entity_id",
    "box_id",
    "slot",
    "path",
    "in_stock",
    "object",
}


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, dolt_env: dict[str, str]) -> None:
    """Run the CLI with the same isolated Dolt/Git environment as the ledger."""
    for key, value in dolt_env.items():
        monkeypatch.setenv(key, value)


def build_ledger(ledger_factory) -> dict[str, Any]:
    """A ledger whose aliquot rows pin both the typed and the text order.

    Two ``lot`` stocks, one with six stored tubes (a box and a slot each) and
    one with four pending tubes (unplaced).  The updates set the labels, units,
    quantities, sequences and creation timestamps the assertions read; the last
    placed tube keeps an empty (``''``) ``box_id``/``slot`` so the text-column
    empty rule has a row of each shape.
    """
    ledger = ledger_factory([BASE], name="aliquot-where-operators", id_prefix="77")
    item = ledger.entities.add("item", name="alpha")
    ledger.boxes.add("grid96", "BOX")
    placed = ledger.stocks.add(
        "lot",
        entity=item["id"],
        aliquots=6,
        box="BOX",
        slots=("A1", "A2", "A3", "A4", "A5", "A6"),
        quantity=5,
        unit="ml",
    )
    pending = ledger.stocks.add("lot", entity=item["id"], aliquots=4)
    stock_one, stock_two = placed["id"], pending["id"]
    one = list(placed["aliquots"])
    two = list(pending["aliquots"])

    rows: dict[str, str] = {
        "alpha": one[0],
        "beta": one[1],
        "gamma": one[2],
        "null_label": one[3],
        "empty": one[4],
        "none": one[5],
        "Alpha": two[0],
        "gamma2": two[1],
        "spaces": two[2],
        "delta": two[3],
    }
    # (seq, label, quantity, unit, created_at, notes); NULL means "unset".
    values: list[tuple[Any, ...]] = [
        ("alpha", 1, "alpha", "10", "ml", "2026-01-05 00:00:00", "n1"),
        ("beta", 2, "beta", "9", "ml", "2026-02-01 12:00:00", None),
        ("gamma", 10, "gamma", "1.5", "ml", "2025-12-31 23:59:59", None),
        ("null_label", 3, None, None, None, "2026-03-01 00:00:00", None),
        ("empty", 4, "", "0", "ml", "2026-04-01 00:00:00", None),
        ("none", 5, "none", "2.50", "ml", "2026-05-01 00:00:00", None),
        ("Alpha", 2, "Alpha", "100", "ul", "2024-01-01 00:00:00", "n2"),
        ("gamma2", 7, "gamma", "3", "ul", "2026-06-01 00:00:00", None),
        ("spaces", 8, "  ", "4", "ul", "2026-07-01 00:00:00", None),
        ("delta", 9, "delta", "5", "ul", "2023-06-01 00:00:00", None),
    ]
    # ``(stock_id, seq)`` is unique: park every row on its own temporary
    # sequence first, so the final values never collide with a row the loop has
    # not rewritten yet.
    for offset, key in enumerate(rows):
        ledger.driver.execute(
            "UPDATE `aliquot` SET `seq` = %s WHERE `id` = %s",
            (100 + offset, rows[key]),
        )
    for key, seq, label, quantity, unit, created_at, notes in values:
        ledger.driver.execute(
            "UPDATE `aliquot` SET `seq` = %s, `label` = %s, `quantity` = %s, "
            "`unit` = %s, `created_at` = %s, `notes` = %s WHERE `id` = %s",
            (seq, label, quantity, unit, created_at, notes, rows[key]),
        )
    # The text-column empty rule has a row of each shape: '' and NULL.
    ledger.driver.execute(
        "UPDATE `aliquot` SET `box_id` = '', `slot` = NULL WHERE `id` = %s",
        (rows["empty"],),
    )
    return {
        "ledger": ledger,
        "ids": rows,
        "stock_one": stock_one,
        "stock_two": stock_two,
        "population": set(rows.values()),
    }


def listed(ledger, stock: str | None = None, **kwargs: Any) -> set[str]:
    """The ids the facade lists (every status unless the caller narrows it)."""
    kwargs.setdefault("all_statuses", True)
    return {
        str(row["id"]) for row in ledger.aliquots.ls(stock, **kwargs)
    }


def filtered(ledger, *filters: str, **kwargs: Any) -> set[str]:
    """The ids of ``where=[...]``; every status by default."""
    return listed(ledger, where=list(filters), **kwargs)


def assert_complement(positive: set[str], negative: set[str], all_ids: set[str]) -> None:
    """Exactly one of the two filters matches every row of the population."""
    assert positive.isdisjoint(negative)
    assert positive | negative == all_ids


def run_cli(ledger, *args: str):
    return CliRunner().invoke(app, ["--ledger", str(ledger.path), *args])


def cli_ids(ledger, *args: str) -> list[str]:
    result = run_cli(ledger, *args, "--json")
    assert result.exit_code == 0, result.output
    return [row["id"] for row in json.loads(result.output)]


# ---------------------------------------------------------------------------
# The eight operators on the text columns (and the NULL complement rule)
# ---------------------------------------------------------------------------


def test_text_operators_and_their_complements(ledger_factory) -> None:
    """=, !=, ~, !~ and the ordered comparisons on label/unit/box/slot/notes."""
    data = build_ledger(ledger_factory)
    ledger, rows, all_ids = data["ledger"], data["ids"], data["population"]

    # ``=`` is exact and case-sensitive; ``!=`` is its complement (NULL too).
    assert filtered(ledger, "label=gamma") == {rows["gamma"], rows["gamma2"]}
    assert_complement(
        filtered(ledger, "label=gamma"),
        filtered(ledger, "label!=gamma"),
        all_ids,
    )
    assert rows["null_label"] in filtered(ledger, "label!=gamma")
    assert filtered(ledger, "label=") == {rows["empty"], rows["null_label"]}
    assert_complement(
        filtered(ledger, "label="), filtered(ledger, "label!="), all_ids
    )

    # A pattern matches the column text; NULL never matches either side of it.
    assert filtered(ledger, "label~^a") == {rows["alpha"]}
    assert rows["Alpha"] not in filtered(ledger, "label~^a")
    assert_complement(
        filtered(ledger, "label~^gamma$"),
        filtered(ledger, "label!~^gamma$"),
        all_ids,
    )
    assert rows["null_label"] not in filtered(ledger, "label~.*")
    assert rows["null_label"] in filtered(ledger, "label!~.*")

    # Text columns compare in the database's collation order: '' first, then
    # the spaces, then uppercase, then lowercase.  NULL never matches.
    assert filtered(ledger, "label<beta") == {
        rows["empty"],
        rows["spaces"],
        rows["Alpha"],
        rows["alpha"],
    }
    assert filtered(ledger, "label>=beta") == {
        rows["beta"],
        rows["delta"],
        rows["gamma"],
        rows["gamma2"],
        rows["none"],
    }
    assert filtered(ledger, "unit=ml") == {
        rows["alpha"],
        rows["beta"],
        rows["gamma"],
        rows["empty"],
        rows["none"],
    }
    assert filtered(ledger, "notes<n2") == {rows["alpha"]}
    # The value after the operator is kept verbatim: two spaces are two spaces.
    assert filtered(ledger, "label=  ") == {rows["spaces"]}

    # An empty value keeps the aliquot rule: '' or NULL on a text column, so the
    # four unplaced tubes (NULL box/slot) match too.
    unplaced = {rows["Alpha"], rows["gamma2"], rows["spaces"], rows["delta"]}
    assert filtered(ledger, "box_id=") == {rows["empty"]} | unplaced
    assert filtered(ledger, "slot=") == {rows["empty"]} | unplaced
    assert filtered(ledger, "box_id!=") == all_ids - ({rows["empty"]} | unplaced)

    # The allowlist's identity columns filter through the same operators, and a
    # numeric column still accepts a pattern on its text form.
    assert filtered(ledger, f"id={rows['beta']}") == {rows["beta"]}
    assert filtered(ledger, f"stock_id={data['stock_one']}") == {
        rows["alpha"],
        rows["beta"],
        rows["gamma"],
        rows["null_label"],
        rows["empty"],
        rows["none"],
    }
    assert filtered(ledger, "seq~^1") == {rows["alpha"], rows["gamma"]}
    assert filtered(ledger, "quantity~^2.") == {rows["none"]}


# ---------------------------------------------------------------------------
# The typed ordered comparisons (seq, quantity, created_at)
# ---------------------------------------------------------------------------


def test_ordered_comparisons_are_typed(ledger_factory) -> None:
    """``seq``/``quantity`` compare numerically and ``created_at`` in time order."""
    data = build_ledger(ledger_factory)
    ledger, rows = data["ledger"], data["ids"]

    # Numeric, not text: 10 is greater than 2 although "10" sorts before "2".
    assert filtered(ledger, "seq>2") == {
        rows["gamma"],
        rows["null_label"],
        rows["empty"],
        rows["none"],
        rows["gamma2"],
        rows["spaces"],
        rows["delta"],
    }
    assert filtered(ledger, "seq<2") == {rows["alpha"]}
    assert filtered(ledger, "seq>=2") == data["population"] - {rows["alpha"]}
    # ``=`` still binds the raw text, so "2" matches both rows stored as 2.
    assert filtered(ledger, "seq=2") == {rows["beta"], rows["Alpha"]}

    # A decimal compares numerically too (1.5 < 9 even though "1.5" > "9").
    assert filtered(ledger, "quantity<10") == {
        rows["beta"],
        rows["gamma"],
        rows["empty"],
        rows["none"],
        rows["gamma2"],
        rows["spaces"],
        rows["delta"],
    }
    assert filtered(ledger, "quantity<=9") == filtered(ledger, "quantity<10")
    assert filtered(ledger, "quantity>=9") == {
        rows["alpha"],
        rows["beta"],
        rows["Alpha"],
    }
    assert filtered(ledger, "quantity>1.5") == {
        rows["alpha"],
        rows["beta"],
        rows["none"],
        rows["Alpha"],
        rows["gamma2"],
        rows["spaces"],
        rows["delta"],
    }
    # ``=`` compares the raw text: a stored 0 matches "0", not "0.0".
    assert filtered(ledger, "quantity=0") == {rows["empty"]}
    # ``quantity=`` keeps the NULL-only rule (a stored 0 never looks empty).
    assert filtered(ledger, "quantity=") == {rows["null_label"]}

    # created_at is a datetime: a date means its midnight.
    assert filtered(ledger, "created_at>=2026-01-01") == {
        rows["alpha"],
        rows["beta"],
        rows["null_label"],
        rows["empty"],
        rows["none"],
        rows["gamma2"],
        rows["spaces"],
    }
    # The date means its midnight, so 2025-12-31 23:59:59 is NOT <= 2025-12-31.
    assert filtered(ledger, "created_at<=2025-12-31") == {
        rows["Alpha"],
        rows["delta"],
    }
    assert filtered(ledger, "created_at>=2025-12-31") == data["population"] - {
        rows["Alpha"],
        rows["delta"],
    }
    assert filtered(ledger, "created_at>=2026-02-01T00:00:00") == {
        rows["beta"],
        rows["null_label"],
        rows["empty"],
        rows["none"],
        rows["gamma2"],
        rows["spaces"],
    }
    assert filtered(ledger, "created_at>2023-06-01") == data["population"] - {
        rows["delta"]
    }

    # A non-numeric ordered value is a usage error naming the filter and option;
    # the non-finite decimal spellings are not numbers either.
    for text, message in [
        ("seq>abc", r"invalid --where 'seq>abc': the value 'abc' is not a number"),
        ("quantity>x", r"invalid --where 'quantity>x': the value 'x' is not a number"),
        (
            "quantity>NaN",
            r"invalid --where 'quantity>NaN': the value 'NaN' is not a number",
        ),
        (
            "quantity>Infinity",
            r"invalid --where 'quantity>Infinity': the value 'Infinity' is not a "
            "number",
        ),
    ]:
        with pytest.raises(UsageError, match=message):
            filtered(ledger, text)
    # Inside the OR group the message names that option and the same filter.
    with pytest.raises(UsageError, match=r"invalid --where-any 'quantity>x'"):
        listed(ledger, where_any=["quantity>x"])
    # A bad ISO value keeps the shared core-datetime message.
    with pytest.raises(
        UsageError,
        match=r"invalid --where 'created_at>yesterday': the value 'yesterday' is "
        "not an ISO date or datetime",
    ):
        filtered(ledger, "created_at>yesterday")


# ---------------------------------------------------------------------------
# AND with the OR group, and the unchanged scopes
# ---------------------------------------------------------------------------


def test_the_or_group_ands_with_the_where_filters(ledger_factory) -> None:
    """``--where`` AND (one ``--where-any`` alternative) across the scopes."""
    data = build_ledger(ledger_factory)
    ledger, rows = data["ledger"], data["ids"]

    # The OR group alone: the default in-stock scope still applies.
    assert listed(ledger, all_statuses=False, where_any=["label=gamma"]) == {
        rows["gamma"]
    }
    assert listed(
        ledger, all_statuses=True, where_any=["label=gamma"]
    ) == {rows["gamma"], rows["gamma2"]}
    assert listed(
        ledger,
        all_statuses=False,
        in_stock=False,
        where_any=["label=gamma"],
    ) == {rows["gamma2"]}
    assert listed(
        ledger, status="pending", where_any=["label=gamma"]
    ) == {rows["gamma2"]}

    # AND: one group narrows the other.
    assert listed(
        ledger,
        where=["unit=ml"],
        where_any=["label=alpha", "label=beta"],
    ) == {rows["alpha"], rows["beta"]}
    assert listed(
        ledger, box="BOX", where_any=["label=alpha", "label=delta"]
    ) == {rows["alpha"]}
    assert listed(
        ledger, kind="lot", where=["unit=ml"]
    ) == {
        rows["alpha"],
        rows["beta"],
        rows["gamma"],
        rows["empty"],
        rows["none"],
    }
    assert listed(
        ledger, data["stock_one"], where_any=["label=delta"]
    ) == set()
    assert listed(
        ledger, data["stock_two"], where_any=["label=delta"]
    ) == {rows["delta"]}

    # The non-``=`` operators may repeat one field inside --where (ANDed).
    assert filtered(ledger, "quantity>=2", "quantity<=9") == {
        rows["beta"],
        rows["none"],
        rows["gamma2"],
        rows["spaces"],
        rows["delta"],
    }
    # Repeating a field inside --where-any is how alternatives are written.
    assert listed(
        ledger, where_any=["label=alpha", "label=delta"]
    ) == {rows["alpha"], rows["delta"]}
    # No alternative matches: an empty list, not an error.
    assert listed(ledger, where_any=["label=zzz"]) == set()

    # Mixing ``=`` with another operator on one field keeps the shared error.
    with pytest.raises(UsageError, match=r"cannot be filtered with both '=' and '~'"):
        filtered(ledger, "label=alpha", "label~^a")


# ---------------------------------------------------------------------------
# Both API forms and the CLI answer the same
# ---------------------------------------------------------------------------


def test_the_api_and_the_cli_agree(ledger_factory, cli_env: None) -> None:
    """List and Mapping forms, ``--where-any`` and the ``-w`` short flag."""
    data = build_ledger(ledger_factory)
    ledger, rows = data["ledger"], data["ids"]

    where = ["quantity>=2"]
    where_any = ["label~^a", "label=delta"]
    expected = {rows["alpha"], rows["delta"]}
    assert listed(ledger, where=where, where_any=where_any) == expected

    cli = cli_ids(
        ledger,
        "aliquot",
        "list",
        "--all",
        "--where",
        "quantity>=2",
        "--where-any",
        "label~^a",
        "--where-any",
        "label=delta",
    )
    assert set(cli) == expected
    assert cli_ids(ledger, "aliquot", "list", "--all", "-w", "label=beta") == [
        rows["beta"]
    ]

    # A mapping is equality only, and ``None``/``""`` mean "is empty".
    assert listed(ledger, where={"label": "gamma"}) == {
        rows["gamma"],
        rows["gamma2"],
    }
    assert listed(ledger, where={"unit": None}) == {rows["null_label"]}
    assert listed(ledger, where={"unit": ""}) == {rows["null_label"]}
    # The text "a~b" is a value, not an operator.
    assert listed(ledger, where={"label": "a~b"}) == set()
    # where_any= takes the same two forms.
    assert listed(ledger, where_any=["label=gamma"]) == {
        rows["gamma"],
        rows["gamma2"],
    }
    assert listed(ledger, where_any={"label": "gamma"}) == {
        rows["gamma"],
        rows["gamma2"],
    }


# ---------------------------------------------------------------------------
# Refusals and their option naming
# ---------------------------------------------------------------------------


def test_status_is_refused_in_either_group_with_every_operator(ledger_factory) -> None:
    """``status`` stays the ``--status`` filter, whatever the operator."""
    data = build_ledger(ledger_factory)
    ledger = data["ledger"]

    for text in ("status=", "status!=stored", "status~^s", "status>a"):
        for kwargs in ({"where": [text]}, {"where_any": [text]}):
            with pytest.raises(
                UsageError, match="filter the status with --status"
            ):
                listed(ledger, **kwargs)


def test_errors_name_the_right_option_and_filter(ledger_factory) -> None:
    """Unknown/malformed/invalid names and patterns are usage errors (exit 2)."""
    data = build_ledger(ledger_factory)
    ledger = data["ledger"]

    with pytest.raises(
        UsageError, match=r"unknown field\(s\) 'nope' in --where"
    ):
        filtered(ledger, "nope=1")
    with pytest.raises(
        UsageError, match=r"unknown field\(s\) 'nope' in --where-any"
    ):
        listed(ledger, where_any=["nope=1"])

    for text in ("label", "label!x", "=x", " label!x"):
        with pytest.raises(UsageError, match="expected field=value"):
            filtered(ledger, text)

    # A list filter's field is an identifier by construction, so a non-identifier
    # name reaches the parser only through a mapping key (the shared rule).
    with pytest.raises(UsageError, match=r"'bad-name' is not a valid field name"):
        listed(ledger, where={"bad-name": "x"})
    with pytest.raises(UsageError, match=r"not a valid field name"):
        listed(ledger, where_any={"bad-name": "x"})

    # An invalid pattern is a usage error naming the group, never a raw driver
    # error (the database compiles the pattern, so the query fails first).
    with pytest.raises(
        UsageError,
        match=r"invalid regular expression in the --where filter 'label~\['",
    ):
        filtered(ledger, "label~[")
    with pytest.raises(
        UsageError,
        match=r"invalid regular expression in the --where-any filter 'label~\['",
    ):
        listed(ledger, where_any=["label~["])


# ---------------------------------------------------------------------------
# The unchanged read: shape, order, one batch
# ---------------------------------------------------------------------------


@pytest.mark.embedded_driver
def test_the_record_shape_order_and_the_one_read_batch(
    ledger_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every operator and the OR group ride the one batched driver read."""
    data = build_ledger(ledger_factory)
    ledger = data["ledger"]

    query_many_calls: list[list[tuple[str, object]]] = []
    invoke_calls: list[str] = []
    original_query_many = EmbeddedDriver.query_many
    original_invoke = EmbeddedDriver._invoke

    def counted_query_many(
        self: EmbeddedDriver, statements: list[tuple[str, object]]
    ) -> list[list[dict[str, object]]]:
        query_many_calls.append(list(statements))
        return original_query_many(self, statements)

    def counted_invoke(self: EmbeddedDriver, sql: str, result_format: str) -> str:
        invoke_calls.append(result_format)
        return original_invoke(self, sql, result_format)

    monkeypatch.setattr(EmbeddedDriver, "query_many", counted_query_many)
    monkeypatch.setattr(EmbeddedDriver, "_invoke", counted_invoke)

    records = ledger.aliquots.ls(
        where=["quantity>=1.5", "label!~^zzz"],
        where_any=["label~^a", "label=delta"],
        all_statuses=True,
    )
    assert {row["id"] for row in records} == {
        data["ids"]["alpha"],
        data["ids"]["delta"],
    }
    assert len(query_many_calls) == 1
    assert len(query_many_calls[0]) == 2  # the rows and their aliases
    assert_single_read_process(invoke_calls)

    # The record shape is the list shape: no events, sorted aliases, derived
    # in_stock, the same keys as tm aliquot show.
    every = ledger.aliquots.ls(all_statuses=True)
    for record in every:
        assert set(record) == RECORD_KEYS
        assert isinstance(record["aliases"], list)
        assert record["object"] == "aliquot"
    assert all(row["in_stock"] is True for row in every if row["status"] == "stored")
    assert all(row["in_stock"] is False for row in every if row["status"] == "pending")
    # Rows are ordered by stock_id, then numeric seq, then id.
    assert every == sorted(every, key=lambda row: (row["stock_id"], row["seq"], row["id"]))
