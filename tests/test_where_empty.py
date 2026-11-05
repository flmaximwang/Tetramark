"""``--where FIELD=`` selects empty fields (Tetramark-0ez.1).

An exact filter whose value is exactly the empty string means "the field is
empty": ``field=`` matches ``NULL`` or ``''`` for a text-like column and only
``NULL`` for a non-text one (so a stored ``0`` never looks empty).  The same
backend serves ``tm entity|stock|box list``, ``tm find --where`` and the Python
facades, and the Python mapping form spells the empty value ``None`` or ``""``
(``None`` used to become the text ``"None"`` -- that is the bug this file also
pins).

Everything runs against the real ``dolt`` binary, like the rest of the suite.
The ledger is built once per module and the tests only read it.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
import yaml
from typer.testing import CliRunner

from conftest import FACTORY_AUTHOR, isolated_env
from tetramark.cli.app import app
from tetramark.core import objects as objects_module
from tetramark.core.ledger import Ledger, init_ledger
from tetramark.errors import UsageError

#: One self-contained preset: every nullable type AC c/d needs, a kind ref, a
#: second entity kind (the "kind without the field" of AC j), a stock and a
#: slotless box kind.
PRESET = {
    "preset": "where-empty",
    "version": 6,
    "mixins": {
        "named_fields": {
            "name": {"type": "string", "required": True, "unique": True},
        },
    },
    "kinds": {
        "item": {
            "object": "entity",
            "mixins": ["named_fields"],
            "fields": {
                "code": {"type": "string"},
                "choice": {"type": "enum", "values": ["alpha", "beta"]},
                "count": {"type": "int"},
                "amount": {"type": "decimal", "precision": 10, "scale": 2},
                "born": {"type": "date"},
                "seen_at": {"type": "datetime"},
                "label": {"type": "string"},
                "parent": {"type": "ref", "to": "item"},
                "display": {
                    "type": "computed",
                    "expr": "CONCAT(name, '-', code)",
                    "sql_type": "string",
                },
            },
        },
        "other": {
            "object": "entity",
            "mixins": ["named_fields"],
            "fields": {"tag": {"type": "string"}},
        },
        "tube": {
            "object": "stock",
            "entity_kinds": ["item"],
            "fields": {
                "purification": {"type": "ref", "to": "item"},
                "supplier": {"type": "string"},
                "count": {"type": "int"},
                "amount": {"type": "decimal", "precision": 10, "scale": 2},
            },
        },
        "shelf": {
            "object": "box",
            "geometry": {"type": "slotless", "capacity": 4},
            "fields": {"code": {"type": "string"}},
        },
    },
}


@pytest.fixture(scope="module")
def where_env(
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, str]:
    """The isolated Dolt environment the module's ledger and CLI share."""
    root = tmp_path_factory.mktemp("where-empty-env")
    return {
        **isolated_env(root / "dolt-root", root / "home"),
        # Module fixtures are set up before the per-test backend fixture runs;
        # pin the backend so the ledger and the CLI always agree on it.
        "TETRAMARK_DRIVER": "server",
    }


@pytest.fixture(scope="module")
def where_ledger(
    tmp_path_factory: pytest.TempPathFactory,
    dolt_binary: str,
    where_env: dict[str, str],
) -> Iterator[tuple[Ledger, dict[str, str]]]:
    """One real ledger with the fixture rows, built once per worker."""
    del dolt_binary  # the fixture only skips the module when dolt is missing
    root = tmp_path_factory.mktemp("where-empty")
    preset_path = root / "preset.yaml"
    preset_path.write_text(
        yaml.safe_dump(PRESET, sort_keys=False), encoding="utf-8"
    )
    ledger = init_ledger(
        root / "ledger",
        "where-empty",
        [preset_path],
        id_prefix="77",
        author=FACTORY_AUTHOR,
        env=where_env,
    )
    rows = build_rows(ledger)
    try:
        yield ledger, rows
    finally:
        ledger.close()


