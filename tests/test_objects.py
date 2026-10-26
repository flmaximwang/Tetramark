"""Object operations: entity / stock / aliquot / box / alias (ticket 10).

All tests run against the real ``dolt`` binary through ``ledger_factory`` (which
isolates ``DOLT_ROOT_PATH``, ``HOME`` and Git config inside ``tmp_path``).
"""

from __future__ import annotations

import hashlib
import uuid
from decimal import Decimal

import pytest

from tetramark.core.ledger import open as open_ledger
from tetramark.errors import LedgerError, UsageError

from conftest import FACTORY_AUTHOR, PRESETS

FULL = PRESETS / "full"
BASE = PRESETS / "base"


@pytest.fixture
def ledger_factory(server_ledger_factory):
    return server_ledger_factory


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def commit_count(ledger) -> int:
    return int(ledger.driver.query("SELECT COUNT(*) AS n FROM dolt_log")[0]["n"])


def dirty(ledger) -> list[dict[str, object]]:
    return ledger.driver.query("SELECT * FROM dolt_status")


def make_ledger(ledger_factory, name: str = "ops"):
    """A ledger with the small base preset (fast to create)."""
    return ledger_factory([BASE], name=name, id_prefix="77")


def make_full_ledger(ledger_factory, name: str = "full"):
    """A ledger with the full preset (all field types and geometries)."""
    return ledger_factory([FULL], name=name, id_prefix="77")


def new_item(ledger, name: str = "alpha", **kwargs):
    return ledger.entities.add("item", name=name, **kwargs)


def new_box(ledger, name: str = "BOX", kind: str = "grid96", **kwargs):
    return ledger.boxes.add(kind, name, **kwargs)


def events(ledger, aliquot_id: str) -> list[dict[str, object]]:
    return ledger.driver.query(
        "SELECT `event`, `amount`, `unit`, `from_box_id`, `from_slot`, "
        "`to_box_id`, `to_slot`, `old_status`, `new_status`, `actor`, `purpose` "
        "FROM `aliquot_event` WHERE `aliquot_id` = %s ORDER BY `id`",
        (aliquot_id,),
    )


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------


def test_entity_add_show_edit_ls_is_one_commit_each(ledger_factory) -> None:
    ledger = make_ledger(ledger_factory)
    before = commit_count(ledger)

    created = ledger.entities.add(
        "item", name="alpha", values={"category": "lab"}
    )
    assert commit_count(ledger) == before + 1
    assert created == {
        "object": "entity",
        "kind": "item",
        "id": created["id"],
        "name": "alpha",
        "aliases": [],
    }

    shown = ledger.entities.show(created["id"])
    assert shown["name"] == "alpha"
    assert shown["category"] == "lab"
    assert shown["kind"] == "item"
    # The core entity.name and the kind's own name field are kept in sync.
    core = ledger.driver.query(
        "SELECT `name` FROM `entity` WHERE `id` = %s", (created["id"],)
    )[0]
    assert core["name"] == "alpha"

    before = commit_count(ledger)
    edited = ledger.entities.edit(created["id"], values={"category": "field"})
    assert commit_count(ledger) == before + 1
    assert edited["category"] == "field"

    assert [row["id"] for row in ledger.entities.ls("item")] == [created["id"]]
    matching = ledger.entities.ls("item", where={"category": "field"})
    assert [row["id"] for row in matching] == [created["id"]]
    assert ledger.entities.ls("item", where={"category": "missing"}) == []

    ledger.entities.edit(created["id"], values={"name": "beta"})
    shown = ledger.entities.show(created["id"])
    assert shown["name"] == "beta"
    assert ledger.entities.ls("item", where={"name": "beta"})[0]["id"] == created["id"]


