"""Real-Dolt end-to-end proof that ``tm <obj> add-form`` stores what ``add`` stores.

``tests/test_form_state.py`` proves the state machine Dolt-free and
``tests/test_form_cli.py`` proves the command registration and the no-TTY
refusal.  This module is the missing half: it drives a whole form with a
:class:`~tetramark.form.source.ScriptedSource` against a real ledger built from
the repository's own ``presets/base.yaml`` and then compares the stored rows
with the ones the ordinary ``ledger.<objects>.add(...)`` writes for the same
input.

The two paths must not drift: the form only collects values and then calls the
same ``add`` (spec: "Same ledger.<objects>.add(...) as add, with no separate
validation or write logic"), so any difference these tests see is a form bug.
Every ledger comes from the cheap session template ("preset-base"), never a
real ``tm init`` per test.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from tetramark.cli import form_commands
from tetramark.cli.app import app
from tetramark.core.ids import aliquot_id
from tetramark.errors import LedgerError
from tetramark.form.session import run_add_form
from tetramark.form.source import Reply, ScriptedSource

from conftest import PRESETS, REPO_PRESETS

pytestmark = pytest.mark.embedded

BASE = REPO_PRESETS / "base.yaml"
BOXFIELDS = PRESETS / "boxfields"

runner = CliRunner()

#: Columns the two write paths legitimately do not share (random ids, clock).
VOLATILE = frozenset({"id", "created_at"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, dolt_env: dict[str, str]) -> None:
    """Run the CLI with the same isolated Dolt/Git environment as the ledger."""
    for key, value in dolt_env.items():
        monkeypatch.setenv(key, value)


def ledger_pair(ledger_factory, name: str):
    """Two identical ledgers: the form writes one, ``add`` writes the other."""
    return (
        ledger_factory([BASE], name=f"{name}-form"),
        ledger_factory([BASE], name=f"{name}-add"),
    )


def run_form(ledger, object_type: str, *replies: str | Reply):
    """Drive one complete form (answers + review ``y``) and return its result.

    A string reply is :class:`Reply(text=...)`; a :class:`Reply` is used as is
    (the long-field test pastes with ``pasted=True``).  ``ScriptedSource``
    records every prompt it was asked, and the form must ask exactly as many
    questions as the script answers -- a wrong step order then fails here
    instead of silently skipping an answer.
    """
    script = [
        reply if isinstance(reply, Reply) else Reply(text=reply) for reply in replies
    ]
    source = ScriptedSource(script)
    result = run_add_form(ledger, object_type, source=source)
    assert result is not None, "the form was expected to write"
    asked = [prompt.step.key if prompt.step else "review" for prompt in source.prompts]
    assert len(source.prompts) == len(script), (
        f"the form asked fewer questions than the script answered: {asked}"
    )
    return result, source


def comparable(record: dict) -> dict:
    """One ``show`` record without the columns that are random or clocked."""
    return {key: value for key, value in record.items() if key not in VOLATILE}


def stored_ids(ledger, stock_id: str) -> list[str]:
    rows = ledger.driver.query(
        "SELECT `id` FROM `aliquot` WHERE `stock_id` = %s ORDER BY `seq`",
        (stock_id,),
    )
    return [str(row["id"]) for row in rows]


def stock_view(ledger, stock_id: str) -> dict:
    """The stored stock as values only: kind fields, entity name, placements."""
    record = ledger.stocks.show(stock_id)
    entity_name = ledger.driver.query(
        "SELECT `name` FROM `entity` WHERE `id` = %s", (record["entity_id"],)
    )[0]["name"]
    box_names = {
        str(row["id"]): str(row["name"])
        for row in ledger.driver.query("SELECT `id`, `name` FROM `box`")
    }
    return {
        "kind": record["kind"],
        "supplier": record["supplier"],
        "catalog_no": record["catalog_no"],
        "received_on": record["received_on"],
        "expires_on": record["expires_on"],
        "entity": str(entity_name),
        "aliquots": [
            {
                "seq": row["seq"],
                "status": row["status"],
                "quantity": row["quantity"],
                "unit": row["unit"],
                "box": box_names.get(str(row["box_id"])),
                "slot": row["slot"],
            }
            for row in record["aliquots"]
        ],
    }


def aliquot_view(ledger, stock_id: str) -> list[dict]:
    rows = ledger.driver.query(
        "SELECT a.`seq`, a.`status`, a.`quantity`, a.`unit`, a.`slot`, "
        "b.`name` AS `box` FROM `aliquot` a "
        "LEFT JOIN `box` b ON b.`id` = a.`box_id` "
        "WHERE a.`stock_id` = %s ORDER BY a.`seq`",
        (stock_id,),
    )
    return [dict(row) for row in rows]


def box_view(ledger, box_id: str) -> dict:
    row = ledger.driver.query(
        "SELECT b.`name`, b.`kind`, b.`notes`, b.`parent_slot`, "
        "p.`name` AS `parent`, "
        "(SELECT COUNT(*) FROM `slot` s WHERE s.`box_id` = b.`id`) AS `slots` "
        "FROM `box` b LEFT JOIN `box` p ON p.`id` = b.`parent_box_id` "
        "WHERE b.`id` = %s",
        (box_id,),
    )[0]
    return dict(row)


def commit_count(ledger) -> int:
    return int(ledger.driver.query("SELECT COUNT(*) AS n FROM `dolt_log`")[0]["n"])


def dirty(ledger) -> list[dict]:
    return ledger.driver.query("SELECT * FROM `dolt_status`")


def row_counts(ledger, tables: tuple[str, ...] = ("entity", "entity_item", "alias")):
    return {
        table: int(
            ledger.driver.query(f"SELECT COUNT(*) AS n FROM `{table}`")[0]["n"]
        )
        for table in tables
    }


# ---------------------------------------------------------------------------
# 1) Every object's form stores what add stores
# ---------------------------------------------------------------------------


def test_entity_form_stores_what_add_stores(ledger_factory) -> None:
    form_ledger, add_ledger = ledger_pair(ledger_factory, "entity")

    created, _ = run_form(
        form_ledger,
        "entity",
        "item",  # kind
        "Alpha reagent",  # name
        "From the form.",  # description
        "reagent",  # category
        "AL-1, AL-2",  # aliases
        "Made interactively.",  # notes
        "y",  # review: write
    )
    added = add_ledger.entities.add(
        "item",
        values={
            "name": "Alpha reagent",
            "description": "From the form.",
            "category": "reagent",
            "notes": "Made interactively.",
        },
        aliases=["AL-1", "AL-2"],
    )

    form_record = form_ledger.entities.show(created["id"])
    add_record = add_ledger.entities.show(added["id"])
    assert comparable(form_record) == comparable(add_record)
    # Spelled out: these are the values the form asked for.
    assert form_record["name"] == "Alpha reagent"
    assert form_record["description"] == "From the form."
    assert form_record["category"] == "reagent"
    assert form_record["notes"] == "Made interactively."
    assert form_record["aliases"] == ["AL-1", "AL-2"]
    # ``add`` mirrors a kind's name field into the core ``entity.name`` row.
    core = form_ledger.driver.query(
        "SELECT `name` FROM `entity` WHERE `id` = %s", (created["id"],)
    )
    assert core[0]["name"] == "Alpha reagent"


def test_stock_form_stores_what_add_stores(ledger_factory) -> None:
    form_ledger, add_ledger = ledger_pair(ledger_factory, "stock")
    form_item = form_ledger.entities.add("item", values={"name": "TM item"})
    add_item = add_ledger.entities.add("item", values={"name": "TM item"})
    for ledger in (form_ledger, add_ledger):
        ledger.boxes.add("grid8x12", "FORM-96")
    # The lineage of the new stock: a parent stock and the aliquot it was
    # made from.  Both ledgers get their own, so the two paths never share a
    # row.
    form_parent = form_ledger.stocks.add(
        "lot", entity=form_item["id"], values={"supplier": "Acme"}, aliquots=1
    )
    add_parent = add_ledger.stocks.add(
        "lot", entity=add_item["id"], values={"supplier": "Acme"}, aliquots=1
    )
    form_source = form_parent["aliquots"][0]
    add_source = add_parent["aliquots"][0]

    created, source = run_form(
        form_ledger,
        "stock",
        "lot",  # kind
        form_item["id"],  # entity
        "Acme",  # supplier
        "AC-42",  # catalog_no
        "2026-01-05",  # received_on
        "2027-01-05",  # expires_on
        form_parent["id"],  # parent stock
        form_source,  # source aliquot
        "Derived for the assay.",  # notes
        "y",  # review: write
    )
    asked = [prompt.step.key if prompt.step else "review" for prompt in source.prompts]
    assert asked == [
        "kind",
        "entity",
        "supplier",
        "catalog_no",
        "received_on",
        "expires_on",
        "parent",
        "source_aliquot",
        "notes",
        "review",
    ]

    added = add_ledger.stocks.add(
        "lot",
        entity=add_item["id"],
        values={
            "supplier": "Acme",
            "catalog_no": "AC-42",
            "received_on": "2026-01-05",
            "expires_on": "2027-01-05",
            "notes": "Derived for the assay.",
        },
        aliquots=0,
        parent=add_parent["id"],
        source_aliquot=add_source,
    )

    view = stock_view(form_ledger, created["id"])
    assert view == stock_view(add_ledger, added["id"])
    assert view["entity"] == "TM item"
    assert (view["supplier"], view["catalog_no"]) == ("Acme", "AC-42")
    assert (view["received_on"], view["expires_on"]) == ("2026-01-05", "2027-01-05")
    # The lineage and the notes are stored exactly as they were asked for, and
    # the two ledgers point at their own parent stock / source aliquot.
    form_row = stock_lineage_row(form_ledger, created["id"])
    add_row = stock_lineage_row(add_ledger, added["id"])
    assert form_row["parent_stock_id"] == form_parent["id"]
    assert form_row["source_aliquot_id"] == form_source
    assert form_row["notes"] == "Derived for the assay."
    assert add_row["parent_stock_id"] == add_parent["id"]
    assert add_row["source_aliquot_id"] == add_source
    assert add_row["notes"] == "Derived for the assay."
    # The form creates the stock itself: no tube, no placement, no implicit
    # aliquot, exactly like ``tm stock add ... --aliquots 0``.
    assert view["aliquots"] == []
    assert created["aliquots"] == []
    assert added["aliquots"] == []
    assert stored_ids(form_ledger, created["id"]) == []
    status = form_ledger.driver.query(
        "SELECT `status` FROM `v_stock_status` WHERE `stock_id` = %s",
        (created["id"],),
    )[0]["status"]
    assert status == "empty"

    # The separate aliquot workflow still adds tubes to the stock the form made.
    aliquots = form_ledger.aliquots.add(
        created["id"], count=2, box="FORM-96", slots=["A1", "A2"]
    )
    assert len(aliquots["ids"]) == 2
    assert [row["seq"] for row in aliquot_view(form_ledger, created["id"])] == [1, 2]
    after = form_ledger.driver.query(
        "SELECT `aliquots_total` FROM `v_stock_status` WHERE `stock_id` = %s",
        (created["id"],),
    )[0]
    assert after["aliquots_total"] == 2


def test_stock_add_without_aliquots_still_creates_one(
    cli_env: None, ledger_factory
) -> None:
    """Regression: the plain stock CLI keeps its own ``--aliquots`` default."""
    ledger = ledger_factory([BASE], name="stock-add-default")
    item = ledger.entities.add("item", values={"name": "TM item"})
    result = runner.invoke(
        app,
        [
            "--ledger",
            str(ledger.path),
            "stock",
            "add",
            "lot",
            "--entity",
            item["id"],
            "--set",
            "supplier=Acme",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    created = json.loads(result.output)
    assert len(created["aliquots"]) == 1
    assert [row["seq"] for row in aliquot_view(ledger, created["id"])] == [1]


def test_aliquot_form_stores_what_add_stores(ledger_factory) -> None:
    form_ledger, add_ledger = ledger_pair(ledger_factory, "aliquot")
    form_item = form_ledger.entities.add("item", values={"name": "TM item"})
    add_item = add_ledger.entities.add("item", values={"name": "TM item"})
    for ledger in (form_ledger, add_ledger):
        ledger.boxes.add("grid8x12", "FORM-96")
    form_stock = form_ledger.stocks.add(
        "lot", entity=form_item["id"], values={"supplier": "Acme"}, aliquots=0
    )
    add_stock = add_ledger.stocks.add(
        "lot", entity=add_item["id"], values={"supplier": "Acme"}, aliquots=0
    )

    created, source = run_form(
        form_ledger,
        "aliquot",
        "lot",  # kind (the stock's kind)
        form_stock["id"],  # stock
        "2",  # count
        "",  # quantity: none (and so no unit)
        "FORM-96",  # box
        "",  # slots: Enter accepts the next free ones
        "",  # status: skip (the placement default)
        "y",  # review: write
    )
    added = add_ledger.aliquots.add(
        add_stock["id"], count=2, box="FORM-96", slots=["A1", "A2"]
    )

    # The slots step offered the next two free slots and Enter accepted them.
    slots_prompt = next(
        prompt
        for prompt in source.prompts
        if prompt.step and prompt.step.key == "slots"
    )
    assert slots_prompt.value == "A1, A2"

    assert aliquot_view(form_ledger, form_stock["id"]) == aliquot_view(
        add_ledger, add_stock["id"]
    )
    assert aliquot_view(form_ledger, form_stock["id"]) == [
        {
            "seq": 1,
            "status": "stored",
            "quantity": None,
            "unit": None,
            "slot": "A1",
            "box": "FORM-96",
        },
        {
            "seq": 2,
            "status": "stored",
            "quantity": None,
            "unit": None,
            "slot": "A2",
            "box": "FORM-96",
        },
    ]
    # The form's result ids are the same aliquot ids ``add`` allocates.
    assert created["ids"] == stored_ids(form_ledger, form_stock["id"])
    assert created["ids"] == [
        aliquot_id(form_stock["id"], 1),
        aliquot_id(form_stock["id"], 2),
    ]
    assert added["ids"] == stored_ids(add_ledger, add_stock["id"])


def create_events(ledger, aliquot: str) -> list[dict]:
    """One tube's ``create`` events as the values the two write paths share."""
    return [
        {
            "amount": (
                None if event["amount"] is None else Decimal(str(event["amount"]))
            ),
            "unit": event["unit"],
            "to_box": event["to_box"],
            "to_slot": event["to_slot"],
            "new_status": event["new_status"],
        }
        for event in ledger.aliquots.show(aliquot)["events"]
        if event["event"] == "create"
    ]


