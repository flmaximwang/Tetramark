"""One OR group across filters: ``--where-any`` / ``where_any=`` (Tetramark-d0x.2).

The shared filter backend (Tetramark-6ms.1, extended by Tetramark-d0x.1) gains
one OR group per invocation: the result is every ``--where`` filter AND at
least one ``--where-any`` filter.  The group uses the same syntax, operators,
coercion, type rules and empty-value rules; only the per-field repetition rules
of ``--where`` are dropped, because repeating a field is how an alternative is
written (``--where-any 'resistance=kan' --where-any 'resistance=amp'``).

Per kind, an alternative whose field the kind does not declare is dropped (it
is false for that kind), so a kind that declares none of the alternatives
contributes no rows; a kind that lacks a ``--where`` field contributes no rows
as before.  The surviving alternatives become one parenthesised OR condition
appended to the kind's AND conditions, still fetched in one batch.

Everything runs against the real ``dolt`` binary, like the rest of the suite.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from tetramark.cli.app import app
from tetramark.driver.embedded import EmbeddedDriver
from tetramark.errors import UsageError

from conftest import PRESETS
from support.read_asserts import assert_single_read_process

FULL = [str(PRESETS / "full")]


@pytest.fixture
def ledger(ledger_factory):
    """A full-preset ledger with every entity/stock/box kind available."""
    return ledger_factory(FULL, name="where-any")


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, dolt_env: dict[str, str]) -> None:
    """Run the CLI with the same isolated Dolt/Git environment as the ledger."""
    for key, value in dolt_env.items():
        monkeypatch.setenv(key, value)


def run_cli(ledger, *args: str):
    return CliRunner().invoke(app, ["--ledger", str(ledger.path), *args])


def ids(rows) -> list[str]:
    return [str(row["id"]) for row in rows]


# ---------------------------------------------------------------------------
# AND with the OR group, on all three listings
# ---------------------------------------------------------------------------


def test_the_or_group_ands_with_every_where_filter(ledger) -> None:
    alpha = ledger.entities.add("item", name="alpha", values={"category": "lab"})
    beta = ledger.entities.add("item", name="beta", values={"category": "lab"})
    gamma = ledger.entities.add("item", name="gamma", values={"category": "cold"})
    delta = ledger.entities.add("item", name="delta", values={"category": "cold"})

    # (category=lab) AND (name=alpha OR name=beta): both lab rows pass the OR.
    assert set(
        ids(
            ledger.entities.ls(
                where=["category=lab"],
                where_any=["name=alpha", "name=beta"],
            )
        )
    ) == {alpha["id"], beta["id"]}
    # Only gamma passes the OR, and the AND excludes it: nothing matches.
    assert (
        ledger.entities.ls(
            where=["category=lab"],
            where_any=["name=gamma", "name=delta"],
        )
        == []
    )
    # One alternative is enough; the second one does not have to match.
    assert set(
        ids(
            ledger.entities.ls(
                where=["category~^(lab|cold)$"],
                where_any=["name=alpha", "name=delta"],
            )
        )
    ) == {alpha["id"], delta["id"]}
    # With no --where-any the listing is exactly what it always was.
    assert set(ids(ledger.entities.ls(where=["category=lab"]))) == {
        alpha["id"],
        beta["id"],
    }
    assert set(
        ids(ledger.entities.ls(where=["category=lab"], where_any=[]))
    ) == {alpha["id"], beta["id"]}


def test_the_or_group_scopes_itself_per_kind_on_the_stock_list(ledger) -> None:
    """A stock kind only takes the alternatives it can express."""
    host = ledger.entities.add("item", name="host")
    # ``prep`` only accepts the ``everything`` entity kind.
    other_host = ledger.entities.add("everything", name="other-host")
    lot_acme = ledger.stocks.add(
        "lot", entity=host["id"], values={"supplier": "acme"}
    )
    ledger.stocks.add("lot", entity=host["id"], values={"supplier": "other"})
    prep_b1 = ledger.stocks.add(
        "prep", entity=other_host["id"], values={"batch": "B1"}
    )
    ledger.stocks.add("prep", entity=other_host["id"], values={"batch": "B2"})
    # ``bacterial`` declares neither ``supplier`` nor ``batch``.
    ledger.stocks.add(
        "bacterial", entity=host["id"], values={"strain": "BL21"}
    )

    rows = ledger.stocks.ls(where_any=["supplier=acme", "batch=B1"])

    assert ids(rows) == [lot_acme["id"], prep_b1["id"]]
    assert [row["kind"] for row in rows] == ["lot", "prep"]


def test_the_or_group_filters_the_box_list(ledger) -> None:
    shelf = ledger.boxes.add("shelf", "BOX-A")
    rack = ledger.boxes.add("rack", "RACK-A")
    grid = ledger.boxes.add("grid96", "BOX-B")

    # (name~^BOX) AND (kind=shelf OR kind=grid96): the rack is excluded.
    rows = ledger.boxes.ls(
        where=["name~^BOX"], where_any=["kind=shelf", "kind=grid96"]
    )

    assert ids(rows) == [grid["id"], shelf["id"]]
    # The AND group still narrows the OR alternatives.
    assert (
        ledger.boxes.ls(where=["name~^RACK"], where_any=["kind=shelf"])
        == []
    )
    # A core column is a valid alternative too.
    assert ids(ledger.boxes.ls(where_any=["name=RACK-A"])) == [rack["id"]]


# ---------------------------------------------------------------------------
# Per-kind scoping (acceptance c)
# ---------------------------------------------------------------------------


def test_a_kind_missing_one_alternative_still_matches_its_other(ledger) -> None:
    """``category`` is an item field, ``code`` an everything field."""
    lab = ledger.entities.add("item", name="alpha", values={"category": "lab"})
    everything = ledger.entities.add("everything", name="thing", values={"code": "ABC"})
    ledger.entities.add("item", name="beta", values={"category": "cold"})
    # ``plasmid`` declares neither field: it drops every alternative.
    ledger.entities.add("plasmid", name="pUC19")

    rows = ledger.entities.ls(where_any=["category=lab", "code=ABC"])

    assert ids(rows) == [everything["id"], lab["id"]]
    assert [row["kind"] for row in rows] == ["everything", "item"]


def test_a_kind_without_any_alternative_contributes_nothing(ledger) -> None:
    """An OR group with nothing left for a kind is false for that kind."""
    ledger.entities.add("item", name="alpha", values={"category": "lab"})
    everything = ledger.entities.add("everything", name="thing", values={"code": "ABC"})
    ledger.entities.add("plasmid", name="pUC19")

    # ``item`` (and ``plasmid``) have no ``code``: only ``everything`` takes
    # part, even though the item row would match no other filter.
    rows = ledger.entities.ls(where_any=["code=ABC"])

    assert ids(rows) == [everything["id"]]
    assert [row["kind"] for row in rows] == ["everything"]


def test_a_kind_missing_a_where_field_contributes_nothing(ledger) -> None:
    """The AND group's per-kind rule is unchanged by the OR group."""
    host = ledger.entities.add("item", name="host")
    bl21 = ledger.stocks.add(
        "bacterial", entity=host["id"], values={"strain": "BL21"}
    )
    ledger.stocks.add("bacterial", entity=host["id"], values={"strain": "DH5a"})
    ledger.stocks.add("lot", entity=host["id"], values={"supplier": "acme"})

    # ``strain`` is a bacterial field: the lot is dropped by the AND group,
    # even though the OR group has an alternative its kind could express.
    rows = ledger.stocks.ls(
        where=["strain=BL21"], where_any=["kind=bacterial", "kind=lot"]
    )

    assert ids(rows) == [bl21["id"]]
    assert [row["kind"] for row in rows] == ["bacterial"]