def test_entity_add_accepts_aliases_and_show_resolves_them(ledger_factory) -> None:
    ledger = make_full_ledger(ledger_factory)
    created = ledger.entities.add("item", name="alpha", alias=["A1", "A2"])
    assert created["aliases"] == ["A1", "A2"]
    assert ledger.entities.show("A1")["id"] == created["id"]
    assert ledger.entities.show("A2")["id"] == created["id"]
    # An alias is also a valid reference for a later --set ref.
    first = ledger.entities.add("everything", name="parent", values={"code": "AAA"}, alias="P1")
    child = ledger.entities.add(
        "everything", name="child", values={"code": "BBB", "parent": "P1"}
    )
    assert ledger.entities.show(child["id"])["parent"] == first["id"]


def test_entity_field_coercion_and_sequence_normalisation(embedded_ledger_factory) -> None:
    # Stays embedded: the embedded driver renders DATE/DATETIME columns as ISO
    # strings, while PyMySQL returns ``date``/``datetime`` objects; this test
    # asserts the text form (harness gap, see T16 report).
    ledger = make_full_ledger(embedded_ledger_factory)
    ledger.entities.add("everything", name="base", values={"code": "AAA"})
    created = ledger.entities.add(
        "everything",
        name="child",
        values={
            "code": "BBB",
            "blurb": "long text",
            "count": "7",
            "ratio": "1.25",
            "active": "no",
            "born": "2020-02-29",
            "seen_at": "2024-01-01 12:34:56",
            "color": "green",
            "seq": " \n aC gT \t",
        },
    )

    row = ledger.entities.show(created["id"])
    assert row["count"] == 7
    assert isinstance(row["count"], int)
    assert Decimal(str(row["ratio"])) == Decimal("1.25")
    assert row["active"] in (False, 0)
    assert row["born"] == "2020-02-29"
    assert row["seen_at"].startswith("2024-01-01 12:34:56")
    assert row["color"] == "green"
    assert row["seq"] == "ACGT"
    assert row["seq_sha256"] == hashlib.sha256(b"ACGT").hexdigest()
    assert row["display"] == "child-BBB"

    # sequence is unique through its sha256 column
    with pytest.raises(LedgerError):
        ledger.entities.add(
            "everything", name="dup", values={"code": "CCC", "seq": "acgt"}
        )


def test_entity_bad_values_are_usage_errors(ledger_factory) -> None:
    ledger = make_full_ledger(ledger_factory)
    before = commit_count(ledger)
    cases = [
        ({"nope": "1"}, "unknown field"),
        ({"code": "abc"}, "pattern"),
        ({"code": "ABCD"}, "pattern"),
        ({"color": "purple"}, "must be one of"),
        ({"count": "x"}, "integer"),
        ({"ratio": "x"}, "decimal"),
        ({"active": "maybe"}, "boolean"),
        ({"born": "31-01-2024"}, "ISO date"),
        ({"seen_at": "yesterday"}, "ISO datetime"),
        ({"display": "X"}, "computed"),
        ({"parent": "does-not-exist"}, "cannot resolve"),
    ]
    for values, message in cases:
        with pytest.raises(UsageError, match=message):
            ledger.entities.add("everything", name="probe", values=values)
    assert commit_count(ledger) == before
    assert ledger.entities.ls("everything") == []


def test_entity_requires_fields_and_rejects_unknown_kinds(ledger_factory) -> None:
    ledger = make_ledger(ledger_factory)
    with pytest.raises(UsageError, match="missing required field"):
        ledger.entities.add("item")
    with pytest.raises(UsageError, match="unknown entity kind"):
        ledger.entities.add("nosuch", name="x")
    with pytest.raises(UsageError, match="is a stock kind"):
        ledger.entities.add("lot", name="x")
    item = new_item(ledger)
    with pytest.raises(UsageError, match="nothing to edit"):
        ledger.entities.edit(item["id"], values={})


def test_entity_explicit_ids_and_rollback_on_duplicate_name(ledger_factory) -> None:
    ledger = make_ledger(ledger_factory)
    explicit = str(uuid.uuid4())
    created = ledger.entities.add("item", name="explicit", id=explicit)
    assert created["id"] == explicit
    assert ledger.entities.show(explicit)["name"] == "explicit"

    with pytest.raises(LedgerError):
        ledger.entities.add("item", name="bad", id="not-a-uuid")

    before = commit_count(ledger)
    with pytest.raises(LedgerError):
        ledger.entities.add("item", name="explicit")  # UNIQUE name
    assert commit_count(ledger) == before
    # The failed command rolled its core row back too.
    assert ledger.driver.query(
        "SELECT `id` FROM `entity` WHERE `name` = %s", ("explicit",)
    ) == [{"id": explicit}]