def test_aliquot_form_with_quantity_and_unit_stores_what_add_stores(
    ledger_factory,
) -> None:
    """The quantity is the amount of EACH tube, written exactly as ``add`` does."""
    form_ledger, add_ledger = ledger_pair(ledger_factory, "aliquot-quantity")
    form_item = form_ledger.entities.add("item", values={"name": "TM item"})
    add_item = add_ledger.entities.add("item", values={"name": "TM item"})
    for ledger in (form_ledger, add_ledger):
        ledger.boxes.add("grid8x12", "FORM-96")
    form_stock = form_ledger.stocks.add(
        "lot", entity=form_item["id"], values={"supplier": "Acme"}, aliquots=0
    )
    add_stock = add_ledger.stocks.add(
        "lot", entity=add_item["id"], values={"supplier": "Acme"}, aliquots=0
    )

    created, _ = run_form(
        form_ledger,
        "aliquot",
        "lot",  # kind (the stock's kind)
        form_stock["id"],  # stock
        "2",  # count
        "0.5",  # quantity
        "ml",  # unit
        "FORM-96",  # box
        "",  # slots: Enter accepts the next free ones
        "",  # status: skip (the placement default)
        "y",  # review: write
    )
    added = add_ledger.aliquots.add(
        add_stock["id"],
        count=2,
        box="FORM-96",
        slots=["A1", "A2"],
        quantity="0.5",
        unit="ml",
    )

    # Per tube, not divided: each of the two tubes holds 0.5 ml.
    view = aliquot_view(form_ledger, form_stock["id"])
    assert view == aliquot_view(add_ledger, add_stock["id"])
    assert [(row["seq"], row["unit"], row["slot"]) for row in view] == [
        (1, "ml", "A1"),
        (2, "ml", "A2"),
    ]
    assert [Decimal(str(row["quantity"])) for row in view] == [
        Decimal("0.5"),
        Decimal("0.5"),
    ]
    # Each tube's create event carries the same amount and unit.
    assert [create_events(form_ledger, alq) for alq in created["ids"]] == [
        [
            {
                "amount": Decimal("0.5"),
                "unit": "ml",
                "to_box": "FORM-96",
                "to_slot": slot,
                "new_status": "stored",
            }
        ]
        for slot in ("A1", "A2")
    ]
    assert create_events(form_ledger, created["ids"][0]) == create_events(
        add_ledger, added["ids"][0]
    )


