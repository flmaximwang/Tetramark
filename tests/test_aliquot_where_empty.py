"""``--where FIELD=`` selects empty aliquot fields (Tetramark-0ez.2).

Spec point 4 / AC k: ``box_id=``, ``label=`` and ``slot=`` match rows where the
column is NULL and rows where it is ``''``; ``quantity=`` matches only NULL and
never a stored ``0``; ``status=`` stays the dedicated ``--status`` usage error.
The exact-only spellings (``none``, whitespace) keep their meaning.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from tetramark.cli.app import app
from tetramark.errors import UsageError

from conftest import PRESETS

BASE = PRESETS / "base"


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, dolt_env: dict[str, str]) -> None:
    """Run the CLI with the same isolated Dolt/Git environment as the ledger."""
    for key, value in dolt_env.items():
        monkeypatch.setenv(key, value)


def base_args(ledger) -> list[str]:
    return ["--ledger", str(ledger.path)]


def invoke(ledger, *args: str):
    return CliRunner().invoke(app, [*base_args(ledger), *args])


def read(ledger, *args: str):
    result = invoke(ledger, *args)
    assert result.exit_code == 0, result.output
    return result


def listed_ids(ledger, *args: str) -> list[str]:
    return [row["id"] for row in json.loads(read(ledger, *args).output)]


def make_ledger(ledger_factory, name: str = "where-empty") -> dict[str, object]:
    """One unplaced tube per empty shape plus a fully placed stock.

    The unplaced tubes are ``pending`` (not in stock), so the listing needs
    ``--all`` or an explicit ``--status`` to include them.
    """
    ledger = ledger_factory([BASE], name=name, id_prefix="77")
    item = ledger.entities.add("item", name="alpha")
    ledger.boxes.add("grid96", "BOX")
    blank = ledger.stocks.add("lot", entity=item["id"], aliquots=4)
    null_box, null_slot, zero_quantity, all_null = blank["aliquots"]
    ledger.driver.execute(
        "UPDATE `aliquot` SET `box_id` = '' WHERE `id` = %s", (null_box,)
    )
    ledger.driver.execute(
        "UPDATE `aliquot` SET `slot` = '' WHERE `id` = %s", (null_slot,)
    )
    ledger.driver.execute(
        "UPDATE `aliquot` SET `label` = '', `quantity` = 0 WHERE `id` = %s",
        (zero_quantity,),
    )
    placed = ledger.stocks.add(
        "lot",
        entity=item["id"],
        aliquots=4,
        box="BOX",
        slots=("A1", "A2", "A3", "A4"),
        quantity=5,
        unit="ml",
    )
    placed_null, placed_empty, literal_none, whitespace = placed["aliquots"]
    ledger.driver.execute(
        "UPDATE `aliquot` SET `label` = '' WHERE `id` = %s", (placed_empty,)
    )
    ledger.driver.execute(
        "UPDATE `aliquot` SET `label` = 'none' WHERE `id` = %s", (literal_none,)
    )
    ledger.driver.execute(
        "UPDATE `aliquot` SET `label` = '  ' WHERE `id` = %s", (whitespace,)
    )
    return {
        "ledger": ledger,
        "null_box": null_box,
        "null_slot": null_slot,
        "zero_quantity": zero_quantity,
        "all_null": all_null,
        "placed_null": placed_null,
        "placed_empty": placed_empty,
        "literal_none": literal_none,
        "whitespace": whitespace,
    }


def test_aliquot_list_where_empty_text_fields(ledger_factory, cli_env: None) -> None:
    """box_id=, slot= and label= match both NULL and '', through the CLI."""
    data = make_ledger(ledger_factory)
    ledger = data["ledger"]

    box_ids = listed_ids(
        ledger, "aliquot", "list", "--all", "--where", "box_id=", "--json"
    )
    assert sorted(box_ids) == sorted(
        [
            data["null_box"],
            data["null_slot"],
            data["zero_quantity"],
            data["all_null"],
        ]
    )

    slot_ids = listed_ids(
        ledger, "aliquot", "list", "--all", "--where", "slot=", "--json"
    )
    assert sorted(slot_ids) == sorted(
        [
            data["null_box"],
            data["null_slot"],
            data["zero_quantity"],
            data["all_null"],
        ]
    )

    label_ids = listed_ids(
        ledger, "aliquot", "list", "--all", "--where", "label=", "--json"
    )
    assert sorted(label_ids) == sorted(
        [
            data["null_box"],
            data["null_slot"],
            data["zero_quantity"],
            data["all_null"],
            data["placed_null"],
            data["placed_empty"],
        ]
    )


def test_aliquot_list_where_empty_quantity_never_matches_zero(
    ledger_factory, cli_env: None
) -> None:
    """quantity= matches NULL only; a stored 0 (and a stored 5) never match."""
    data = make_ledger(ledger_factory, name="where-quantity")
    ledger = data["ledger"]

    quantity_ids = listed_ids(
        ledger, "aliquot", "list", "--all", "--where", "quantity=", "--json"
    )
    assert sorted(quantity_ids) == sorted(
        [data["null_box"], data["null_slot"], data["all_null"]]
    )
    assert data["zero_quantity"] not in quantity_ids
    # The row really stores 0 (a NULL row would not prove the distinction).
    every = {
        row["id"]: row
        for row in json.loads(
            read(ledger, "aliquot", "list", "--all", "--json").output
        )
    }
    assert every[data["zero_quantity"]]["quantity"] == "0.000000"


def test_aliquot_list_where_none_mapping_means_empty(ledger_factory) -> None:
    """{"f": None} and {"f": ""} equal ["f="] through the facade."""
    data = make_ledger(ledger_factory, name="where-mapping")
    ledger = data["ledger"]

    by_none = [
        row["id"]
        for row in ledger.aliquots.ls(
            all_statuses=True, where={"box_id": None}
        )
    ]
    by_empty = [
        row["id"]
        for row in ledger.aliquots.ls(
            all_statuses=True, where={"box_id": ""}
        )
    ]
    by_list = [
        row["id"]
        for row in ledger.aliquots.ls(all_statuses=True, where=["box_id="])
    ]
    assert by_none == by_empty == by_list
    assert sorted(by_none) == sorted(
        [
            data["null_box"],
            data["null_slot"],
            data["zero_quantity"],
            data["all_null"],
        ]
    )
    # The literal text "None" is still exact, not the empty filter.
    assert ledger.aliquots.ls(all_statuses=True, where={"box_id": "None"}) == []

    with pytest.raises(UsageError, match="filter the status with --status"):
        ledger.aliquots.ls(where={"status": None})
    with pytest.raises(UsageError, match="unknown field"):
        ledger.aliquots.ls(where={"nope": None})


def test_aliquot_list_where_status_stays_the_status_option_error(
    ledger_factory, cli_env: None
) -> None:
    """status= remains the --status usage error, empty value or not."""
    data = make_ledger(ledger_factory, name="where-status")
    result = invoke(data["ledger"], "aliquot", "list", "--where", "status=")
    assert isinstance(result.exception, UsageError)
    assert result.exception.exit_code == 2
    assert "filter the status with --status" in str(result.exception)


def test_aliquot_list_where_empty_ands_with_status_and_box(
    ledger_factory, cli_env: None
) -> None:
    """An empty filter ANDs with --status and --box like any other."""
    data = make_ledger(ledger_factory, name="where-and")
    ledger = data["ledger"]

    pending_box = listed_ids(
        ledger,
        "aliquot",
        "list",
        "--status",
        "pending",
        "--where",
        "box_id=",
        "--json",
    )
    assert sorted(pending_box) == sorted(
        [
            data["null_box"],
            data["null_slot"],
            data["zero_quantity"],
            data["all_null"],
        ]
    )
    # Stored tubes all sit in a box, so no stored row has an empty box_id.
    assert (
        listed_ids(
            ledger,
            "aliquot",
            "list",
            "--status",
            "stored",
            "--where",
            "box_id=",
            "--json",
        )
        == []
    )
    boxed_labels = listed_ids(
        ledger,
        "aliquot",
        "list",
        "--box",
        "BOX",
        "--where",
        "label=",
        "--json",
    )
    assert sorted(boxed_labels) == sorted(
        [data["placed_null"], data["placed_empty"]]
    )


def test_aliquot_list_where_none_and_whitespace_stay_exact(
    ledger_factory, cli_env: None
) -> None:
    """label=none matches the literal text; label='  ' still never matches NULL."""
    data = make_ledger(ledger_factory, name="where-exact")
    ledger = data["ledger"]

    assert listed_ids(
        ledger, "aliquot", "list", "--all", "--where", "label=none", "--json"
    ) == [data["literal_none"]]
    whitespace = listed_ids(
        ledger, "aliquot", "list", "--all", "--where", "label=  ", "--json"
    )
    assert data["whitespace"] in whitespace
    assert data["placed_null"] not in whitespace
    assert data["all_null"] not in whitespace