# ---------------------------------------------------------------------------
# The operators of Tetramark-d0x.1 inside the OR group
# ---------------------------------------------------------------------------


def test_the_or_group_uses_every_operator(ledger) -> None:
    five = ledger.entities.add("everything", name="five", values={"count": 5})
    ten = ledger.entities.add("everything", name="ten", values={"count": 10})
    twenty = ledger.entities.add("everything", name="twenty", values={"count": 20})

    # An ordered comparison is an alternative like any other.
    assert set(
        ids(ledger.entities.ls(where_any=["count<6", "count>=20"]))
    ) == {five["id"], twenty["id"]}
    # So is a negated pattern and a negated comparison.
    assert set(
        ids(ledger.entities.ls(where_any=["name!~^f", "count=20"]))
    ) == {ten["id"], twenty["id"]}
    assert set(
        ids(ledger.entities.ls(where_any=["name!=five", "name=twenty"]))
    ) == {ten["id"], twenty["id"]}
    # The AND group composes with an ordered alternative.
    assert ids(
        ledger.entities.ls(
            where=["count<15"], where_any=["count=5", "count=20"]
        )
    ) == [five["id"]]


# ---------------------------------------------------------------------------
# Errors name the option the filter came from
# ---------------------------------------------------------------------------


def test_an_unknown_field_names_the_where_any_option(ledger) -> None:
    ledger.entities.add("item", name="alpha")

    with pytest.raises(UsageError) as error:
        ledger.entities.ls(where_any=["bogus=1"])

    message = str(error.value)
    assert "unknown field(s) 'bogus' in --where-any" in message
    assert "valid fields" in message