def test_aliquot_form_zero_quantity_is_stored_with_its_unit(ledger_factory) -> None:
    ledger = ledger_factory([BASE], name="aliquot-zero")
    ledger.boxes.add("grid8x12", "ZERO-96")
    item = ledger.entities.add("item", values={"name": "TM item"})
    stock = ledger.stocks.add(
        "lot", entity=item["id"], values={"supplier": "Acme"}, aliquots=0
    )

    created, _ = run_form(
        ledger,
        "aliquot",
        "lot",  # kind
        stock["id"],  # stock
        "2",  # count
        "0",  # quantity: zero is a quantity
        "ul",  # unit
        "ZERO-96",  # box
        "",  # slots
        "",  # status: skip (the placement default)
        "y",  # review: write
    )

    view = aliquot_view(ledger, stock["id"])
    assert [Decimal(str(row["quantity"])) for row in view] == [
        Decimal("0"),
        Decimal("0"),
    ]
    assert [row["unit"] for row in view] == ["ul", "ul"]
    assert create_events(ledger, created["ids"][0]) == [
        {
            "amount": Decimal("0"),
            "unit": "ul",
            "to_box": "ZERO-96",
            "to_slot": "A1",
            "new_status": "stored",
        }
    ]


# ---------------------------------------------------------------------------
# 1b) Every writable creation property: lineage, notes and creation status
#     (Tetramark-6gy)
# ---------------------------------------------------------------------------