# ---------------------------------------------------------------------------
# Stocks
# ---------------------------------------------------------------------------


def test_stock_add_defaults_lineage_and_show(embedded_ledger_factory) -> None:
    # Stays embedded: asserts the ISO text form of a DATE column, which only
    # the embedded driver returns (PyMySQL yields ``datetime.date``).
    ledger = make_ledger(embedded_ledger_factory)
    item = new_item(ledger)
    before = commit_count(ledger)

    first = ledger.stocks.add(
        "lot",
        entity=item["id"],
        values={"supplier": "acme", "received_on": "2024-01-02"},
    )
    assert commit_count(ledger) == before + 1
    assert first["aliquots"] == [f"{first['id']}.1"]

    shown = ledger.stocks.show(first["id"])
    assert shown["supplier"] == "acme"
    assert shown["received_on"] == "2024-01-02"
    assert shown["entity_id"] == item["id"]
    assert len(shown["aliquots"]) == 1
    assert shown["aliquots"][0]["status"] == "pending"
    assert shown["aliquots"][0]["box_id"] is None
    assert shown["aliquots"][0]["slot"] is None

    second = ledger.stocks.add(
        "lot",
        entity=item["id"],
        parent=first["id"],
        source_aliquot=first["aliquots"][0],
    )
    assert second["parent_stock_id"] == first["id"]
    assert second["source_aliquot_id"] == first["aliquots"][0]


def test_stock_add_enforces_entity_kinds(ledger_factory) -> None:
    ledger = make_full_ledger(ledger_factory)
    plasmid = ledger.entities.add("plasmid", name="p1")
    before = commit_count(ledger)
    with pytest.raises(LedgerError, match="only accepts entity kinds"):
        ledger.stocks.add("lot", entity=plasmid["id"])
    assert commit_count(ledger) == before


def test_stock_add_count_placement_quantity_and_errors(ledger_factory) -> None:
    ledger = make_ledger(ledger_factory)
    item = new_item(ledger)
    new_box(ledger, "BOX", "grid96")

    created = ledger.stocks.add(
        "lot",
        entity=item["id"],
        aliquots=2,
        box="BOX",
        slots=["A1", "A2"],
    )
    assert created["aliquots"] == [f"{created['id']}.1", f"{created['id']}.2"]
    rows = ledger.stocks.show(created["id"])["aliquots"]
    assert [row["id"] for row in rows] == created["aliquots"]
    assert [row["slot"] for row in rows] == ["A1", "A2"]
    assert [row["status"] for row in rows] == ["stored", "stored"]

    with pytest.raises(UsageError, match="requires --box"):
        ledger.stocks.add("lot", entity=item["id"], slots=["A1"])
    with pytest.raises(UsageError, match="time"):
        ledger.stocks.add(
            "lot", entity=item["id"], aliquots=1, box="BOX", slots=["A1", "A2"]
        )
    with pytest.raises(LedgerError, match="one --slot per aliquot"):
        ledger.stocks.add("lot", entity=item["id"], aliquots=2, box="BOX", slots=["A1"])

    empty = ledger.stocks.add("lot", entity=item["id"], aliquots=0)
    assert empty["aliquots"] == []
    assert ledger.stocks.show(empty["id"])["aliquots"] == []

    with pytest.raises(UsageError, match="cannot place aliquots"):
        ledger.stocks.add("lot", entity=item["id"], aliquots=0, box="BOX")

    with pytest.raises(UsageError, match="given together"):
        ledger.stocks.add("lot", entity=item["id"], quantity=5)
    with pytest.raises(UsageError, match="non-negative"):
        ledger.stocks.add("lot", entity=item["id"], quantity=-1, unit="ml")

    placed = ledger.stocks.add(
        "lot", entity=item["id"], box="BOX", slots=["B1"], quantity=100, unit="ml"
    )
    alq = ledger.stocks.show(placed["id"])["aliquots"][0]
    assert alq["status"] == "stored"
    assert alq["slot"] == "B1"
    assert Decimal(str(alq["quantity"])) == Decimal("100")
    assert alq["unit"] == "ml"