@pytest.fixture
def cli_env(
    monkeypatch: pytest.MonkeyPatch, where_env: dict[str, str]
) -> None:
    """Run the CLI in the same isolated Dolt environment as the ledger."""
    for key, value in where_env.items():
        monkeypatch.setenv(key, value)


def build_rows(ledger: Ledger) -> dict[str, str]:
    """Create the rows the acceptance criteria read (the tests never write)."""
    alpha = ledger.entities.add(
        "item",
        name="alpha",
        values={
            "code": "X1",
            "choice": "alpha",
            "count": 0,
            "amount": 0,
            "born": "2020-01-02",
            "seen_at": "2024-01-01 12:00:00",
            "label": "none",
        },
    )
    beta = ledger.entities.add("item", name="beta")
    gamma = ledger.entities.add(
        "item", name="gamma", values={"code": "", "label": "", "notes": ""}
    )
    delta = ledger.entities.add(
        "item", name="delta", values={"code": " ", "label": "  "}
    )
    holder = ledger.entities.add(
        "item", name="holder", values={"code": "P", "label": "null"}
    )
    child = ledger.entities.add(
        "item", name="child", values={"code": "C", "parent": holder["id"]}
    )
    foreign = ledger.entities.add("other", name="foreign", values={"tag": "v"})

    tube_full = ledger.stocks.add(
        "tube",
        entity=alpha["id"],
        values={
            "purification": holder["id"],
            "supplier": "Acme",
            "count": 0,
            "amount": 0,
        },
    )
    tube_empty = ledger.stocks.add(
        "tube", entity=beta["id"], values={"supplier": ""}
    )
    tube_child = ledger.stocks.add(
        "tube", entity=alpha["id"], parent=tube_full["id"]
    )

    box_top = ledger.boxes.add("shelf", "SHELF-TOP", values={"code": "X"})
    box_empty = ledger.boxes.add("shelf", "SHELF-EMPTY", values={"code": ""})
    box_null = ledger.boxes.add("shelf", "SHELF-NULL")
    box_child = ledger.boxes.add(
        "shelf", "SHELF-CHILD", parent=box_top["id"], values={"code": "Y"}
    )

    rows = {
        "alpha": alpha["id"],
        "beta": beta["id"],
        "gamma": gamma["id"],
        "delta": delta["id"],
        "holder": holder["id"],
        "child": child["id"],
        "foreign": foreign["id"],
        "tube_full": tube_full["id"],
        "tube_empty": tube_empty["id"],
        "tube_child": tube_child["id"],
        "box_top": box_top["id"],
        "box_empty": box_empty["id"],
        "box_null": box_null["id"],
        "box_child": box_child["id"],
    }
    return {name: str(value) for name, value in rows.items()}


def ids(rows) -> set[str]:
    """The ids of the listed rows."""
    return {str(row["id"]) for row in rows}


def run_cli(ledger: Ledger, *args: str):
    return CliRunner().invoke(app, ["--ledger", str(ledger.path), *args])


def cli_json(ledger: Ledger, *args: str):
    result = run_cli(ledger, *args, "--json")
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


# ---------------------------------------------------------------------------
# AC a: the three listings, kind fields and core columns
# ---------------------------------------------------------------------------


def test_entity_list_empty_kind_field_matches_null_and_empty(
    cli_env: None, where_ledger
) -> None:
    ledger, rows = where_ledger

    # ``code`` is NULL on beta and '' on gamma; the valued and whitespace rows
    # stay out, and the ``other`` kind (which has no ``code``) contributes none.
    expected = {rows["beta"], rows["gamma"]}
    assert ids(ledger.entities.ls(where=["code="])) == expected
    assert set(ids(cli_json(ledger, "entity", "list", "--where", "code="))) == (
        expected
    )

    # A core text column behaves the same: ``notes`` is '' on gamma and NULL
    # on every other entity row.
    assert ids(ledger.entities.ls(where=["notes="])) == {
        rows["alpha"],
        rows["beta"],
        rows["gamma"],
        rows["delta"],
        rows["holder"],
        rows["child"],
        rows["foreign"],
    }