def stock_lineage_row(ledger, stock_id: str) -> dict:
    """The stock columns the form's new lineage and notes answers write."""
    return dict(
        ledger.driver.query(
            "SELECT `kind`, `entity_id`, `parent_stock_id`, "
            "`source_aliquot_id`, `notes` FROM `stock` WHERE `id` = %s",
            (stock_id,),
        )[0]
    )


def test_t5_aliquot_form_status_matches_add(ledger_factory) -> None:
    """An explicit status and a skipped status both match ``aliquot add``.

    The twin-ledger test above covers the placed placement default; this test
    adds the explicit status and the unplaced default on one ledger, comparing
    every row and create event with the ordinary ``add``.
    """
    ledger = ledger_factory([BASE], name="aliquot-status")
    item = ledger.entities.add("item", values={"name": "TM item"})
    form_stock = ledger.stocks.add(
        "lot", entity=item["id"], values={"supplier": "Acme"}, aliquots=0
    )
    add_stock = ledger.stocks.add(
        "lot", entity=item["id"], values={"supplier": "Acme"}, aliquots=0
    )

    # An explicit status: the tubes carry it and the create event names it.
    created, _ = run_form(
        ledger,
        "aliquot",
        "lot",  # kind
        form_stock["id"],  # stock
        "1",  # count
        "",  # quantity: none
        "",  # box: none
        "missing",  # status: explicitly not the placement default
        "y",  # review: write
    )
    added = ledger.aliquots.add(add_stock["id"], count=1, status="missing")
    assert [row["status"] for row in aliquot_view(ledger, form_stock["id"])] == [
        "missing"
    ]
    assert create_events(ledger, created["ids"][0]) == create_events(
        ledger, added["ids"][0]
    )
    assert create_events(ledger, created["ids"][0]) == [
        {
            "amount": None,
            "unit": None,
            "to_box": None,
            "to_slot": None,
            "new_status": "missing",
        }
    ]

    # A skipped status keeps the add command's placement default: an unplaced
    # tube is ``pending``, exactly like omitting the option.
    default_tube, _ = run_form(
        ledger,
        "aliquot",
        "lot",  # kind
        form_stock["id"],  # stock
        "1",  # count
        "",  # quantity: none
        "",  # box: none
        "",  # status: skip (the placement default)
        "y",  # review: write
    )
    default_add = ledger.aliquots.add(add_stock["id"], count=1)
    assert create_events(ledger, default_tube["ids"][0]) == create_events(
        ledger, default_add["ids"][0]
    )
    assert [row["status"] for row in aliquot_view(ledger, form_stock["id"])] == [
        "missing",
        "pending",
    ]
    assert aliquot_view(ledger, form_stock["id"]) == aliquot_view(
        ledger, add_stock["id"]
    )