def test_stock_edit_show_and_ls(ledger_factory) -> None:
    ledger = make_ledger(ledger_factory)
    item = new_item(ledger)
    stock = ledger.stocks.add("lot", entity=item["id"], values={"supplier": "old"})

    before = commit_count(ledger)
    edited = ledger.stocks.edit(stock["id"], values={"supplier": "new", "notes": "n"})
    assert commit_count(ledger) == before + 1
    assert edited["supplier"] == "new"
    assert edited["notes"] == "n"

    assert [row["id"] for row in ledger.stocks.ls("lot")] == [stock["id"]]
    assert ledger.stocks.ls("lot", where={"supplier": "new"})[0]["id"] == stock["id"]
    assert ledger.stocks.ls("lot", where={"supplier": "old"}) == []
    with pytest.raises(UsageError, match="unknown field"):
        ledger.stocks.ls("lot", where={"bogus": "x"})
    with pytest.raises(UsageError, match="nothing to edit"):
        ledger.stocks.edit(stock["id"], values={})


def test_aliquot_fields_are_written_to_the_extension_table(ledger_factory) -> None:
    ledger = make_full_ledger(ledger_factory)
    plasmid = ledger.entities.add("plasmid", name="p1")
    stock = ledger.stocks.add(
        "bacterial",
        entity=plasmid["id"],
        values={"strain": "DH5a"},
        aliquot_values={"glycerol_pct": "20"},
    )
    row = ledger.driver.query(
        "SELECT * FROM `aliquot_bacterial` WHERE `id` = %s",
        (stock["aliquots"][0],),
    )[0]
    assert Decimal(str(row["glycerol_pct"])) == Decimal("20")

    with pytest.raises(UsageError, match="unknown aliquot field"):
        ledger.stocks.add(
            "bacterial",
            entity=plasmid["id"],
            values={"strain": "DH5a"},
            aliquot_values={"bogus": "1"},
        )


# ---------------------------------------------------------------------------
# Aliquots
# ---------------------------------------------------------------------------


def test_aliquot_add_move_use_status_events(ledger_factory) -> None:
    ledger = make_ledger(ledger_factory)
    item = new_item(ledger)
    new_box(ledger, "BOX", "grid96")
    stock = ledger.stocks.add("lot", entity=item["id"])

    before = commit_count(ledger)
    added = ledger.aliquots.add(
        stock["id"], count=2, box="BOX", slots=["A1", "A2"], quantity=200, unit="ul"
    )
    assert commit_count(ledger) == before + 1
    assert added["ids"] == [f"{stock['id']}.2", f"{stock['id']}.3"]
    alq = added["ids"][0]

    before = commit_count(ledger)
    moved = ledger.aliquots.move(alq, box="BOX", slot="A3")
    assert commit_count(ledger) == before + 1
    assert moved["slot"] == "A3"

    before = commit_count(ledger)
    used = ledger.aliquots.use(alq, "50", "ul", purpose="prep")
    assert commit_count(ledger) == before + 1
    assert Decimal(used["quantity"]) == Decimal("150")
    assert used["status"] == "stored"

    before = commit_count(ledger)
    emptied = ledger.aliquots.use(alq, "150", "ul")
    assert commit_count(ledger) == before + 1
    assert Decimal(emptied["quantity"]) == 0
    assert emptied["status"] == "consumed"

    before = commit_count(ledger)
    changed = ledger.aliquots.status(alq, "missing")
    assert commit_count(ledger) == before + 1
    assert changed == {"object": "aliquot", "id": alq, "old_status": "consumed", "status": "missing"}

    history = events(ledger, alq)
    assert [row["event"] for row in history] == [
        "create",
        "move",
        "use",
        "use",
        "status",
    ]
    assert Decimal(str(history[2]["amount"])) == Decimal("50")
    assert history[2]["purpose"] == "prep"
    assert history[1]["from_slot"] == "A1"
    assert history[1]["to_slot"] == "A3"
    assert history[4]["old_status"] == "consumed"
    assert history[4]["new_status"] == "missing"
    assert history[0]["actor"] == "Ledger Factory"
    assert ledger.author == FACTORY_AUTHOR


