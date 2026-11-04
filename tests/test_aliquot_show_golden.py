"""Golden JSON for ``tm aliquot show --json`` (Tetramark-i93.5).

The three files under ``tests/data/golden/`` pin the *shape* of the record the
CLI prints for one tube: the field set, the value types (``quantity`` is a
decimal rendered as a string, ``aliases`` a sorted list, the path a single
string or ``null``) and the stable values that depend on the fixture only
(status, quantity, label, notes, slot, path, ``in_stock``, ``object``):

* ``aliquot_show_in_stock.json`` -- a tube in stock: placed in a nested box,
  with a quantity, a unit, a label and a note;
* ``aliquot_show_not_in_stock.json`` -- a tube that never left stock: no
  position, so ``box_id``/``slot``/``path`` are null and ``in_stock`` is false;
* ``aliquot_show_aliased.json`` -- a tube looked up by one of its aliases: a
  non-empty, sorted ``aliases`` list.

Every fixture tube carries its own event trail in the new ``events`` key: the
create event that made it, with its type, amount, unit, status and box name.

**Volatile fields.**  ``created_at`` (the creation instant) and every generated
id (``id``/``stock_id`` datecode ids, ``entity_id``/``box_id`` uuid4 ids)
differ on every run, so the comparison cannot freeze them.  The same holds for
the events: their ``at`` instant, their auto-increment id and the uuid of the
box they name.  :func:`_normalise` *checks* each one is the expected real
value for this ledger -- the created timestamp parses, the uuid ids are
canonical uuid4 and the ids are the ones the fixture just created, including
``id == <stock_id>.<seq>`` -- and then replaces it with a placeholder
(``<timestamp>``, ``<stock-id>``, ...).  The golden therefore pins the shape
and every stable value, not a timestamp or a random uuid.  Regenerate the
files after an intended output change with::

    TETRAMARK_UPDATE_GOLDENS=1 uv run pytest tests/test_aliquot_show_golden.py

The test module is deliberately **not** marked: it runs on the session's
default backend (the server driver unless the session was started with
``TETRAMARK_DRIVER=embedded``), while the process budget of the same read is
pinned on the embedded driver in ``tests/test_read_round_trips.py``.
"""

from __future__ import annotations

import difflib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from tetramark.cli.app import app
from tetramark.cli.output import is_uuid4
from tetramark.ops.io_csv import import_csv

from conftest import PRESETS

BASE = PRESETS / "base"
GOLDEN = Path(__file__).parent / "data" / "golden"

#: Setting this to a non-empty, non-zero value rewrites the golden files with
#: the payload the code currently prints (the maintainability escape hatch; it
#: is never set by the lanes).
UPDATE_GOLDENS_ENV = "TETRAMARK_UPDATE_GOLDENS"


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, dolt_env: dict[str, str]) -> None:
    """Run the CLI with the same isolated Dolt/Git environment as the ledger."""
    for key, value in dolt_env.items():
        monkeypatch.setenv(key, value)


def _show_json(ledger, ref: str) -> dict[str, Any]:
    """The payload ``tm aliquot show REF --json`` prints."""
    result = CliRunner().invoke(
        app, ["--ledger", str(ledger.path), "aliquot", "show", ref, "--json"]
    )
    assert result.exit_code == 0, result.output
    record = json.loads(result.output)
    assert isinstance(record, dict)
    return record


def _normalise_events(events: list[Any]) -> list[dict[str, Any]]:
    """Replace the per-run values of the event trail with placeholders.

    Three values of an event differ on every run: the instant it happened
    (``at``), the auto-increment id the ledger gave it and the uuid of a box
    it names.  Each one is asserted to be the real value of this run first
    (the timestamp parses, the id is a positive integer, the box id is a
    canonical uuid4), then replaced.  Everything else is left untouched: the
    event type, the amount, the statuses and the box *names* are the values
    the golden pins.
    """
    assert isinstance(events, list)
    normalised: list[dict[str, Any]] = []
    for event in events:
        assert isinstance(event, dict)
        at = event["at"]
        assert isinstance(at, str)
        datetime.fromisoformat(at)  # Dolt renders DATETIME with a space
        assert isinstance(event["id"], int) and event["id"] > 0
        row = dict(event)
        row["at"] = "<timestamp>"
        row["id"] = "<event-id>"
        for key in ("from_box_id", "to_box_id"):
            value = row[key]
            if value is not None:
                assert is_uuid4(value)
                row[key] = "<box-id>"
        normalised.append(row)
    return normalised