#: A test-only legacy closure for the creation-status step: one stock kind
#: whose per-kind set has no ``pending`` (a cryo set).  The form's status list
#: is that set, in its own order.
CUSTOM_SET_PRESET = """\
preset: formstatus
version: 1
description: >-
  Test-only legacy closure for the add-form creation-status step (Tetramark-6gy):
  one stock kind whose per-kind status set has no ``pending``.
status_sets:
  cryo:
    stored: {in_stock: true, to: [thawed, missing]}
    missing: {in_stock: false, to: [stored, thawed]}
    thawed: {in_stock: false}
mixins:
  named_fields:
    name: {type: string, required: true, unique: true}
kinds:
  item:
    object: entity
    mixins: [named_fields]
  vial:
    object: stock
    entity_kinds: [item]
    statuses: cryo
  shelf:
    object: box
    geometry: {type: slotless, capacity: 4}
"""


def test_t5_aliquot_form_custom_status_set_matches_add(
    ledger_factory, tmp_path
) -> None:
    """A per-kind set without ``pending``.

    The form never re-implements the core's placement default: it offers the
    set's own statuses and, when the step is skipped, the core decides.  The
    cryo set has no ``pending``, so a placed tube defaults to its in-stock
    status (``stored``) and an unplaced tube would default to that same
    in-stock status -- which needs a position, so the core refuses it and the
    form shows the very same refusal.  An explicitly chosen non-default status
    is stored and recorded on the create event.  (The last fallback -- a set
    with no in-stock status taking its first status -- is covered by the
    Dolt-free status-set test in tests/test_form_state.py, because the Dolt
    fixtures describe the fixed/legacy vocabulary rather than every custom set.)
    """
    preset = tmp_path / "formstatus.yaml"
    preset.write_text(CUSTOM_SET_PRESET, encoding="utf-8")
    ledger = ledger_factory([preset], name="form-status-sets", id_prefix="77")
    line = ledger.entities.add("item", values={"name": "HEK293T"})
    ledger.boxes.add("shelf", "CRYO")

    # The cryo set: placed, a skipped status takes the set's in-stock status.
    form_vial = ledger.stocks.add("vial", entity=line["id"], aliquots=0)
    add_vial = ledger.stocks.add("vial", entity=line["id"], aliquots=0)
    placed, _ = run_form(
        ledger,
        "aliquot",
        "vial",  # kind
        form_vial["id"],  # stock
        "1",  # count
        "",  # quantity: none
        "CRYO",  # box: placed
        "",  # status: skip (the placed default)
        "y",  # review: write
    )
    placed_add = ledger.aliquots.add(add_vial["id"], count=1, box="CRYO")
    assert [row["status"] for row in aliquot_view(ledger, form_vial["id"])] == [
        "stored"
    ]
    assert create_events(ledger, placed["ids"][0]) == create_events(
        ledger, placed_add["ids"][0]
    )

    # Unplaced, a skipped status would default to an in-stock status, which
    # needs a position: the core refuses it.  The refusing ``add`` runs on the
    # same stock, so the message (which names the aliquot id) is identical.
    with pytest.raises(LedgerError) as core_error:
        ledger.aliquots.add(form_vial["id"], count=1)
    assert "would have status 'stored'" in str(core_error.value)

    thawed, source = run_form(
        ledger,
        "aliquot",
        "vial",  # kind
        form_vial["id"],  # stock
        "1",  # count
        "",  # quantity: none
        "",  # box: none
        "",  # status: skip -> refused, back to the step
        "y",  # review: the write is refused
        "thawed",  # explicitly chosen status of the cryo set
        "y",  # review: write
    )
    refused = [
        prompt.error
        for prompt in source.prompts
        if prompt.step and prompt.step.key == "status" and prompt.error
    ]
    assert refused == [str(core_error.value)]

    assert create_events(ledger, thawed["ids"][0]) == [
        {
            "amount": None,
            "unit": None,
            "to_box": None,
            "to_slot": None,
            "new_status": "thawed",
        }
    ]