def test_aliquot_use_errors(ledger_factory) -> None:
    ledger = make_ledger(ledger_factory)
    item = new_item(ledger)
    stock = ledger.stocks.add("lot", entity=item["id"], quantity=100, unit="ul")
    alq = stock["aliquots"][0]

    with pytest.raises(LedgerError, match="unit mismatch"):
        ledger.aliquots.use(alq, "1", "ml")
    with pytest.raises(LedgerError, match="only"):
        ledger.aliquots.use(alq, "101", "ul")
    for bad in ("0", "-1", "nope"):
        with pytest.raises(UsageError, match="positive|decimal"):
            ledger.aliquots.use(alq, bad, "ul")

    no_quantity = ledger.stocks.add("lot", entity=item["id"])
    with pytest.raises(LedgerError, match="no quantity"):
        ledger.aliquots.use(no_quantity["aliquots"][0], "1", "ul")

    ledger.aliquots.use(alq, "100", "ul")
    with pytest.raises(LedgerError, match="already consumed"):
        ledger.aliquots.use(alq, "1", "ul")
    with pytest.raises(LedgerError, match="unknown aliquot"):
        ledger.aliquots.use("nope.1", "1", "ul")


def test_aliquot_move_placement_errors(ledger_factory) -> None:
    ledger = make_full_ledger(ledger_factory)
    item = new_item(ledger)
    new_box(ledger, "GRID", "grid96")
    new_box(ledger, "TWO", "grid9x9")
    new_box(ledger, "FREE", "slotlessbox")
    new_box(ledger, "CHILD", "rack", parent="TWO", slot="A1")
    stock = ledger.stocks.add("lot", entity=item["id"], aliquots=4)

    with pytest.raises(LedgerError, match="needs --slot"):
        ledger.aliquots.move(stock["aliquots"][0], box="GRID")
    with pytest.raises(LedgerError, match="has no slot"):
        ledger.aliquots.move(stock["aliquots"][0], box="GRID", slot="Z99")
    with pytest.raises(LedgerError, match="no slots"):
        ledger.aliquots.move(stock["aliquots"][0], box="FREE", slot="A1")
    with pytest.raises(LedgerError, match="child box"):
        ledger.aliquots.move(stock["aliquots"][0], box="TWO", slot="A1")
    with pytest.raises(LedgerError, match="unknown box"):
        ledger.aliquots.move(stock["aliquots"][0], box="NOPE")

    # slot_capacity 2 on grid9x9
    ledger.aliquots.move(stock["aliquots"][0], box="TWO", slot="B1")
    ledger.aliquots.move(stock["aliquots"][1], box="TWO", slot="B1")
    with pytest.raises(LedgerError, match="full"):
        ledger.aliquots.move(stock["aliquots"][2], box="TWO", slot="B1")


def test_aliquot_status_vocabulary_and_slot_consistency(ledger_factory) -> None:
    ledger = make_full_ledger(ledger_factory)
    item = new_item(ledger)
    new_box(ledger, "GRID", "grid96")
    stock = ledger.stocks.add("lot", entity=item["id"], box="GRID", slots=["A1"])
    alq = stock["aliquots"][0]

    with pytest.raises(UsageError, match="unknown status"):
        ledger.aliquots.status(alq, "sold")

    # Simulate a legacy row: in a slotted box but without a slot.
    ledger.driver.execute(
        "UPDATE `aliquot` SET `slot` = NULL, `status` = 'pending' WHERE `id` = %s",
        (alq,),
    )
    before = commit_count(ledger)
    with pytest.raises(LedgerError, match="has no slot"):
        ledger.aliquots.status(alq, "stored")
    assert commit_count(ledger) == before

    # A slotless box has no slot requirement.
    new_box(ledger, "FREE", "slotlessbox")
    ledger.aliquots.move(alq, box="FREE")
    ledger.aliquots.status(alq, "stored")
    assert ledger.driver.query(
        "SELECT `status`, `box_id`, `slot` FROM `aliquot` WHERE `id` = %s", (alq,)
    )[0]["status"] == "stored"


