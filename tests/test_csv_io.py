"""CSV import/export (ticket 12).

Everything runs against the real ``dolt`` binary through the shared ledger
fixtures (which keep ``DOLT_ROOT_PATH``/``HOME`` inside ``tmp_path``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from tetramark.cli.app import app
from tetramark.ops.io_csv import (
    CsvImportError,
    export_csv,
    import_csv,
)

from conftest import PRESETS

BASE = PRESETS / "base"
FULL = PRESETS / "full"


@pytest.fixture
def ledger_factory(server_ledger_factory):
    return server_ledger_factory


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def commit_count(ledger) -> int:
    return int(ledger.driver.query("SELECT COUNT(*) AS n FROM dolt_log")[0]["n"])


def write_csv(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8", newline="")
    return path


def snapshot(ledger, tables: list[str]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for table in tables:
        rows = [dict(row) for row in ledger.driver.query(f"SELECT * FROM `{table}`")]
        result[table] = sorted(
            rows, key=lambda row: json.dumps(row, sort_keys=True, default=str)
        )
    return result


def all_tables(ledger) -> list[str]:
    tables = ["entity", "stock", "box", "slot", "aliquot", "alias"]
    for kind_name in ledger.schema.entity_kinds:
        tables.append(f"entity_{kind_name}")
    for kind_name in ledger.schema.stock_kinds:
        tables.append(f"stock_{kind_name}")
        kind = ledger.schema.kind(kind_name)
        if kind.aliquot_fields:
            tables.append(f"aliquot_{kind_name}")
    for kind_name in ledger.schema.box_kinds:
        tables.append(f"box_{kind_name}")
    return tables


def make_source(ledger):
    """A small but broad data set covering every exportable column."""
    first = ledger.entities.add(
        "everything",
        name="alpha",
        values={
            "code": "AAA",
            "blurb": "first\nline",
            "count": "3",
            "ratio": "1.5",
            "active": "yes",
            "born": "2024-02-29",
            "seen_at": "2024-01-01 12:34:56",
            "color": "green",
            "seq": " ac gt ",
            "legacy": "old",
        },
        alias=["Alpha", "A"],
    )
    second = ledger.entities.add(
        "everything",
        name="beta",
        values={"code": "BBB", "seq": "TTTT", "parent": first["id"], "count": "0"},
        alias="B",
    )
    item = ledger.entities.add(
        "item", name="widget", values={"category": "reagent"}, alias="W"
    )

    plate = ledger.boxes.add("grid96", "PLATE")
    shelf = ledger.boxes.add("shelf", "SHELF")
    rack = ledger.boxes.add("rack", "RACK")
    named = ledger.boxes.add("namedbox", "NAMED")
    tub = ledger.boxes.add("slotlessbox", "TUB")
    grid = ledger.boxes.add("grid9x9", "GRID")
    ledger.boxes.move("PLATE", parent="SHELF")
    ledger.boxes.move("NAMED", parent="SHELF")
    ledger.aliases.add("box", plate["id"], "Plate")

    prep = ledger.stocks.add(
        "prep",
        entity=first["id"],
        values={"batch": "B1"},
        aliquots=2,
        box="PLATE",
        slots=["A1", "A2"],
        quantity="500",
        unit="ul",
        aliquot_values={"volume_ul": "250"},
    )
    bacterial = ledger.stocks.add(
        "bacterial",
        entity=item["id"],
        values={"strain": "DH5a", "medium": "lb"},
        aliquots=1,
        box="NAMED",
        slots=["top"],
        quantity="1",
        unit="ml",
        aliquot_values={"glycerol_pct": "20"},
    )
    lot = ledger.stocks.add(
        "lot",
        entity=item["id"],
        values={
            "supplier": "acme",
            "received_on": "2024-01-01",
        },
        aliquots=0,
    )
    empty = ledger.stocks.add(
        "prep",
        entity=second["id"],
        values={"batch": "B2"},
        aliquots=0,
        parent=prep["id"],
        source_aliquot=prep["aliquots"][0],
    )
    tub_stock = ledger.stocks.add(
        "prep",
        entity=second["id"],
        values={"batch": "B3"},
        aliquots=1,
        box="TUB",
        quantity="2",
        unit="ml",
    )
    ledger.aliases.add("stock", prep["id"], "S1")
    ledger.aliases.add("aliquot", prep["aliquots"][1], "TUBE2")
    ledger.aliquots.status(prep["aliquots"][0], "missing")
    ledger.aliquots.use(prep["aliquots"][1], "100", "ul", purpose="test")
    return {
        "entities": [first, second, item],
        "boxes": [plate, shelf, rack, named, tub, grid],
        "stocks": [prep, bacterial, lot, empty, tub_stock],
    }


def cli_args(ledger) -> list[str]:
    return [
        "--workspace",
        str(ledger.workspace.data_dir),
        "--ledger",
        ledger.name,
    ]


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, dolt_env: dict[str, str]) -> None:
    for key, value in dolt_env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("TETRAMARK_WORKSPACE", raising=False)


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------


def test_export_all_then_import_round_trips_identical_data(
    ledger_factory, tmp_path: Path
) -> None:
    source = ledger_factory([FULL], name="source")
    make_source(source)
    tables = all_tables(source)
    expected = snapshot(source, tables)

    out = tmp_path / "export"
    exported = export_csv(source, "all", out_dir=out)
    names = {path.name for path in exported.files}
    assert {
        "everything.csv",
        "item.csv",
        "plasmid.csv",
        "boxes.csv",
        "prep.csv",
        "bacterial.csv",
        "lot.csv",
        "aliquots.csv",
        "aliases.csv",
    } <= names

    target = ledger_factory([FULL], name="target")
    before = commit_count(target)
    result = import_csv(target, "all", out)

    # One commit for the whole import, nothing else.
    assert commit_count(target) == before + 1
    assert result.rows == sum(exported.counts[name] for name in names)
    assert snapshot(target, tables) == expected
    assert target.check().passed


def test_round_trip_preserves_aliases_and_events_are_recreated(
    ledger_factory, tmp_path: Path
) -> None:
    source = ledger_factory([BASE], name="source")
    item = source.entities.add("item", name="widget", alias=["w1", "w2"])
    box = source.boxes.add("grid96", "BOX")
    stock = source.stocks.add(
        "lot",
        entity=item["id"],
        values={"supplier": "acme"},
        aliquots=1,
        box="BOX",
        slots=["C3"],
        quantity="5",
        unit="g",
    )
    source.aliases.add("stock", stock["id"], "LOT")
    source.aliases.add("aliquot", stock["aliquots"][0], "TUBE")

    out = tmp_path / "export"
    export_csv(source, "all", out_dir=out)
    target = ledger_factory([BASE], name="target")
    import_csv(target, "all", out)

    assert sorted(
        (row["object_type"], row["alias"])
        for row in target.driver.query("SELECT `object_type`, `alias` FROM `alias`")
    ) == [
        ("aliquot", "TUBE"),
        ("entity", "w1"),
        ("entity", "w2"),
        ("stock", "LOT"),
    ]
    events = target.driver.query(
        "SELECT `event`, `new_status` FROM `aliquot_event` ORDER BY `id`"
    )
    assert events == [{"event": "create", "new_status": "stored"}]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_one_bad_row_aborts_everything_with_a_row_number(
    ledger_factory, tmp_path: Path
) -> None:
    ledger = ledger_factory([BASE], name="bad")
    before_stocks = len(ledger.entities.ls())
    before = commit_count(ledger)
    path = write_csv(
        tmp_path / "items.csv",
        "name,category\n"
        "alpha,tools\n"
        ",missing-name\n"
        "gamma,tools\n",
    )

    with pytest.raises(CsvImportError) as excinfo:
        import_csv(ledger, "item", path)

    message = str(excinfo.value)
    assert "row 2" in message
    assert "nothing was written" in message
    assert len(ledger.entities.ls()) == before_stocks
    assert commit_count(ledger) == before
    assert ledger.driver.query("SELECT * FROM `dolt_status`") == []


def test_every_bad_row_is_reported(
    ledger_factory, tmp_path: Path
) -> None:
    ledger = ledger_factory([FULL], name="bad")
    path = write_csv(
        tmp_path / "everything.csv",
        "name,code,seq\n"
        "alpha,aaa,ACGT\n"  # lowercase: pattern ^[A-Z]{3}$
        "beta,BBB,TTTT\n"
        "gamma,CC,GGGG\n"  # too short
    )

    with pytest.raises(CsvImportError) as excinfo:
        import_csv(ledger, "everything", path)

    message = str(excinfo.value)
    assert "row 1" in message
    assert "row 3" in message
    assert "row 2" not in message
    assert ledger.entities.ls("everything") == []


def test_header_errors_name_the_row_and_column(
    ledger_factory, tmp_path: Path
) -> None:
    ledger = ledger_factory([BASE], name="bad")
    path = write_csv(
        tmp_path / "items.csv",
        "name,unknown_column\nalpha,x\n",
    )
    with pytest.raises(Exception) as excinfo:
        import_csv(ledger, "item", path)
    assert "row 1" in str(excinfo.value)
    assert "unknown_column" in str(excinfo.value)


def test_computed_fields_cannot_be_imported(
    ledger_factory, tmp_path: Path
) -> None:
    ledger = ledger_factory([FULL], name="bad")
    path = write_csv(
        tmp_path / "everything.csv",
        "name,display\nalpha,whatever\n",
    )
    with pytest.raises(Exception) as excinfo:
        import_csv(ledger, "everything", path)
    assert "computed" in str(excinfo.value).lower() or "unknown column" in str(
        excinfo.value
    )


# ---------------------------------------------------------------------------
# Legacy ids and optional placement columns
# ---------------------------------------------------------------------------


def test_explicit_legacy_ids_are_accepted(ledger_factory, tmp_path: Path) -> None:
    ledger = ledger_factory([BASE], name="legacy")
    item = ledger.entities.add("item", name="widget")
    path = write_csv(
        tmp_path / "lots.csv",
        "id,entity,supplier,received_on\n"
        f"0826FP02,{item['id']},acme,2024-01-01\n"
        f"0826FP01,{item['id']},acme,2024-02-01\n",
    )
    before = commit_count(ledger)
    result = import_csv(ledger, "lot", path)

    assert result.rows == 2
    assert commit_count(ledger) == before + 1
    ids = [row["id"] for row in ledger.stocks.ls("lot")]
    assert ids == ["0826FP01", "0826FP02"]


def test_explicit_entity_id_is_accepted(ledger_factory, tmp_path: Path) -> None:
    ledger = ledger_factory([BASE], name="legacy")
    path = write_csv(
        tmp_path / "items.csv",
        "id,name,category\nlegacy-item-1,widget,reagent\n",
    )
    import_csv(ledger, "item", path)
    assert ledger.entities.show("legacy-item-1")["name"] == "widget"


def test_stock_row_can_create_and_place_aliquots(
    ledger_factory, tmp_path: Path
) -> None:
    ledger = ledger_factory([BASE], name="stock")
    item = ledger.entities.add("item", name="widget")
    ledger.boxes.add("grid96", "BOX")
    path = write_csv(
        tmp_path / "lots.csv",
        "entity,aliquots,box,slot,quantity,unit,status,supplier\n"
        f"{item['id']},2,BOX,A1;A2,500,ul,stored,acme\n",
    )

    result = import_csv(ledger, "lot", path)
    assert result.counts == {"stock": 1, "aliquot": 2}
    stock = ledger.stocks.ls("lot")[0]
    aliquots = ledger.stocks.show(stock["id"])["aliquots"]
    assert [row["slot"] for row in aliquots] == ["A1", "A2"]
    assert [row["status"] for row in aliquots] == ["stored", "stored"]
    assert [str(row["quantity"]) for row in aliquots] == ["500.000000", "500.000000"]
    assert [row["unit"] for row in aliquots] == ["ul", "ul"]


def test_aliquot_rows_accept_id_or_stock_seq(
    ledger_factory, tmp_path: Path
) -> None:
    ledger = ledger_factory([BASE], name="aliquot")
    item = ledger.entities.add("item", name="widget")
    ledger.boxes.add("grid96", "BOX")
    stock = ledger.stocks.add(
        "lot",
        entity=item["id"],
        values={"supplier": "acme"},
        aliquots=0,
    )

    first = write_csv(
        tmp_path / "one.csv",
        "id,box,slot,status,quantity,unit,notes\n"
        f"{stock['id']}.1,BOX,A1,stored,100,ul,first\n",
    )
    import_csv(ledger, "aliquot", first, object_type="aliquot")

    second = write_csv(
        tmp_path / "two.csv",
        "stock,seq,box,slot\n" f"{stock['id']},2,BOX,A2\n",
    )
    import_csv(ledger, "lot", second, object_type="aliquot")

    rows = ledger.driver.query(
        "SELECT `id`, `seq`, `slot`, `status`, `quantity`, `unit`, `notes` "
        "FROM `aliquot` ORDER BY `seq`"
    )
    assert [row["id"] for row in rows] == [f"{stock['id']}.1", f"{stock['id']}.2"]
    assert [row["slot"] for row in rows] == ["A1", "A2"]
    assert rows[0]["status"] == "stored"
    assert rows[0]["notes"] == "first"
    assert rows[1]["status"] == "stored"


def test_dry_run_reports_sql_and_writes_nothing(
    ledger_factory, tmp_path: Path
) -> None:
    ledger = ledger_factory([BASE], name="dry")
    before = commit_count(ledger)
    path = write_csv(tmp_path / "items.csv", "name,category\nalpha,tools\n")
    result = import_csv(ledger, "item", path, dry_run=True)

    assert result.dry_run
    assert "INSERT INTO" in result.sql
    assert result.commit_hash is None
    assert ledger.entities.ls() == []
    assert commit_count(ledger) == before


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def test_export_one_kind(ledger_factory, tmp_path: Path) -> None:
    ledger = ledger_factory([BASE], name="export")
    ledger.entities.add("item", name="widget", alias="W")
    ledger.boxes.add("grid96", "BOX")

    result = export_csv(ledger, "item", out_dir=tmp_path / "one")
    assert [path.name for path in result.files] == ["item.csv"]
    text = (tmp_path / "one" / "item.csv").read_text(encoding="utf-8")
    assert (
        text.splitlines()[0]
        == "id,kind,name,created_at,notes,description,category,alias"
    )
    assert ",W" in text

    boxes = export_csv(ledger, "boxes", out_dir=tmp_path / "boxes")
    assert [path.name for path in boxes.files] == ["boxes.csv"]
    header = (tmp_path / "boxes" / "boxes.csv").read_text(encoding="utf-8").splitlines()[0]
    assert header.startswith("id,kind,name,parent,parent_slot,created_at,notes")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_export_and_import_round_trip(
    cli_env: None, ledger_factory, tmp_path: Path
) -> None:
    source = ledger_factory([BASE], name="source")
    ledger_item = source.entities.add("item", name="cli-widget", alias="CW")
    source.boxes.add("grid96", "CLI-BOX")
    source.stocks.add(
        "lot",
        entity=ledger_item["id"],
        values={"supplier": "acme"},
        aliquots=1,
        box="CLI-BOX",
        slots=["A1"],
        quantity="2",
        unit="ml",
    )

    out = tmp_path / "cli-export"
    runner = CliRunner()
    exported = runner.invoke(
        app, [*cli_args(source), "export", "all", "--out", str(out)]
    )
    assert exported.exit_code == 0, exported.output
    assert "Exported" in exported.output

    target = ledger_factory([BASE], name="target")
    before = commit_count(target)
    imported = runner.invoke(app, [*cli_args(target), "import", "all", str(out)])
    assert imported.exit_code == 0, imported.output
    assert "Imported" in imported.output
    assert commit_count(target) == before + 1
    assert [row["id"] for row in target.entities.ls("item")] == [ledger_item["id"]]
    assert target.entities.show("CW")["name"] == "cli-widget"


def test_cli_bad_row_exits_one_and_names_the_row(
    cli_env: None, ledger_factory, tmp_path: Path
) -> None:
    ledger = ledger_factory([BASE], name="bad")
    path = write_csv(
        tmp_path / "items.csv",
        "name,category\nalpha,tools\n,broken\n",
    )
    before = commit_count(ledger)
    result = CliRunner().invoke(app, [*cli_args(ledger), "import", "item", str(path)])
    assert result.exit_code == 1
    assert "row 2" in str(result.exception)
    assert commit_count(ledger) == before


def test_cli_dry_run_prints_the_import_sql(
    cli_env: None, ledger_factory, tmp_path: Path
) -> None:
    ledger = ledger_factory([BASE], name="dry")
    path = write_csv(tmp_path / "items.csv", "name,category\nalpha,tools\n")
    before = commit_count(ledger)
    result = CliRunner().invoke(
        app,
        [*cli_args(ledger), "--dry-run", "import", "item", str(path)],
    )
    assert result.exit_code == 0, result.output
    assert "INSERT INTO" in result.output
    assert ledger.entities.ls() == []
    assert commit_count(ledger) == before