def test_stock_list_empty_kind_ref_and_core_column(
    cli_env: None, where_ledger
) -> None:
    ledger, rows = where_ledger

    # Every tube here was never moved into a box, so every stock is depleted:
    # the empty-filter assertions read the wider scope (the default stock
    # listing is active-only) exactly as before.
    # A kind ref field: NULL on the empty and child tubes, set on the full one.
    expected = {rows["tube_empty"], rows["tube_child"]}
    assert ids(ledger.stocks.ls(where=["purification="], all_statuses=True)) == expected
    assert set(
        ids(
            cli_json(
                ledger, "stock", "list", "--all", "--where", "purification="
            )
        )
    ) == expected

    # A core column: ``parent_stock_id`` is set only on the child tube.
    expected_parent = {rows["tube_full"], rows["tube_empty"]}
    assert (
        ids(ledger.stocks.ls(where=["parent_stock_id="], all_statuses=True))
        == expected_parent
    )
    assert set(
        ids(
            cli_json(
                ledger,
                "stock",
                "list",
                "--all",
                "--where",
                "parent_stock_id=",
            )
        )
    ) == expected_parent

    # A string kind field matches both NULL and ''.
    assert ids(ledger.stocks.ls(where=["supplier="], all_statuses=True)) == {
        rows["tube_empty"],
        rows["tube_child"],
    }


def test_box_list_empty_kind_field_and_core_column(
    cli_env: None, where_ledger
) -> None:
    ledger, rows = where_ledger

    expected = {rows["box_empty"], rows["box_null"]}
    assert ids(ledger.boxes.ls(where=["code="])) == expected
    assert set(ids(cli_json(ledger, "box", "list", "--where", "code="))) == expected

    # ``parent_box_id`` is set only on the child box.
    expected_parent = {rows["box_top"], rows["box_empty"], rows["box_null"]}
    assert ids(ledger.boxes.ls(where=["parent_box_id="])) == expected_parent
    assert set(
        ids(cli_json(ledger, "box", "list", "--where", "parent_box_id="))
    ) == expected_parent


# ---------------------------------------------------------------------------
# AC c and d: no coercion for an empty value, no zero-looking empty field
# ---------------------------------------------------------------------------


def test_an_empty_value_on_any_kind_type_raises_no_coercion_error(
    where_ledger,
) -> None:
    ledger, rows = where_ledger

    # ref, enum, int, decimal, date and datetime: an empty value is the empty
    # filter, not a value to coerce (a ref would otherwise resolve "" to an
    # alias and an int would say it must be an integer).
    for filter_text in (
        "parent=",
        "choice=",
        "count=",
        "amount=",
        "born=",
        "seen_at=",
    ):
        ledger.entities.ls(where=[filter_text])

    # A ref is text-like, but a stored id is never '', so only NULL matches.
    assert ids(ledger.entities.ls(where=["parent="])) == {
        rows["alpha"],
        rows["beta"],
        rows["gamma"],
        rows["delta"],
        rows["holder"],
    }