def test_aliquot_purge_removes_events_and_extension(ledger_factory) -> None:
    ledger = make_full_ledger(ledger_factory)
    plasmid = ledger.entities.add("plasmid", name="p1")
    new_box(ledger, "RACK", "rack")
    stock = ledger.stocks.add(
        "bacterial",
        entity=plasmid["id"],
        values={"strain": "DH5a"},
        box="RACK",
        slots=["top"],
        quantity=100,
        unit="ul",
    )
    alq = stock["aliquots"][0]
    ledger.aliquots.use(alq, "10", "ul")
    assert events(ledger, alq)

    before = commit_count(ledger)
    purged = ledger.aliquots.purge(alq)
    assert commit_count(ledger) == before + 1
    assert purged["purged"] is True
    assert purged["events"] >= 2
    assert events(ledger, alq) == []
    assert ledger.driver.query(
        "SELECT `id` FROM `aliquot` WHERE `id` = %s", (alq,)
    ) == []
    assert ledger.driver.query(
        "SELECT `id` FROM `aliquot_bacterial` WHERE `id` = %s", (alq,)
    ) == []


def test_purge_refuses_an_aliquot_used_as_a_source(ledger_factory) -> None:
    ledger = make_ledger(ledger_factory)
    item = new_item(ledger)
    first = ledger.stocks.add("lot", entity=item["id"])
    ledger.stocks.add(
        "lot", entity=item["id"], source_aliquot=first["aliquots"][0]
    )
    before = commit_count(ledger)
    with pytest.raises(LedgerError):
        ledger.aliquots.purge(first["aliquots"][0])
    assert commit_count(ledger) == before
    assert ledger.driver.query(
        "SELECT `id` FROM `aliquot` WHERE `id` = %s", (first["aliquots"][0],)
    )


# ---------------------------------------------------------------------------
# Boxes
# ---------------------------------------------------------------------------


def test_box_add_materialises_slots_and_lists_paths(ledger_factory) -> None:
    ledger = make_full_ledger(ledger_factory)
    before = commit_count(ledger)
    grid = ledger.boxes.add("grid9x9", "FREEZER")
    assert commit_count(ledger) == before + 1
    assert grid["slots"] == 81

    labels = [
        row["label"]
        for row in ledger.driver.query(
            "SELECT `label` FROM `slot` WHERE `box_id` = %s ORDER BY `ordinal`",
            (grid["id"],),
        )
    ]
    assert labels[0] == "A1"
    assert labels[-1] == "J9"
    assert "I1" not in labels
    assert len(labels) == len(set(labels)) == 81

    named = ledger.boxes.add("namedbox", "TUBES")
    assert named["slots"] == 3
    slotless = ledger.boxes.add("slotlessbox", "SHELF")
    assert slotless["slots"] == 0

    child = ledger.boxes.add("grid96", "RACK-1", parent="FREEZER", slot="A1")
    paths = {row["name"]: row["path"] for row in ledger.boxes.ls()}
    assert paths["FREEZER"] == "FREEZER"
    assert paths["RACK-1"] == "FREEZER/RACK-1"
    assert child["parent_slot"] == "A1"

    with pytest.raises(LedgerError, match="already in use"):
        ledger.boxes.add("grid96", "FREEZER")
    with pytest.raises(UsageError, match="requires --in"):
        ledger.boxes.add("grid96", "ORPHAN", slot="A1")


