"""Related-object lines of ``tm stock show`` (Tetramark-obv, spec §6).

The record's ``related`` object names the entity, the parent stock and the
source aliquot the three id columns point at; the human output prints them as
plain ``*`` lines, the JSON carries the structured shape and CSV keeps exactly
the columns it had before.
"""

from __future__ import annotations

import json
import uuid

import pytest
from typer.testing import CliRunner

from tetramark.cli.app import app
from tetramark.driver.embedded import EmbeddedDriver

from conftest import PRESETS

BASE = PRESETS / "base"
EXTRAS = PRESETS / "extras"


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, dolt_env: dict[str, str]) -> None:
    """Run the CLI with the same isolated Dolt/Git environment as the ledger."""
    for key, value in dolt_env.items():
        monkeypatch.setenv(key, value)


def _read(ledger, *args: str):
    result = CliRunner().invoke(app, ["--ledger", str(ledger.path), *args])
    assert result.exit_code == 0, result.output
    return result


def _raw_write(ledger, statements, summary: str = "seed raw rows") -> None:
    """Insert rows straight into the tables, in one commit (legacy-data style)."""
    with ledger.write(summary, verb="add", object="stock") as session:
        for sql, params in statements:
            session.execute(sql, params)


def _index_of(lines: list[str], needle: str) -> int:
    return next(index for index, line in enumerate(lines) if needle in line)


def _aliquots_at(lines: list[str]) -> int:
    return next(
        index for index, line in enumerate(lines) if line.startswith("aliquots")
    )


def _build_chain(ledger) -> dict:
    """A child stock of pUC19 whose parent is pBR322 and whose source is a tube."""
    entity = ledger.entities.add("item", name="pUC19")
    parent_entity = ledger.entities.add("item", name="pBR322")
    parent = ledger.stocks.add("lot", entity=parent_entity["id"], aliquots=0)
    vial = ledger.stocks.add("lot", entity=entity["id"], quantity=100, unit="ul")
    source = vial["aliquots"][0]
    child = ledger.stocks.add(
        "lot",
        entity=entity["id"],
        parent=parent["id"],
        source_aliquot=source,
        aliquots=0,
    )
    return {
        "entity": entity,
        "parent_entity": parent_entity,
        "parent": parent,
        "source": source,
        "child": child,
    }


def test_stock_show_prints_the_three_relation_lines_before_the_tubes(
    cli_env: None, ledger_factory
) -> None:
    """Entity, parent and source lines sit between the fields and ``aliquots``."""
    ledger = ledger_factory([BASE], name="obv-related", id_prefix="77")
    built = _build_chain(ledger)

    lines = _read(ledger, "stock", "show", built["child"]["id"]).output.splitlines()
    related = [line for line in lines if line.startswith("*")]
    assert related == [
        "* entity          item · pUC19",
        f"* parent stock    lot · {built['parent']['id']} (pBR322)",
        f"* source aliquot  {built['source']} · pending · lot of pUC19",
    ]

    # Plain text at column zero, after the field block and before the tubes.
    assert _index_of(lines, "entity_id") < lines.index(related[0])
    assert lines.index(related[-1]) < _aliquots_at(lines)
    for line in related:
        assert line[0] == "*" and line[1] == " "
        assert line[2] not in " │─┌└"  # not a rich table border or indent


def test_stock_show_json_carries_related_and_keeps_the_core_ids(
    cli_env: None, ledger_factory
) -> None:
    """``related`` is additive: the three id columns and their kinds stay put."""
    ledger = ledger_factory([BASE], name="obv-related-json", id_prefix="77")
    built = _build_chain(ledger)

    record = json.loads(
        _read(ledger, "stock", "show", built["child"]["id"], "--json").output
    )
    assert record["entity_id"] == built["entity"]["id"]
    assert record["parent_stock_id"] == built["parent"]["id"]
    assert record["source_aliquot_id"] == built["source"]
    assert record["related"] == {
        "entity": {
            "id": built["entity"]["id"],
            "kind": "item",
            "name": "pUC19",
        },
        "parent_stock": {
            "id": built["parent"]["id"],
            "kind": "lot",
            "entity_id": built["parent_entity"]["id"],
            "entity_name": "pBR322",
            "entity_missing": False,
        },
        "source_aliquot": {
            "id": built["source"],
            "seq": 1,
            "status": "pending",
            "stock_kind": "lot",
            "entity_id": built["entity"]["id"],
            "entity_name": "pUC19",
            "entity_missing": False,
        },
    }


def test_stock_show_csv_keeps_its_previous_columns(
    cli_env: None, ledger_factory
) -> None:
    """CSV does not gain a ``related`` column: it stays the flat machine row."""
    ledger = ledger_factory([BASE], name="obv-related-csv", id_prefix="77")
    built = _build_chain(ledger)

    record = json.loads(
        _read(ledger, "stock", "show", built["child"]["id"], "--json").output
    )
    csv_text = _read(
        ledger, "--format", "csv", "stock", "show", built["child"]["id"]
    ).output
    header = csv_text.splitlines()[0].split(",")
    assert "related" not in header
    assert header == sorted(set(record) - {"related"})
    assert "related" not in csv_text