def test_an_invalid_pattern_names_the_where_any_option(ledger) -> None:
    ledger.entities.add("item", name="alpha")

    with pytest.raises(UsageError) as error:
        ledger.entities.ls(where_any=["name~["])

    message = str(error.value)
    assert "invalid regular expression" in message
    assert "name~[" in message
    assert "--where-any" in message
    assert message.count("regular expression") == 1


def test_the_cli_reports_where_any_errors_as_usage_errors(
    cli_env: None, ledger
) -> None:
    ledger.entities.add("item", name="alpha")

    unknown = run_cli(ledger, "entity", "list", "--where-any", "bogus=1")
    assert isinstance(unknown.exception, UsageError)
    assert unknown.exception.exit_code == 2
    assert "--where-any" in str(unknown.exception)

    invalid = run_cli(ledger, "entity", "list", "--where-any", "name~[")
    assert isinstance(invalid.exception, UsageError)
    assert invalid.exception.exit_code == 2
    assert "invalid regular expression in the --where-any filter" in str(
        invalid.exception
    )

    # A valid --where-any listing prints exactly the matching rows.
    payload = run_cli(
        ledger, "entity", "list", "--where-any", "name~^alp", "--json"
    )
    assert payload.exit_code == 0, payload.output
    assert [row["name"] for row in json.loads(payload.output)] == ["alpha"]


# ---------------------------------------------------------------------------
# The Python API: list and mapping forms
# ---------------------------------------------------------------------------


def test_the_facade_takes_a_list_and_a_mapping(cli_env: None, ledger) -> None:
    alpha = ledger.entities.add("item", name="alpha", values={"category": "lab"})
    ledger.entities.add("item", name="beta", values={"category": "cold"})

    listed = ledger.entities.ls(where_any=["category=lab"])
    mapped = ledger.entities.ls(where_any={"category": "lab"})
    cli = json.loads(
        run_cli(
            ledger, "entity", "list", "--where-any", "category=lab", "--json"
        ).output
    )
    assert ids(listed) == ids(mapped) == ids(cli) == [alpha["id"]]
    # A mapping value ``None`` is the empty filter, exactly like ``--where``.
    assert ledger.entities.ls(where_any={"category": None}) == []


# ---------------------------------------------------------------------------
# The read budget
# ---------------------------------------------------------------------------


@pytest.mark.embedded_driver
def test_the_or_group_keeps_one_read_batch(
    ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The OR condition rides in the one batch every listing already builds."""
    alpha = ledger.entities.add("item", name="alpha", values={"category": "lab"})
    ledger.entities.add("item", name="beta", values={"category": "cold"})
    lot = ledger.stocks.add(
        "lot", entity=alpha["id"], values={"supplier": "acme"}
    )

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

    rows = ledger.entities.ls(
        where=["category=lab"], where_any=["name=alpha", "name=gamma"]
    )
    assert ids(rows) == [alpha["id"]]
    assert len(query_many_calls) == 1
    assert_single_read_process(invoke_calls)

    query_many_calls.clear()
    invoke_calls.clear()
    assert ids(ledger.stocks.ls(where_any=["supplier=acme", "batch=B1"])) == [
        lot["id"]
    ]
    assert len(query_many_calls) == 1
    assert_single_read_process(invoke_calls)