def test_box_nesting_exclusivity_acyclicity_and_slotless_capacity(
    ledger_factory,
) -> None:
    ledger = make_full_ledger(ledger_factory)
    ledger.boxes.add("grid9x9", "FREEZER")
    ledger.boxes.add("grid96", "SHELF", parent="FREEZER", slot="A1")
    ledger.boxes.add("slotlessbox", "DRAWER")

    with pytest.raises(LedgerError, match="already holds child box"):
        ledger.boxes.add("grid96", "CLASH", parent="FREEZER", slot="A1")

    item = new_item(ledger)
    with pytest.raises(LedgerError, match="child box"):
        ledger.stocks.add("lot", entity=item["id"], box="FREEZER", slots=["A1"])

    with pytest.raises(LedgerError, match="inside itself"):
        ledger.boxes.move("FREEZER", parent="SHELF", slot="A1")
    with pytest.raises(LedgerError, match="has no slot"):
        ledger.boxes.move("SHELF", parent="FREEZER", slot="Z99")
    with pytest.raises(LedgerError, match="no slots"):
        ledger.boxes.move("SHELF", parent="DRAWER", slot="A1")

    # slotless capacity 5 (aliquots + child boxes)
    for index in range(5):
        ledger.boxes.add("rack", f"R{index}", parent="DRAWER")
    with pytest.raises(LedgerError, match="full"):
        ledger.boxes.add("rack", "R6", parent="DRAWER")

    # aliquots in the same slotless box count towards the capacity too
    with pytest.raises(LedgerError, match="full"):
        ledger.stocks.add("lot", entity=item["id"], box="DRAWER", aliquots=1)


def test_box_move_updates_parent_and_path(ledger_factory) -> None:
    ledger = make_full_ledger(ledger_factory)
    ledger.boxes.add("grid9x9", "FREEZER")
    ledger.boxes.add("grid96", "SHELF", parent="FREEZER", slot="A1")
    before = commit_count(ledger)
    moved = ledger.boxes.move("SHELF", parent="FREEZER", slot="B2")
    assert commit_count(ledger) == before + 1
    assert moved["parent_slot"] == "B2"
    paths = {row["name"]: row["path"] for row in ledger.boxes.ls()}
    assert paths["SHELF"] == "FREEZER/SHELF"
    with pytest.raises(LedgerError, match="unknown box"):
        ledger.boxes.move("NOPE", parent="FREEZER", slot="A1")


# ---------------------------------------------------------------------------
# Aliases
# ---------------------------------------------------------------------------


def test_alias_uniqueness_per_object_type_and_removal(ledger_factory) -> None:
    ledger = make_ledger(ledger_factory)
    first = new_item(ledger, "first")
    second = new_item(ledger, "second")
    stock = ledger.stocks.add("lot", entity=first["id"])

    before = commit_count(ledger)
    added = ledger.aliases.add("entity", first["id"], "X")
    assert commit_count(ledger) == before + 1
    assert added["object_id"] == first["id"]
    assert ledger.entities.show("X")["id"] == first["id"]

    with pytest.raises(LedgerError, match="already has the alias"):
        ledger.aliases.add("entity", first["id"], "X")
    with pytest.raises(LedgerError, match="already used by"):
        ledger.aliases.add("entity", second["id"], "X")
    # The primary key is (object_type, alias): the same text is free elsewhere.
    ledger.aliases.add("stock", stock["id"], "X")

    with pytest.raises(UsageError, match="unknown object type"):
        ledger.aliases.add("mixture", first["id"], "Y")
    with pytest.raises(LedgerError, match="unknown entity"):
        ledger.aliases.add("entity", "missing", "Y")

    before = commit_count(ledger)
    removed = ledger.aliases.rm("entity", "X")
    assert commit_count(ledger) == before + 1
    assert removed["removed"] is True
    # Without an object type the alias is removed wherever it exists.
    anywhere = ledger.aliases.rm(None, "X")
    assert anywhere["objects"] == [
        {"object_type": "stock", "object_id": stock["id"]}
    ]
    with pytest.raises(LedgerError, match="no alias"):
        ledger.aliases.rm("entity", "X")


# ---------------------------------------------------------------------------
# Commit discipline, dry-run, no-commit and IDs
# ---------------------------------------------------------------------------