def test_box_form_stores_what_add_stores(ledger_factory) -> None:
    form_ledger, add_ledger = ledger_pair(ledger_factory, "box")
    for ledger in (form_ledger, add_ledger):
        ledger.boxes.add("rack", "RACK-1")

    created, _ = run_form(
        form_ledger,
        "box",
        "grid8x12",  # kind
        "PLATE-1",  # name
        "RACK-1",  # parent (a slotted rack, so the slot step appears)
        "1",  # slot
        "Handwritten label.",  # notes
        "y",  # review: write
    )
    added = add_ledger.boxes.add(
        "grid8x12",
        "PLATE-1",
        parent="RACK-1",
        slot="1",
        notes="Handwritten label.",
    )

    view = box_view(form_ledger, created["id"])
    assert view == box_view(add_ledger, added["id"])
    assert view == {
        "name": "PLATE-1",
        "kind": "grid8x12",
        "notes": "Handwritten label.",
        "parent_slot": "1",
        "parent": "RACK-1",
        "slots": 96,
    }
    assert created["slots"] == added["slots"] == 96


def test_box_form_stores_box_kind_fields_what_add_stores(ledger_factory) -> None:
    """The box form asks the kind's fields (after the name) and writes them.

    ``tm box add`` gained ``--set`` (Tetramark-026); the form still calls the
    same ``ledger.boxes.add(..., values=...)``, so the two rows must match.  A
    box kind's own ``name`` field is not asked: the form's name step is the
    core NAME argument.
    """
    form_ledger = ledger_factory([BOXFIELDS], name="boxfields-form")
    add_ledger = ledger_factory([BOXFIELDS], name="boxfields-add")

    created, source = run_form(
        form_ledger,
        "box",
        "reqbox",  # kind
        "R-FORM",  # name
        "alpha",  # label (required, no default)
        "3",  # count
        "",  # parent: none
        "",  # notes: none
        "y",  # review: write
    )
    added = add_ledger.boxes.add(
        "reqbox", "R-FORM", values={"label": "alpha", "count": 3}
    )

    asked = [prompt.step.key for prompt in source.prompts if prompt.step]
    assert asked == ["kind", "name", "label", "count", "parent", "notes"]
    row_sql = (
        "SELECT b.`name`, b.`notes`, k.`label`, k.`count` FROM `box` b "
        "JOIN `box_reqbox` k ON k.`id` = b.`id` WHERE b.`id` = %s"
    )
    form_row = form_ledger.driver.query(row_sql, (created["id"],))
    add_row = add_ledger.driver.query(row_sql, (added["id"],))
    assert form_row == add_row == [
        {"name": "R-FORM", "notes": None, "label": "alpha", "count": 3}
    ]
    assert created["fields"] == added["fields"] == {"label": "alpha", "count": 3}