def test_an_empty_ref_does_no_alias_lookup(
    where_ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, _ = where_ledger
    calls: list[tuple] = []

    def no_alias_lookup(*args: object) -> str:
        calls.append(args)
        raise AssertionError("an empty ref must not be resolved")

    monkeypatch.setattr(objects_module, "_resolve_ref", no_alias_lookup)

    assert ledger.entities.ls(where=["parent="])
    # The ref is non-text: a stored id never equals '', so it matches NULL only.
    assert calls == []


def test_empty_int_and_decimal_never_match_zero(where_ledger) -> None:
    """AC d (mandatory): ``f=`` matches NULL and never a stored ``0``."""
    ledger, rows = where_ledger

    int_rows = ledger.entities.ls(where=["count="])
    assert rows["alpha"] not in ids(int_rows)  # alpha stores 0
    assert rows["beta"] in ids(int_rows)  # beta stores NULL
    assert ids(ledger.entities.ls(where=["count=0"])) == {rows["alpha"]}

    decimal_rows = ledger.entities.ls(where=["amount="])
    assert rows["alpha"] not in ids(decimal_rows)  # alpha stores 0
    assert rows["beta"] in ids(decimal_rows)
    assert ids(ledger.entities.ls(where=["amount=0"])) == {rows["alpha"]}

    # The same on the stock kind's int/decimal fields.  The tubes are depleted,
    # so the wider scope is what lists them (the default stock scope is
    # active-only); the NULL-vs-zero rule is unchanged.
    assert rows["tube_full"] not in ids(
        ledger.stocks.ls(where=["count="], all_statuses=True)
    )
    assert rows["tube_empty"] in ids(
        ledger.stocks.ls(where=["count="], all_statuses=True)
    )
    assert rows["tube_full"] not in ids(
        ledger.stocks.ls(where=["amount="], all_statuses=True)
    )
    assert rows["tube_empty"] in ids(
        ledger.stocks.ls(where=["amount="], all_statuses=True)
    )


# ---------------------------------------------------------------------------
# AC e: the computed-field usage error still fires
# ---------------------------------------------------------------------------


def test_an_empty_filter_on_a_computed_field_is_a_usage_error(
    cli_env: None, where_ledger
) -> None:
    ledger, _ = where_ledger

    with pytest.raises(UsageError, match="field 'display' is computed"):
        ledger.entities.ls(where=["display="])

    for args in (
        ("entity", "list", "--where", "display="),
        ("find", "--where", "display="),
    ):
        result = run_cli(ledger, *args)
        assert isinstance(result.exception, UsageError)
        assert result.exception.exit_code == 2
        assert "is computed and cannot be set" in str(result.exception)


# ---------------------------------------------------------------------------
# AC f, g, h: what an empty value does NOT change
# ---------------------------------------------------------------------------


def test_none_and_null_literals_stay_exact(cli_env: None, where_ledger) -> None:
    ledger, rows = where_ledger

    assert ids(ledger.entities.ls(where=["label=none"])) == {rows["alpha"]}
    assert ids(ledger.entities.ls(where=["label=null"])) == {rows["holder"]}
    assert set(
        ids(cli_json(ledger, "entity", "list", "--where", "label=none"))
    ) == {rows["alpha"]}


def test_a_whitespace_only_value_stays_an_exact_match(
    cli_env: None, where_ledger
) -> None:
    ledger, rows = where_ledger

    # Only the row that stores whitespace matches ``label=  ``; NULL rows must
    # never match, which is exactly what the empty branch would have done.
    assert ids(ledger.entities.ls(where=["label=  "])) == {rows["delta"]}
    assert set(
        ids(cli_json(ledger, "entity", "list", "--where", "label=  "))
    ) == {rows["delta"]}
    # The same for a one-space value: only delta stores a space.
    assert ids(ledger.entities.ls(where=["code= "])) == {rows["delta"]}


def test_a_dollar_anchor_pattern_still_never_matches_null(
    cli_env: None, where_ledger
) -> None:
    ledger, rows = where_ledger

    # ``^$`` matches gamma's stored '', never beta's NULL.
    assert ids(ledger.entities.ls(where=["code~^$"])) == {rows["gamma"]}
    assert set(ids(cli_json(ledger, "entity", "list", "--where", "code~^$"))) == {
        rows["gamma"]
    }


# ---------------------------------------------------------------------------
# AC b: tm find --where
# ---------------------------------------------------------------------------


def test_find_empty_filter_lists_and_narrows_hits(
    cli_env: None, where_ledger
) -> None:
    ledger, rows = where_ledger

    # ``find`` covers every object type: the item entities and the shelf boxes
    # carry a ``code``, and both kinds contribute their NULL/'' rows.
    expected = {
        rows["beta"],
        rows["gamma"],
        rows["box_empty"],
        rows["box_null"],
    }
    hits = cli_json(ledger, "find", "--where", "code=")
    assert set(ids(hits)) == expected
    assert {hit["match"] for hit in hits} == {"filter"}

    # A QUERY narrows the hits: "ta" matches beta and delta by name, and the
    # empty filter keeps only beta.
    narrowed = cli_json(ledger, "find", "ta", "--where", "code=")
    assert ids(narrowed) == {rows["beta"]}

    # The facade takes the same filter (mapping form as well).
    assert set(ids(ledger.find(where=["code="]))) == expected
    assert set(ids(ledger.find("ta", where={"code": None}))) == {rows["beta"]}


# ---------------------------------------------------------------------------
# AC i: the Python facades and the mapping forms
# ---------------------------------------------------------------------------


def test_facades_treat_none_and_empty_as_the_list_form(where_ledger) -> None:
    ledger, rows = where_ledger

    cases = [
        (ledger.entities.ls, "code", {}),
        # The tubes are depleted, so the wider stock scope keeps this
        # comparison about the empty filter, not about availability.
        (ledger.stocks.ls, "purification", {"all_statuses": True}),
        (ledger.boxes.ls, "parent_box_id", {}),
    ]
    for ls, field, scope in cases:
        from_list = ls(where=[f"{field}="], **scope)
        from_empty = ls(where={field: ""}, **scope)
        from_none = ls(where={field: None}, **scope)
        assert ids(from_list) == ids(from_empty) == ids(from_none)

    # The old bug turned None into the literal text "None": the row that
    # stores "none" in its label must not match where={"label": None}.
    assert rows["alpha"] not in ids(ledger.entities.ls(where={"label": None}))
    # And find uses the same mapping interpretation (it also covers the shelf
    # boxes, whose kind declares the same ``code`` field).
    assert set(ids(ledger.find(where={"code": None}))) == {
        rows["beta"],
        rows["gamma"],
        rows["box_empty"],
        rows["box_null"],
    }


# ---------------------------------------------------------------------------
# AC j: ANDing and the kind rule
# ---------------------------------------------------------------------------


def test_an_empty_filter_ands_with_other_filters(
    cli_env: None, where_ledger
) -> None:
    ledger, rows = where_ledger

    # Two filters AND: gamma has the empty code and the matching name.
    assert ids(ledger.entities.ls(where=["code=", "name=gamma"])) == {
        rows["gamma"]
    }
    assert set(
        ids(
            cli_json(
                ledger,
                "entity",
                "list",
                "--where",
                "code=",
                "--where",
                "name=gamma",
            )
        )
    ) == {rows["gamma"]}

    # No kind has both ``code`` (item) and ``tag`` (other): no rows, no error.
    assert ledger.entities.ls(where=["code=", "tag="]) == []
    # A kind lacking the field contributes no rows: the ``other`` kind would
    # match an empty text column, but it never reaches the query.
    assert rows["foreign"] not in ids(ledger.entities.ls(where=["code="]))


# ---------------------------------------------------------------------------
# AC m: non-empty values are unchanged
# ---------------------------------------------------------------------------


def test_non_empty_values_still_match_exactly(cli_env: None, where_ledger) -> None:
    ledger, rows = where_ledger

    assert ids(ledger.entities.ls(where=["code=X1"])) == {rows["alpha"]}
    assert ids(ledger.entities.ls(where=["label=  "])) == {rows["delta"]}
    # The stock rows are depleted; the default stock listing is active-only.
    assert ids(
        ledger.stocks.ls(where=["supplier=Acme"], all_statuses=True)
    ) == {rows["tube_full"]}
    assert ids(
        ledger.stocks.ls(
            where=[f"purification={rows['holder']}"], all_statuses=True
        )
    ) == {rows["tube_full"]}
    assert ids(
        ledger.stocks.ls(
            where=[f"parent_stock_id={rows['tube_full']}"], all_statuses=True
        )
    ) == {rows["tube_child"]}
    assert ids(ledger.boxes.ls(where=[f"parent_box_id={rows['box_top']}"])) == {
        rows["box_child"]
    }
    assert ids(ledger.boxes.ls(where=["code=Y"])) == {rows["box_child"]}