def test_every_command_is_exactly_one_commit(ledger_factory) -> None:
    ledger = make_ledger(ledger_factory)

    def step(call):
        before = commit_count(ledger)
        result = call()
        assert commit_count(ledger) == before + 1
        return result

    item = step(lambda: ledger.entities.add("item", name="alpha"))
    step(lambda: ledger.entities.edit(item["id"], values={"category": "x"}))
    box = step(lambda: ledger.boxes.add("grid96", "BOX"))
    assert box["slots"] == 96
    step(lambda: ledger.boxes.add("grid96", "FREEZER"))
    step(lambda: ledger.boxes.move("BOX", parent="FREEZER", slot="A1"))
    stock = step(lambda: ledger.stocks.add("lot", entity=item["id"]))
    step(lambda: ledger.stocks.edit(stock["id"], values={"supplier": "s"}))
    added = step(
        lambda: ledger.aliquots.add(
            stock["id"],
            count=2,
            box="BOX",
            slots=["A1", "A2"],
            quantity=50,
            unit="ul",
        )
    )
    alq = added["ids"][0]
    step(lambda: ledger.aliquots.move(alq, box="BOX", slot="A3"))
    step(lambda: ledger.aliquots.use(alq, "1", "ul"))
    step(lambda: ledger.aliquots.status(alq, "missing"))
    step(lambda: ledger.aliases.add("aliquot", alq, "ALQ"))
    step(lambda: ledger.aliases.rm("aliquot", "ALQ"))
    step(lambda: ledger.aliquots.purge(alq))


def test_dry_run_collects_sql_and_leaves_status_clean(ledger_factory) -> None:
    ledger = make_ledger(ledger_factory)
    new_box(ledger, "BOX", "grid96")
    item = new_item(ledger)
    before = commit_count(ledger)

    ledger.dry_run = True
    result = ledger.entities.add("item", name="planned")
    assert commit_count(ledger) == before
    assert dirty(ledger) == []
    assert ledger.last_write is not None
    assert "INSERT INTO `entity`" in ledger.last_write.sql
    assert result["id"]

    stock = ledger.stocks.add(
        "lot", entity=item["id"], box="BOX", slots=["A1"], quantity=1, unit="ml"
    )
    assert commit_count(ledger) == before
    assert dirty(ledger) == []
    assert ledger.last_write is not None
    assert "INSERT INTO `aliquot_event`" in ledger.last_write.sql
    assert stock["aliquots"]

    ledger.dry_run = False
    ledger.stocks.add("lot", entity=item["id"])
    assert commit_count(ledger) == before + 1
    assert dirty(ledger) == []


def test_no_commit_executes_without_a_commit(ledger_factory) -> None:
    ledger = make_ledger(ledger_factory)
    opened = open_ledger(ledger.workspace, ledger.name, no_commit=True)
    try:
        before = commit_count(opened)
        opened.entities.add("item", name="uncommitted")
        assert commit_count(opened) == before
        assert dirty(opened)
    finally:
        opened.close()


def test_datecode_stock_ids_sequence_within_a_day(ledger_factory) -> None:
    ledger = make_ledger(ledger_factory)
    item = new_item(ledger)
    first = ledger.stocks.add("lot", entity=item["id"])
    second = ledger.stocks.add("lot", entity=item["id"])
    assert first["id"][:-2] == second["id"][:-2]
    assert int(first["id"][-2:]) + 1 == int(second["id"][-2:])
    assert ledger.id_strategy("stock").validate(first["id"])
    assert first["aliquots"] == [f"{first['id']}.1"]
    assert second["aliquots"] == [f"{second['id']}.1"]


def test_stock_show_resolves_stock_aliases(ledger_factory) -> None:
    ledger = make_ledger(ledger_factory)
    item = new_item(ledger)
    stock = ledger.stocks.add("lot", entity=item["id"])
    ledger.aliases.add("stock", stock["id"], "LOT1")
    assert ledger.stocks.show("LOT1")["id"] == stock["id"]
    assert ledger.aliquots.add("LOT1")["stock_id"] == stock["id"]