# ---------------------------------------------------------------------------
# 2) The CLI prints the same message (and JSON) as ``add``
# ---------------------------------------------------------------------------


def _sequence_entity_ledger(ledger_factory, name: str) -> Path:
    """A template ledger whose entity ids are deterministic (E00001, E00002...).

    Entity ids are UUIDs by default, so two runs would print different ids and
    the messages could not be compared byte for byte.  ``ledger.yaml`` is the
    supported place to choose the strategy (defaults: entity UUID, stock
    datecode); the schema fingerprint does not cover ids, so the copy stays
    valid.
    """
    ledger = ledger_factory([BASE], name=name)
    path = ledger.path / "ledger.yaml"
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    payload.setdefault("ids", {})["entity"] = {"strategy": "sequence", "prefix": "E"}
    path.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    return ledger.path


def entity_replies(name: str, aliases: str = "SH-1, SH-2") -> tuple[Reply, ...]:
    """The entity input both paths create, as the form's questions ask it."""
    return (
        Reply(text="item"),  # kind
        Reply(text=name),  # name
        Reply(text="Entered through the form."),  # description
        Reply(text="reagent"),  # category
        Reply(text=aliases),  # aliases
        Reply(text="Same message as add."),  # notes
        Reply(text="y"),  # review: write
    )


def add_argv(name: str, alias: tuple[str, ...] = ("SH-1", "SH-2")) -> tuple[str, ...]:
    """The same entity input through the ordinary ``tm entity add`` flags."""
    argv = [
        "entity",
        "add",
        "item",
        "--name",
        name,
        "--set",
        "description=Entered through the form.",
        "--set",
        "category=reagent",
        "--set",
        "notes=Same message as add.",
    ]
    for value in alias:
        argv.extend(["--alias", value])
    return tuple(argv)


