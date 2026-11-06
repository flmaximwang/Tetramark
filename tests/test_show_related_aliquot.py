"""``tm aliquot show`` resolves its owning stock's entity (Tetramark-obv.2).

Real Dolt, like the rest of the CLI suite: every case builds one tube and then
reads the text, JSON and CSV arms of ``tm aliquot show``.  The line format
itself is pinned without a ledger in ``tests/test_cli_related.py``; here the
question is what the core resolves and where the CLI prints it.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from tetramark.cli.app import app

from conftest import PRESETS

BASE = PRESETS / "base"


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, dolt_env: dict[str, str]) -> None:
    """Run the CLI with the same isolated Dolt/Git environment as the ledger."""
    for key, value in dolt_env.items():
        monkeypatch.setenv(key, value)


def _invoke(ledger, *args: str):
    return CliRunner().invoke(app, ["--ledger", str(ledger.path), *args])


def _read(ledger, *args: str):
    result = _invoke(ledger, *args)
    assert result.exit_code == 0, result.output
    return result


def _tube(ledger, name: str = "alpha") -> dict:
    """One entity + lot + tube, optionally placed at ``BOX:A3``.

    The tube is placed so the text order has all four parts: field block,
    ``* entity`` line, ``path:`` line and the events section.
    """
    ledger.boxes.add("grid96", "BOX")
    entity = ledger.entities.add("item", name=name)
    stock = ledger.stocks.add(
        "lot",
        entity=entity["id"],
        quantity=100,
        unit="ul",
        box="BOX",
        slots=["A3"],
    )
    return {
        "entity": entity,
        "stock": stock,
        "aliquot": stock["aliquots"][0],
    }


def _field_lines(lines: list[str]) -> list[str]:
    """The key/value field block: no relation, path, event or event-section line."""
    return [
        line
        for line in lines
        if line
        and not line.startswith(("path:", "* "))
        and not line.startswith("events  ")
        and not line.startswith("  ")
    ]


def test_entity_is_a_relation_line_between_the_fields_and_the_path(
    cli_env: None, ledger_factory
) -> None:
    """Text order is field block -> ``* entity`` -> ``path:`` -> events."""
    ledger = ledger_factory([BASE], name="related-aliquot-text", id_prefix="77")
    made = _tube(ledger)

    lines = _read(ledger, "aliquot", "show", made["aliquot"]).output.splitlines()

    entity_lines = [line for line in lines if line.startswith("* entity")]
    assert entity_lines == ["* entity          item · alpha"]
    assert sum(line.startswith("* ") for line in lines) == 1

    entity_at = lines.index(entity_lines[0])
    path_at = lines.index("path: BOX:A3")
    events_at = lines.index("events  1")
    assert entity_at < path_at < events_at
    # Everything above the relation line is a field row; the relation never
    # leaks into the key/value table as a ``related`` row.
    assert _field_lines(lines[:entity_at]) == lines[:entity_at]
    assert all(not line.startswith("related") for line in lines)
    assert lines[entity_at - 1].startswith("in_stock")


def test_an_existing_entity_without_a_name_prints_kind_and_short_id(
    cli_env: None, ledger_factory
) -> None:
    """A NULL name is ``kind · <short id> (unnamed)``; JSON keeps ``name: null``."""
    ledger = ledger_factory([BASE], name="related-aliquot-unnamed", id_prefix="77")
    made = _tube(ledger)
    entity_id = made["entity"]["id"]
    ledger.driver.execute(
        "UPDATE `entity` SET `name` = NULL WHERE `id` = %s", (entity_id,)
    )

    record = json.loads(
        _read(ledger, "aliquot", "show", made["aliquot"], "--json").output
    )
    assert record["related"] == {
        "entity": {"id": entity_id, "kind": "item", "name": None}
    }
    # The old keys stay: related is additive, never a replacement.
    assert record["entity_id"] == entity_id
    assert record["stock_kind"] == "lot"
    assert "entity_name" not in record

    text = _read(ledger, "aliquot", "show", made["aliquot"]).output
    assert (
        f"* entity          item · {entity_id[:8]} (unnamed)" in text.splitlines()
    )


def test_a_missing_entity_row_prints_the_id_and_does_not_fail(
    cli_env: None, ledger_factory
) -> None:
    """A dangling ``stock.entity_id`` is ``<short id> (missing)``, exit 0.

    Foreign-key checks are disabled for the two deletes on one connection --
    the same lever ``tm stock renumber`` uses to rewrite referenced ids -- so
    the tube keeps a stock row that points at an entity row that is gone.
    """
    ledger = ledger_factory([BASE], name="related-aliquot-missing", id_prefix="77")
    made = _tube(ledger)
    entity_id = made["entity"]["id"]
    ledger.driver.execute_many(
        [
            "SET FOREIGN_KEY_CHECKS = 0",
            ("DELETE FROM `entity` WHERE `id` = %s", (entity_id,)),
            "SET FOREIGN_KEY_CHECKS = 1",
        ]
    )

    record = json.loads(
        _read(ledger, "aliquot", "show", made["aliquot"], "--json").output
    )
    assert record["related"] == {"entity": {"id": entity_id, "missing": True}}
    assert record["entity_id"] == entity_id
    assert record["stock_kind"] == "lot"

    result = _read(ledger, "aliquot", "show", made["aliquot"])
    assert result.exit_code == 0, result.output
    assert f"* entity          {entity_id[:8]} (missing)" in result.output.splitlines()


def test_control_characters_in_a_name_become_the_literal_separator(
    cli_env: None, ledger_factory
) -> None:
    """Text replaces control runs with `` | ``; JSON keeps the original name."""
    ledger = ledger_factory([BASE], name="related-aliquot-controls", id_prefix="77")
    name = "alpha\nbeta\tgamma"
    made = _tube(ledger, name=name)

    text = _read(ledger, "aliquot", "show", made["aliquot"]).output
    assert "* entity          item · alpha | beta | gamma" in text.splitlines()
    assert "alpha\tbeta" not in text
    assert "alpha\nbeta" not in text

    record = json.loads(
        _read(ledger, "aliquot", "show", made["aliquot"], "--json").output
    )
    assert record["related"]["entity"]["name"] == name


def test_json_related_entity_is_the_owner_and_csv_keeps_its_old_columns(
    cli_env: None, ledger_factory
) -> None:
    """JSON gains ``related.entity``; CSV is byte-for-byte the previous record."""
    ledger = ledger_factory([BASE], name="related-aliquot-csv", id_prefix="77")
    made = _tube(ledger)

    record = json.loads(
        _read(ledger, "aliquot", "show", made["aliquot"], "--json").output
    )
    assert record["related"] == {
        "entity": {
            "id": made["entity"]["id"],
            "kind": "item",
            "name": "alpha",
        }
    }
    assert record["related"]["entity"]["id"] == record["entity_id"]
    # JSON is key-sorted, so ``related`` cannot change the payload's shape.
    assert list(record) == sorted(record)

    csv_output = _read(
        ledger, "--format", "csv", "aliquot", "show", made["aliquot"]
    ).output
    header = csv_output.splitlines()[0].split(",")
    assert header == sorted(set(record) - {"events", "related"})
    assert "related" not in header
    assert "entity_name" not in header
    # One header plus one flat row: no extra column, no extra line.
    assert len(csv_output.splitlines()) == 2
    assert all(len(line.split(",")) == len(header) for line in csv_output.splitlines())