def test_stock_show_omits_a_null_parent_and_source(
    cli_env: None, ledger_factory
) -> None:
    """No parent and no source: only the entity line, and JSON nulls."""
    ledger = ledger_factory([BASE], name="obv-related-null", id_prefix="77")
    entity = ledger.entities.add("item", name="pUC19")
    stock = ledger.stocks.add("lot", entity=entity["id"], aliquots=0)

    text = _read(ledger, "stock", "show", stock["id"]).output
    assert "* entity          item · pUC19" in text
    assert "* parent stock" not in text
    assert "* source aliquot" not in text

    record = json.loads(
        _read(ledger, "stock", "show", stock["id"], "--json").output
    )
    assert record["related"]["entity"] == {
        "id": entity["id"],
        "kind": "item",
        "name": "pUC19",
    }
    assert record["related"]["parent_stock"] is None
    assert record["related"]["source_aliquot"] is None


def test_an_unnamed_entity_keeps_its_kind_and_names_the_id(
    cli_env: None, ledger_factory
) -> None:
    """An entity kind without a name prints ``kind · <short id> (unnamed)``."""
    ledger = ledger_factory([EXTRAS], name="obv-related-unnamed", id_prefix="77")
    entity_id = str(uuid.uuid4())
    stock_id = "RAW-UNNAMED"
    _raw_write(
        ledger,
        [
            (
                "INSERT INTO `entity` (`id`, `kind`) VALUES (%s, %s)",
                (entity_id, "local_entity"),
            ),
            (
                "INSERT INTO `stock` (`id`, `kind`, `entity_id`) VALUES (%s, %s, %s)",
                (stock_id, "lot", entity_id),
            ),
            ("INSERT INTO `stock_lot` (`id`) VALUES (%s)", (stock_id,)),
        ],
    )

    text = _read(ledger, "stock", "show", stock_id).output
    assert f"* entity          local_entity · {entity_id[:8]} (unnamed)" in text

    record = json.loads(
        _read(ledger, "stock", "show", stock_id, "--json").output
    )
    assert record["related"]["entity"] == {
        "id": entity_id,
        "kind": "local_entity",
        "name": None,
    }


def test_a_missing_entity_row_prints_its_short_id_and_keeps_exit_zero(
    cli_env: None, ledger_factory
) -> None:
    """A dangling ``entity_id`` is reported, not raised (defensive branch)."""
    ledger = ledger_factory([BASE], name="obv-related-dangling", id_prefix="77")
    entity = ledger.entities.add("item", name="doomed")
    stock = ledger.stocks.add("lot", entity=entity["id"], aliquots=0)
    with ledger.write("remove the entity row", verb="add", object="stock") as session:
        session.execute("SET FOREIGN_KEY_CHECKS = 0")
        session.execute("DELETE FROM `entity` WHERE `id` = %s", (entity["id"],))

    result = _read(ledger, "stock", "show", stock["id"])
    assert f"* entity          {entity['id'][:8]} (missing)" in result.output

    record = json.loads(
        _read(ledger, "stock", "show", stock["id"], "--json").output
    )
    assert record["related"]["entity"] == {
        "id": entity["id"],
        "missing": True,
    }


@pytest.mark.embedded_driver
def test_stock_show_reads_its_relations_in_the_same_batch(
    ledger_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The three relation lookups ride in the stock's own ``query_many`` batch.

    A stock show has always cost two ``dolt sql`` processes: the batch (core
    row, aliases and extras) plus the kind-table read that must wait for the
    core row to name the kind.  The relations are extra statements inside that
    same batch, so they add no process and never grow with the tube count.
    """
    calls: list[str] = []
    original = EmbeddedDriver._invoke

    def counting(self: EmbeddedDriver, sql: str, result_format: str) -> str:
        calls.append(result_format)
        return original(self, sql, result_format)

    monkeypatch.setattr(EmbeddedDriver, "_invoke", counting)
    ledger = ledger_factory([BASE], name="obv-related-batch", id_prefix="77")
    built = _build_chain(ledger)
    plain = ledger.stocks.add("lot", entity=built["entity"]["id"], aliquots=3)

    calls.clear()
    shown = ledger.stocks.show(built["child"]["id"])
    with_relations = list(calls)

    calls.clear()
    ledger.stocks.show(plain["id"])
    without_relations = list(calls)

    assert isinstance(ledger.driver, EmbeddedDriver)
    assert shown["related"]["parent_stock"]["entity_name"] == "pBR322"
    assert with_relations == without_relations == ["json", "json"]