def _normalise(
    record: dict[str, Any],
    *,
    aliquot: str,
    stock: str,
    entity: str,
    box: str | None,
) -> dict[str, Any]:
    """Replace the per-run values with placeholders, after checking them.

    The values the golden cannot freeze are exactly the created-at timestamp
    and the generated ids.  Each one is asserted to be the real value of this
    run first (so a shell that returned the wrong tube, or a broken id, still
    fails), then replaced:

    * ``created_at`` -> ``<timestamp>`` (Dolt's ``YYYY-MM-DD HH:MM:SS``);
    * ``entity_id`` / ``box_id`` -> ``<entity-id>`` / ``<box-id>`` (canonical
      uuid4 ids; ``box_id`` stays ``null`` for a tube without a position);
    * ``stock_id`` -> ``<stock-id>`` and ``id`` -> ``<stock-id>.<seq>``, keeping
      the contract that a tube id is its stock id plus its sequence.

    The events get the same treatment: their instant, their id and the uuid of
    a box they name are placeholders, the rest is pinned.

    Every other field is left untouched: those are the values the golden pins.
    """
    assert record["id"] == aliquot == f"{stock}.{record['seq']}"
    assert record["stock_id"] == stock
    assert record["entity_id"] == entity
    assert record["box_id"] == box

    created_at = record["created_at"]
    assert isinstance(created_at, str)
    datetime.fromisoformat(created_at)  # Dolt renders DATETIME with a space

    assert is_uuid4(record["entity_id"])
    if box is not None:
        assert is_uuid4(box)

    normalised = dict(record)
    normalised["created_at"] = "<timestamp>"
    normalised["entity_id"] = "<entity-id>"
    normalised["box_id"] = "<box-id>" if box is not None else None
    normalised["id"] = f"<stock-id>.{record['seq']}"
    normalised["stock_id"] = "<stock-id>"
    normalised["events"] = _normalise_events(record.get("events") or [])
    return normalised


def _assert_golden(
    record: dict[str, Any],
    golden_name: str,
    *,
    aliquot: str,
    stock: str,
    entity: str,
    box: str | None,
) -> None:
    """Compare one show payload with its normalised golden file, byte for byte."""
    normalised = _normalise(
        record, aliquot=aliquot, stock=stock, entity=entity, box=box
    )
    # The CLI prints JSON with indent=2 and sort_keys=True; a golden is that
    # same serialisation, so the comparison also pins key order and nesting.
    rendered = json.dumps(normalised, indent=2, sort_keys=True) + "\n"
    golden = GOLDEN / golden_name
    if os.environ.get(UPDATE_GOLDENS_ENV, "").strip() not in ("", "0"):
        golden.write_text(rendered, encoding="utf-8")
    stored = golden.read_text(encoding="utf-8")
    if stored != rendered:
        diff = "".join(
            difflib.unified_diff(
                stored.splitlines(keepends=True),
                rendered.splitlines(keepends=True),
                fromfile=f"{golden_name} (stored)",
                tofile=f"{golden_name} (printed)",
            )
        )
        pytest.fail(
            f"{golden_name} is out of date; regenerate it with "
            f"{UPDATE_GOLDENS_ENV}=1 uv run pytest "
            f"{Path(__file__).name}\n{diff}"
        )