def test_add_form_cli_message_and_json_match_add(
    cli_env: None, ledger_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    form_path = _sequence_entity_ledger(ledger_factory, "cli-form")
    add_path = _sequence_entity_ledger(ledger_factory, "cli-add")

    # Each command runs twice (plain, then --json) with a fresh name, so the
    # two ledgers see the same two writes and the deterministic ids match.
    sources = [
        ScriptedSource(entity_replies("Shared entity")),
        ScriptedSource(entity_replies("Second entity", "SH-3, SH-4")),
    ]
    monkeypatch.setattr(form_commands, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(form_commands, "_make_source", lambda: sources.pop(0))

    form_plain = runner.invoke(
        app, ["--ledger", str(form_path), "entity", "add-form"]
    )
    assert form_plain.exit_code == 0, form_plain.output
    add_plain = runner.invoke(
        app, ["--ledger", str(add_path), *add_argv("Shared entity")]
    )
    assert add_plain.exit_code == 0, add_plain.output
    assert "E00001" in form_plain.output  # the configured strategy was used
    # Byte for byte, not just "contains": the form prints add's message.
    assert form_plain.output == add_plain.output

    form_json = runner.invoke(
        app, ["--ledger", str(form_path), "entity", "add-form", "--json"]
    )
    assert form_json.exit_code == 0, form_json.output
    add_json = runner.invoke(
        app,
        [
            "--ledger",
            str(add_path),
            *add_argv("Second entity", ("SH-3", "SH-4")),
            "--json",
        ],
    )
    assert add_json.exit_code == 0, add_json.output
    assert form_json.output == add_json.output
    payload = json.loads(form_json.output)
    assert payload["id"] == "E00002"
    assert set(payload) == {"aliases", "id", "kind", "name", "object"}
    assert payload["aliases"] == ["SH-3", "SH-4"]


def test_add_form_cli_aliquot_quantity_and_json_match_add(
    cli_env: None, ledger_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The aliquot form prints exactly what ``tm aliquot add -q -u`` prints."""
    form_ledger = ledger_factory([BASE], name="cli-aliquot-form")
    add_ledger = ledger_factory([BASE], name="cli-aliquot-add")
    stocks = []
    for ledger in (form_ledger, add_ledger):
        item = ledger.entities.add("item", values={"name": "TM item"})
        stocks.append(
            ledger.stocks.add(
                "lot", entity=item["id"], values={"supplier": "Acme"}, aliquots=0
            )
        )
    form_stock, add_stock = stocks
    # The datecode stock id is deterministic, so both messages name the same
    # stock and the aliquot ids line up.
    assert form_stock["id"] == add_stock["id"]

    replies = (
        "lot",  # kind
        form_stock["id"],  # stock
        "2",  # count
        "0.5",  # quantity
        "ml",  # unit
        "",  # box: none
        "",  # status: skip (the placement default)
        "y",  # review: write
    )
    monkeypatch.setattr(form_commands, "_stdin_is_tty", lambda: True)
    sources = [
        ScriptedSource(tuple(Reply(text=text) for text in replies)),
        ScriptedSource(tuple(Reply(text=text) for text in replies)),
    ]
    monkeypatch.setattr(form_commands, "_make_source", lambda: sources.pop(0))

    form_plain = runner.invoke(
        app, ["--ledger", str(form_ledger.path), "aliquot", "add-form"]
    )
    assert form_plain.exit_code == 0, form_plain.output
    add_plain = runner.invoke(
        app,
        [
            "--ledger",
            str(add_ledger.path),
            "aliquot",
            "add",
            form_stock["id"],
            "--count",
            "2",
            "--quantity",
            "0.5",
            "--unit",
            "ml",
        ],
    )
    assert add_plain.exit_code == 0, add_plain.output
    # Byte for byte, not just "contains": the form prints add's message.
    assert form_plain.output == add_plain.output

    form_json = runner.invoke(
        app, ["--ledger", str(form_ledger.path), "aliquot", "add-form", "--json"]
    )
    assert form_json.exit_code == 0, form_json.output
    add_json = runner.invoke(
        app,
        [
            "--ledger",
            str(add_ledger.path),
            "aliquot",
            "add",
            form_stock["id"],
            "--count",
            "2",
            "--quantity",
            "0.5",
            "--unit",
            "ml",
            "--json",
        ],
    )
    assert add_json.exit_code == 0, add_json.output
    assert form_json.output == add_json.output
    payload = json.loads(form_json.output)
    assert payload["object"] == "aliquot"
    assert payload["stock_id"] == form_stock["id"]
    assert payload["ids"] == [
        aliquot_id(form_stock["id"], 3),
        aliquot_id(form_stock["id"], 4),
    ]
    assert [Decimal(str(row["quantity"])) for row in aliquot_view(
        form_ledger, form_stock["id"]
    )] == [Decimal("0.5")] * 4


# ---------------------------------------------------------------------------
# 3) --dry-run: no write, the INSERT SQL on stdout
# ---------------------------------------------------------------------------


def test_add_form_dry_run_writes_nothing_and_prints_the_sql(
    cli_env: None, ledger_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = ledger_factory([BASE], name="dry-run")
    before_log = commit_count(ledger)
    before_rows = row_counts(ledger)

    source = ScriptedSource(entity_replies("Dry run entity"))
    monkeypatch.setattr(form_commands, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(form_commands, "_make_source", lambda: source)

    result = runner.invoke(
        app, ["--ledger", str(ledger.path), "--dry-run", "entity", "add-form"]
    )
    assert result.exit_code == 0, result.output
    assert "INSERT INTO `entity`" in result.output
    assert "INSERT INTO `entity_item`" in result.output
    assert "INSERT INTO `alias`" in result.output
    # Nothing reached the database: no commit, no row, no dirty table.
    assert commit_count(ledger) == before_log
    assert row_counts(ledger) == before_rows
    assert dirty(ledger) == []


# ---------------------------------------------------------------------------
# 4) Long fields: stored in full, never shown in full
# ---------------------------------------------------------------------------


def _long_description() -> str:
    """A >120-char, multi-line description (``named_fields.description`` is long)."""
    lines = [
        "First line: " + "A" * 90,
        "Middle line: " + "G" * 100,
        "Last line: " + "T" * 80,
    ]
    text = "\n".join(lines)
    assert len(text) > 120 and text.count("\n") == 2
    return text


def test_long_description_is_stored_in_full_but_never_shown(
    ledger_factory,
) -> None:
    long_text = _long_description()
    form_ledger, add_ledger = ledger_pair(ledger_factory, "long")

    created, source = run_form(
        form_ledger,
        "entity",
        "item",  # kind
        "Long note item",  # name
        Reply(text=long_text, pasted=True),  # description, bracketed paste
        "",  # category: skip
        "",  # aliases: none
        "",  # notes: skip
        "y",  # review: write
    )
    added = add_ledger.entities.add(
        "item",
        values={"name": "Long note item", "description": long_text},
    )

    stored = form_ledger.entities.show(created["id"])["description"]
    assert stored == long_text
    assert stored == add_ledger.entities.show(added["id"])["description"]

    # No prompt ever contained the value in full: what a source may display
    # (the input line and the review rows) is the short form only.
    displayed = [prompt.value for prompt in source.prompts]
    displayed.extend(
        row.value for prompt in source.prompts for row in prompt.rows
    )
    assert all(long_text not in text for text in displayed)
    review = next(prompt for prompt in source.prompts if prompt.mode == "review")
    row = next(row for row in review.rows if row.label == "description")
    assert row.value.startswith("[pasted ")
    assert "3 lines" in row.value
    assert long_text not in row.value
