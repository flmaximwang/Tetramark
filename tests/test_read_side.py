"""Read side: find, box show, history and the shared output formats (ticket 11).

Everything runs against a real ``dolt`` binary, like the rest of the suite.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from tetramark.cli.app import app
from tetramark.errors import LedgerError
from tetramark.ops.render import box_csv_rows, render_box_text

from conftest import PRESETS

BUNDLED_BASE = "base"
FULL = PRESETS / "full"


@pytest.fixture
def ledger_factory(server_ledger_factory):
    return server_ledger_factory


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, dolt_env: dict[str, str]) -> None:
    for key, value in dolt_env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("TETRAMARK_WORKSPACE", raising=False)


def base_args(ledger) -> list[str]:
    return [
        "--workspace",
        str(ledger.workspace.data_dir),
        "--ledger",
        ledger.name,
    ]


def invoke(ledger, *args: str):
    return CliRunner().invoke(app, [*base_args(ledger), *args])


def add_item(ledger, name: str, **values) -> dict:
    return ledger.entities.add("item", name=name, values=values)


def add_lot(
    ledger,
    entity: str,
    *,
    box: str | None = None,
    slots: tuple[str, ...] = (),
    aliquots: int = 1,
    quantity: str | None = None,
):
    return ledger.stocks.add(
        "lot",
        entity=entity,
        values={"supplier": "acme"},
        aliquots=aliquots,
        box=box,
        slots=slots,
        quantity=quantity,
        unit="ul" if quantity is not None else None,
    )


# ---------------------------------------------------------------------------
# find
# ---------------------------------------------------------------------------


def test_find_precedence_and_ranking(ledger_factory) -> None:
    ledger = ledger_factory([BUNDLED_BASE], name="find-rank")
    alpha = add_item(ledger, "alpha")
    alphabet = add_item(ledger, "alphabet")
    beta = add_item(ledger, "beta", description="alpha appears in this text")
    ledger.aliases.add("entity", alpha["id"], "ALP")

    results = ledger.find("alpha")
    assert [row["id"] for row in results[:3]] == [
        alpha["id"],
        alphabet["id"],
        beta["id"],
    ]
    assert [row["match"] for row in results[:3]] == ["name", "name", "field"]
    assert [row["rank"] for row in results[:3]] == [1, 2, 3]
    assert results[0]["aliases"] == ["ALP"]

    # Case-insensitive, but not tokenising.
    assert ledger.find("ALPHA")[0]["id"] == alpha["id"]

    # An exact alias beats a name match; an exact id beats the alias tier.
    alias_hits = ledger.find("ALP")
    assert alias_hits[0]["id"] == alpha["id"]
    assert alias_hits[0]["match"] == "alias"
    assert ledger.find("alp")[0]["id"] == alpha["id"]

    ledger.aliases.add("entity", alphabet["id"], alpha["id"])
    id_hits = ledger.find(alpha["id"])
    assert id_hits[0]["id"] == alpha["id"]
    assert id_hits[0]["match"] == "id"
    assert any(
        row["id"] == alphabet["id"] and row["match"] == "alias" for row in id_hits
    )
    assert ledger.find("nothing-like-this") == []


def test_find_is_literal_and_case_insensitive(ledger_factory) -> None:
    ledger = ledger_factory([BUNDLED_BASE], name="find-literal")
    underscore = add_item(ledger, "under_score")
    add_item(ledger, "underXscore")

    hits = ledger.find("under_")
    assert [row["id"] for row in hits] == [underscore["id"]]
    assert [row["id"] for row in ledger.find("UNDER_SCORE")] == [underscore["id"]]


def test_find_reports_location_across_objects(ledger_factory) -> None:
    ledger = ledger_factory([BUNDLED_BASE], name="find-place")
    item = add_item(ledger, "placed")
    ledger.boxes.add("grid9x9", "BOX")
    stock = add_lot(
        ledger, item["id"], box="BOX", slots=("A1",), quantity="50"
    )
    aliquot = stock["aliquots"][0]

    result = ledger.find(aliquot)[0]
    assert result["object"] == "aliquot"
    assert result["location"] == "BOX:A1"
    assert result["summary"].startswith(f"aliquot {aliquot}")

    entity_hit = ledger.find(item["id"])[0]
    assert entity_hit["object"] == "entity"
    assert entity_hit["location"] == "BOX:A1"

    stock_hit = ledger.find(stock["id"])[0]
    assert stock_hit["object"] == "stock"
    assert stock_hit["location"] == "BOX:A1"

    box_hit = ledger.find("BOX")[0]
    assert box_hit["object"] == "box"
    assert box_hit["location"] == "BOX"

    # A stock kind's string field is searchable too.
    supplier_hit = ledger.find("acme")[0]
    assert supplier_hit["object"] == "stock"
    assert supplier_hit["id"] == stock["id"]


# ---------------------------------------------------------------------------
# box show
# ---------------------------------------------------------------------------


def test_box_show_grid_json_shape_and_snapshot(ledger_factory) -> None:
    ledger = ledger_factory([BUNDLED_BASE], name="render-grid")
    ledger.boxes.add("grid9x9", "GRID")
    first = add_item(ledger, "first")
    second = add_item(ledger, "second")
    stock = add_lot(
        ledger, first["id"], box="GRID", slots=("A1", "C3"), aliquots=2, quantity="10"
    )
    second_stock = add_lot(
        ledger, second["id"], box="GRID", slots=("J9",), quantity="5"
    )
    first_aliquot, second_aliquot = stock["aliquots"]
    third_aliquot = second_stock["aliquots"][0]

    view = ledger.boxes.show("GRID")
    assert set(view) == {"box", "kind", "geometry", "path", "slots"}
    assert view["box"] == "GRID"
    assert view["kind"] == "grid9x9"
    assert view["path"] == "GRID"
    assert view["geometry"] == {
        "type": "grid",
        "rows": ["A", "B", "C", "D", "E", "F", "G", "H", "J"],
        "cols": ["1", "2", "3", "4", "5", "6", "7", "8", "9"],
        "slot_capacity": 1,
    }
    assert len(view["slots"]) == 81
    by_label = {slot["label"]: slot for slot in view["slots"]}
    assert by_label["A1"]["row"] == "A"
    assert by_label["A1"]["col"] == "1"
    assert [occupant["id"] for occupant in by_label["A1"]["occupants"]] == [
        first_aliquot
    ]
    assert by_label["A1"]["occupants"][0]["status"] == "stored"
    assert [occupant["id"] for occupant in by_label["C3"]["occupants"]] == [
        second_aliquot
    ]
    assert by_label["J9"]["occupants"][0]["id"] == third_aliquot
    assert by_label["A2"]["occupants"] == []

    text = render_box_text(view)
    normalized = "\n".join(
        line.rstrip()
        .replace(first_aliquot, "{first}")
        .replace(second_aliquot, "{second}")
        .replace(third_aliquot, "{third}")
        for line in text.splitlines()
    )
    assert normalized == (
        "GRID  grid9x9  path: GRID\n"
        "3 of 81 slots used\n"
        "       1       2      3       4  5  6  7  8      9\n"
        "A  {first}\n"
        "B\n"
        "C                 {second}\n"
        "D\n"
        "E\n"
        "F\n"
        "G\n"
        "H\n"
        "J                                            {third}"
    )

    csv_rows = box_csv_rows(view)
    assert len(csv_rows) == 81
    assert csv_rows[0] == {
        "slot": "A1",
        "row": "A",
        "col": "1",
        "occupants": first_aliquot,
    }
    assert csv_rows[1]["occupants"] == ""


def test_box_show_capacity_two(ledger_factory) -> None:
    ledger = ledger_factory([FULL], name="render-cap2")
    ledger.boxes.add("grid9x9", "TWO")
    entity = ledger.entities.add(
        "everything", name="thing", values={"code": "ABC", "seq": "ATCG"}
    )
    stock = ledger.stocks.add(
        "prep",
        entity=entity["id"],
        aliquots=2,
        box="TWO",
        slots=("A1", "A1"),
        quantity="1",
        unit="ml",
    )
    first, second = stock["aliquots"]

    view = ledger.boxes.show("TWO")
    assert view["geometry"]["slot_capacity"] == 2
    a1 = next(slot for slot in view["slots"] if slot["label"] == "A1")
    assert [occupant["id"] for occupant in a1["occupants"]] == [first, second]

    text = render_box_text(view)
    a_row = next(line for line in text.splitlines() if line.startswith("A ")).rstrip()
    normalized = a_row.replace(first, "{first}").replace(second, "{second}")
    assert normalized == "A  {first},{second}"


def test_box_show_named_and_slotless_list_forms(ledger_factory) -> None:
    ledger = ledger_factory([BUNDLED_BASE], name="render-list")
    ledger.boxes.add("rack", "RACK")
    item = add_item(ledger, "racked")
    rack_stock = add_lot(
        ledger, item["id"], box="RACK", slots=("1",), quantity="3"
    )
    rack = ledger.boxes.show("RACK")
    assert set(rack) == {"box", "kind", "geometry", "path", "slots"}
    assert rack["geometry"]["type"] == "named"
    assert rack["geometry"]["slots"] == [str(number) for number in range(1, 11)]
    assert next(slot for slot in rack["slots"] if slot["label"] == "1")[
        "occupants"
    ][0]["id"] == rack_stock["aliquots"][0]
    named_text = render_box_text(rack)
    assert "slot" in named_text and "occupants" in named_text

    ledger.boxes.add("shelf", "SHELF")
    ledger.boxes.add("grid96", "INNER")
    ledger.boxes.move("INNER", parent="SHELF")
    shelf_stock = add_lot(
        ledger, item["id"], box="SHELF", quantity="7"
    )
    shelf = ledger.boxes.show("SHELF")
    assert set(shelf) == {"box", "kind", "geometry", "path", "slots", "occupants"}
    assert shelf["geometry"] == {"type": "slotless", "capacity": 12}
    assert shelf["slots"] == []
    occupants = {occupant["type"]: occupant for occupant in shelf["occupants"]}
    assert occupants["box"]["name"] == "INNER"
    assert occupants["aliquot"]["id"] == shelf_stock["aliquots"][0]
    slotless_text = render_box_text(shelf)
    assert "INNER" in slotless_text
    assert shelf_stock["aliquots"][0] in slotless_text


def test_box_show_resolves_name_id_and_alias(ledger_factory) -> None:
    ledger = ledger_factory([BUNDLED_BASE], name="render-resolve")
    created = ledger.boxes.add("grid96", "NAMED")
    ledger.aliases.add("box", created["id"], "NAMED-ALIAS")
    assert ledger.boxes.show("NAMED")["box"] == "NAMED"
    assert ledger.boxes.show(created["id"])["box"] == "NAMED"
    assert ledger.boxes.show("NAMED-ALIAS")["box"] == "NAMED"


# ---------------------------------------------------------------------------
# history
# ---------------------------------------------------------------------------


def _history_fixture(ledger):
    item = add_item(ledger, "hist")
    ledger.boxes.add("grid9x9", "H1")
    ledger.boxes.add("grid9x9", "H2")
    stock = add_lot(
        ledger, item["id"], box="H1", slots=("A1",), quantity="100"
    )
    aliquot = stock["aliquots"][0]
    ledger.aliquots.move(aliquot, box="H2", slot="B2")
    ledger.aliquots.use(aliquot, "40", "ul", purpose="prep")
    return item, stock, aliquot


def test_history_shows_create_move_use_in_order(ledger_factory) -> None:
    ledger = ledger_factory([BUNDLED_BASE], name="history")
    item, stock, aliquot = _history_fixture(ledger)

    result = ledger.history(aliquot)
    assert result["object"] == "aliquot"
    assert result["kind"] == "lot"
    assert result["id"] == aliquot
    assert [event["event"] for event in result["events"]] == [
        "create",
        "move",
        "use",
    ]
    move = result["events"][1]
    assert move["from_slot"] == "A1" and move["to_slot"] == "B2"
    assert move["from_box"] == "H1" and move["to_box"] == "H2"
    use = result["events"][2]
    assert use["purpose"] == "prep"

    messages = [commit["message"] for commit in result["commits"]]
    assert any(message.startswith("add(stock):") for message in messages)
    assert any(message.startswith("move(aliquot):") for message in messages)
    assert any(message.startswith("use(aliquot):") for message in messages)
    orders = [commit["order"] for commit in result["commits"]]
    assert orders == sorted(orders)
    assert orders[0] < orders[-1]

    # A stock and its entity see the same aliquot events.
    stock_history = ledger.history(stock["id"])
    assert [event["event"] for event in stock_history["events"]] == [
        "create",
        "move",
        "use",
    ]
    stock_messages = [commit["message"] for commit in stock_history["commits"]]
    assert any(message.startswith("add(stock):") for message in stock_messages)
    assert any(message.startswith("move(aliquot):") for message in stock_messages)
    assert any(message.startswith("use(aliquot):") for message in stock_messages)

    ledger.entities.edit(item["id"], values={"category": "lab"})
    entity_history = ledger.history(item["id"])
    assert [event["event"] for event in entity_history["events"]] == [
        "create",
        "move",
        "use",
    ]
    # A kind-table edit has no aliquot event but does produce a commit.
    assert any(
        message.startswith("edit(entity):")
        for message in (commit["message"] for commit in entity_history["commits"])
    )


def test_history_box_and_alias_resolution(ledger_factory) -> None:
    ledger = ledger_factory([BUNDLED_BASE], name="history-box")
    item, stock, aliquot = _history_fixture(ledger)
    ledger.aliases.add("aliquot", aliquot, "TUBE-1")

    by_alias = ledger.history("TUBE-1")
    assert by_alias["id"] == aliquot
    assert by_alias["aliases"] == ["TUBE-1"]

    first_box = ledger.history("H1")
    assert [event["event"] for event in first_box["events"]] == ["create", "move"]
    messages = [commit["message"] for commit in first_box["commits"]]
    assert any(message.startswith("move(aliquot):") for message in messages)

    second_box = ledger.history("H2")
    assert [event["event"] for event in second_box["events"]] == ["move"]

    with pytest.raises(LedgerError):
        ledger.history("no-such-object")


# ---------------------------------------------------------------------------
# CLI: shared table/json/csv output
# ---------------------------------------------------------------------------


def test_cli_find_json_and_no_match(cli_env: None, ledger_factory) -> None:
    ledger = ledger_factory([BUNDLED_BASE], name="cli-find")
    item = add_item(ledger, "clifind")

    result = invoke(ledger, "find", "clifind", "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload[0]["id"] == item["id"]
    assert payload[0]["match"] == "name"
    assert payload[0]["rank"] == 1

    missing = invoke(ledger, "find", "definitely-not-there")
    assert missing.exit_code == 1
    assert isinstance(missing.exception, LedgerError)
    assert "no match" in str(missing.exception).lower()


def test_cli_box_show_json_and_csv(cli_env: None, ledger_factory) -> None:
    ledger = ledger_factory([BUNDLED_BASE], name="cli-box-show")
    ledger.boxes.add("grid9x9", "CBOX")
    item = add_item(ledger, "boxitem")
    stock = add_lot(ledger, item["id"], box="CBOX", slots=("B2",), quantity="2")
    aliquot = stock["aliquots"][0]

    result = invoke(ledger, "box", "show", "CBOX", "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert set(payload) == {"box", "kind", "geometry", "path", "slots"}
    assert payload["box"] == "CBOX"
    assert next(slot for slot in payload["slots"] if slot["label"] == "B2")[
        "occupants"
    ][0]["id"] == aliquot

    text = invoke(ledger, "box", "show", "CBOX")
    assert text.exit_code == 0, text.output
    assert "CBOX" in text.output and aliquot in text.output

    csv_result = invoke(ledger, "--format", "csv", "box", "show", "CBOX")
    assert csv_result.exit_code == 0, csv_result.output
    assert csv_result.output.splitlines()[0] == "slot,row,col,occupants"
    assert f"B2,B,2,{aliquot}" in csv_result.output


def test_cli_history_json_and_csv(cli_env: None, ledger_factory) -> None:
    ledger = ledger_factory([BUNDLED_BASE], name="cli-history")
    _item, _stock, aliquot = _history_fixture(ledger)
    ledger.aliases.add("aliquot", aliquot, "CH-1")

    result = invoke(ledger, "history", "CH-1", "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert [event["event"] for event in payload["events"]] == [
        "create",
        "move",
        "use",
    ]
    assert payload["commits"]

    text = invoke(ledger, "history", "CH-1")
    assert text.exit_code == 0, text.output
    assert "Events (3):" in text.output
    assert "Commits (" in text.output

    csv_result = invoke(ledger, "--format", "csv", "history", "CH-1")
    assert csv_result.exit_code == 0, csv_result.output
    header = csv_result.output.splitlines()[0]
    assert header.startswith("section,at,event,aliquot_id")
    assert "event" in csv_result.output.splitlines()[1]


def test_cli_format_applies_to_ls_and_show(cli_env: None, ledger_factory) -> None:
    ledger = ledger_factory([BUNDLED_BASE], name="cli-format")
    first = add_item(ledger, "csvitem")

    csv_result = invoke(ledger, "--format", "csv", "entity", "ls")
    assert csv_result.exit_code == 0, csv_result.output
    lines = csv_result.output.splitlines()
    assert lines[0] == "id,kind,name"
    assert f"{first['id']},item,csvitem" in lines

    show_result = invoke(ledger, "--format", "csv", "entity", "show", first["id"])
    assert show_result.exit_code == 0, show_result.output
    show_header = show_result.output.splitlines()[0].split(",")
    assert {"id", "kind", "name"} <= set(show_header)

    # --json still wins over the global --format.
    override = invoke(ledger, "--format", "csv", "entity", "show", first["id"], "--json")
    assert override.exit_code == 0, override.output
    assert json.loads(override.output)["name"] == "csvitem"

    bad = invoke(ledger, "--format", "yaml", "entity", "ls")
    assert bad.exit_code == 2