def _placed_tube(ledger, csv_dir: Path) -> dict[str, Any]:
    """One tube in stock: nested box, quantity, unit, label and note.

    The label is a core ``aliquot`` column with no ``tm aliquot add`` option
    (spec 6 has no per-field edit for it), so the tube is created through the
    CSV import path -- the public way to set it -- with ``aliquots=0`` on the
    stock so the importer creates the single tube.
    """
    entity = ledger.entities.add("item", name="alpha")
    ledger.boxes.add("shelf", "Freezer-1")
    ledger.boxes.add("shelf", "Shelf-2", parent="Freezer-1")
    box = ledger.boxes.add("grid96", "Box", parent="Shelf-2")
    stock = ledger.stocks.add("lot", entity=entity["id"], aliquots=0)
    path = csv_dir / "aliquots.csv"
    path.write_text(
        "id,stock,seq,status,box,slot,label,quantity,unit,notes\n"
        f"{stock['id']}.1,{stock['id']},1,stored,Box,A3,alpha-1,100,ul,tube one\n",
        encoding="utf-8",
    )
    import_csv(ledger, "aliquot", path, object_type="aliquot")
    return {
        "entity": entity["id"],
        "box": box["id"],
        "stock": stock["id"],
        "aliquot": f"{stock['id']}.1",
    }


def _unplaced_tube(ledger) -> dict[str, Any]:
    """One tube that is not in stock: a quantity but no position."""
    entity = ledger.entities.add("item", name="beta")
    stock = ledger.stocks.add(
        "lot", entity=entity["id"], quantity="50", unit="ul"
    )
    return {
        "entity": entity["id"],
        "box": None,
        "stock": stock["id"],
        "aliquot": stock["aliquots"][0],
    }


def _aliased_tube(ledger) -> dict[str, Any]:
    """One tube with two aliases; the golden shows the sorted list."""
    entity = ledger.entities.add("item", name="gamma")
    stock = ledger.stocks.add("lot", entity=entity["id"])
    aliquot = stock["aliquots"][0]
    # Added out of order on purpose: show must return them sorted.
    ledger.aliases.add("aliquot", aliquot, "tube-alpha")
    ledger.aliases.add("aliquot", aliquot, "tube-1")
    return {
        "entity": entity["id"],
        "box": None,
        "stock": stock["id"],
        "aliquot": aliquot,
    }


def test_aliquot_show_golden_in_stock(
    cli_env: None, ledger_factory, tmp_path: Path
) -> None:
    """A placed tube: row values, nested path, in_stock true, label and notes."""
    ledger = ledger_factory([BASE], name="golden-aliquot-show-stock", id_prefix="77")
    made = _placed_tube(ledger, tmp_path)

    record = _show_json(ledger, made["aliquot"])
    _assert_golden(
        record,
        "aliquot_show_in_stock.json",
        aliquot=made["aliquot"],
        stock=made["stock"],
        entity=made["entity"],
        box=made["box"],
    )


def test_aliquot_show_golden_not_in_stock(cli_env: None, ledger_factory) -> None:
    """A tube without a position: box_id/slot/path null and in_stock false."""
    ledger = ledger_factory(
        [BASE], name="golden-aliquot-show-pending", id_prefix="77"
    )
    made = _unplaced_tube(ledger)

    record = _show_json(ledger, made["aliquot"])
    assert record["in_stock"] is False
    _assert_golden(
        record,
        "aliquot_show_not_in_stock.json",
        aliquot=made["aliquot"],
        stock=made["stock"],
        entity=made["entity"],
        box=None,
    )


def test_aliquot_show_golden_aliased(cli_env: None, ledger_factory) -> None:
    """A tube looked up by an alias: same record, sorted non-empty aliases."""
    ledger = ledger_factory(
        [BASE], name="golden-aliquot-show-alias", id_prefix="77"
    )
    made = _aliased_tube(ledger)

    # Look the tube up by an alias, not by its id: the join path is the one a
    # user follows when the tube label is easier to remember than the id.
    record = _show_json(ledger, "tube-alpha")
    assert record["aliases"] == ["tube-1", "tube-alpha"]
    _assert_golden(
        record,
        "aliquot_show_aliased.json",
        aliquot=made["aliquot"],
        stock=made["stock"],
        entity=made["entity"],
        box=None,
    )
