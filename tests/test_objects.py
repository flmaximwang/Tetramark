"""Object operations: entity / stock / aliquot / box / alias (ticket 10).

All tests run against the real ``dolt`` binary through ``ledger_factory`` (which
isolates ``DOLT_ROOT_PATH``, ``HOME`` and Git config inside ``tmp_path``).
"""

from __future__ import annotations

import hashlib
import inspect
import uuid
from decimal import Decimal

import pytest
import yaml

from tetramark.core.ledger import open as open_ledger
from tetramark.core.objects import _CORE_EDITABLE, _CORE_ON_ADD
from tetramark.errors import LedgerError, UsageError
from tetramark.ops.lineage import lineage

from conftest import FACTORY_AUTHOR, PRESETS, REPO_PRESETS

FULL = PRESETS / "full"
BASE = PRESETS / "base"
BOXFIELDS = PRESETS / "boxfields"
MOUSE = REPO_PRESETS / "mouse.yaml"


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


def make_boxfields_ledger(ledger_factory, name: str = "boxfields"):
    """A ledger whose only kind is the ``boxfields`` slotless box kind."""
    return ledger_factory([BOXFIELDS], name=name, id_prefix="77")


def new_box(ledger, name: str = "BOX", kind: str = "grid96", **kwargs):
    return ledger.boxes.add(kind, name, **kwargs)


def events(ledger, aliquot_id: str) -> list[dict[str, object]]:
    return ledger.driver.query(
        "SELECT `event`, `amount`, `unit`, `from_box_id`, `from_slot`, "
        "`to_box_id`, `to_slot`, `old_status`, `new_status`, `forced`, "
        "`actor`, `message` "
        "FROM `aliquot_event` WHERE `aliquot_id` = %s ORDER BY `id`",
        (aliquot_id,),
    )


# ---------------------------------------------------------------------------
# Regression: empty refs and not-yet-applied kind tables
# ---------------------------------------------------------------------------


def test_lineage_depth_zero_does_not_count_unvisited_stock_kinds(ledger_factory) -> None:
    """Counts are read only for stocks the walk visits."""
    ledger = make_full_ledger(ledger_factory, name="lineage-depth-zero")
    item = new_item(ledger)
    ledger.stocks.add("lot", entity=item["id"])
    ledger.driver.execute("DROP VIEW `v_stock_lot`")

    result = lineage(ledger, item["id"], up=False, down=True, depth=0)

    assert [node.id for node in result.nodes] == [item["id"]]


def test_show_rejects_an_empty_ref(ledger_factory) -> None:
    """An empty ref is unknown, not a prefix that matches everything."""
    ledger = make_ledger(ledger_factory, name="empty-ref")
    item = new_item(ledger)
    box = new_box(ledger)
    stock = ledger.stocks.add("lot", entity=item["id"])

    with pytest.raises(LedgerError, match="unknown entity ''"):
        ledger.entities.show("")
    with pytest.raises(LedgerError, match="unknown stock ''"):
        ledger.stocks.show("")
    with pytest.raises(LedgerError, match="unknown box ''"):
        ledger.boxes.show("")
    # The objects are still reachable normally.
    assert ledger.entities.show(item["id"])["id"] == item["id"]
    assert ledger.stocks.show(stock["id"])["id"] == stock["id"]
    assert ledger.boxes.show(box["name"])["box"] == box["name"]


def test_history_ignores_a_missing_unrelated_kind_table(ledger_factory) -> None:
    """History reads only the resolved kind's diff table."""
    ledger = make_full_ledger(ledger_factory, name="history-missing-kind")
    item = new_item(ledger)

    ledger.driver.execute("DROP TABLE `entity_plasmid`")

    result = ledger.history(item["id"])
    assert result["id"] == item["id"]
    assert result["object"] == "entity"
    assert result["commits"]


def test_show_ignores_a_missing_unrelated_kind_table(ledger_factory) -> None:
    """Only the resolved kind's table is read; another pending kind may be absent."""
    ledger = make_full_ledger(ledger_factory, name="missing-kind")
    item = new_item(ledger)
    stock = ledger.stocks.add("lot", entity=item["id"])

    # A schema kind whose table has not been applied yet (adopt/migration in
    # progress).  It is unrelated to the shown objects.
    ledger.driver.execute("DROP TABLE `entity_plasmid`")
    ledger.driver.execute("DROP TABLE `stock_prep`")

    assert ledger.entities.show(item["id"])["id"] == item["id"]
    assert ledger.stocks.show(stock["id"])["id"] == stock["id"]


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

    ledger.entities.rename(created["id"], "beta")
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
    moved = ledger.aliquots.move(alq, box="BOX", slot="A3", message="moved")
    assert commit_count(ledger) == before + 1
    assert moved["slot"] == "A3"

    before = commit_count(ledger)
    used = ledger.aliquots.use(alq, "50", "ul", message="prep")
    assert commit_count(ledger) == before + 1
    assert Decimal(used["quantity"]) == Decimal("150")
    assert used["status"] == "stored"

    before = commit_count(ledger)
    emptied = ledger.aliquots.use(alq, "150", "ul", message="used up")
    assert commit_count(ledger) == before + 1
    assert Decimal(emptied["quantity"]) == 0
    assert emptied["status"] == "consumed"

    # ``consumed`` is final in the tube set: the change needs --force + message.
    before = commit_count(ledger)
    changed = ledger.aliquots.status(
        alq, "missing", force=True, message="tube found in the back of the shelf"
    )
    assert commit_count(ledger) == before + 1
    assert changed == {
        "object": "aliquot",
        "id": alq,
        "old_status": "consumed",
        "status": "missing",
        "forced": True,
    }

    history = events(ledger, alq)
    assert [row["event"] for row in history] == [
        "create",
        "move",
        "use",
        "use",
        "status",
    ]
    assert Decimal(str(history[2]["amount"])) == Decimal("50")
    assert history[2]["message"] == "prep"
    assert history[1]["from_slot"] == "A1"
    assert history[1]["to_slot"] == "A3"
    assert history[4]["old_status"] == "consumed"
    assert history[4]["new_status"] == "missing"
    assert history[4]["forced"] in (1, True)
    assert history[4]["message"] == "tube found in the back of the shelf"
    assert history[0]["actor"] == "Ledger Factory"
    assert ledger.author == FACTORY_AUTHOR


def test_aliquot_use_errors(ledger_factory) -> None:
    ledger = make_ledger(ledger_factory)
    item = new_item(ledger)
    stock = ledger.stocks.add("lot", entity=item["id"], quantity=100, unit="ul")
    alq = stock["aliquots"][0]

    with pytest.raises(LedgerError, match="unit mismatch"):
        ledger.aliquots.use(alq, "1", "ml", message="prep")
    with pytest.raises(LedgerError, match="only"):
        ledger.aliquots.use(alq, "101", "ul", message="prep")
    for bad in ("0", "-1", "nope"):
        with pytest.raises(UsageError, match="positive|decimal"):
            ledger.aliquots.use(alq, bad, "ul", message="prep")

    # Amount and unit come together: only one of them is a usage error.
    with pytest.raises(UsageError, match="must be given together"):
        ledger.aliquots.use(alq, "1", message="prep")
    with pytest.raises(UsageError, match="must be given together"):
        ledger.aliquots.use(alq, unit="ul", message="prep")

    # The message is required and must not be blank.
    with pytest.raises(UsageError, match="message is required"):
        ledger.aliquots.use(alq, "1", "ul", message="   ")

    no_quantity = ledger.stocks.add("lot", entity=item["id"])
    with pytest.raises(LedgerError, match="no quantity"):
        ledger.aliquots.use(no_quantity["aliquots"][0], "1", "ul", message="prep")

    ledger.aliquots.use(alq, "100", "ul", message="used up")
    with pytest.raises(LedgerError, match="already consumed"):
        ledger.aliquots.use(alq, "1", "ul", message="prep")
    with pytest.raises(LedgerError, match="unknown aliquot"):
        ledger.aliquots.use("nope.1", "1", "ul", message="prep")


def test_aliquot_use_whole_equals_an_explicit_use(ledger_factory) -> None:
    """No amount means "use everything left" (spec §6)."""
    ledger = make_ledger(ledger_factory, name="use-whole")
    item = new_item(ledger)
    box = new_box(ledger, "GRID", "grid96")
    first = ledger.stocks.add(
        "lot",
        entity=item["id"],
        aliquots=1,
        box="GRID",
        slots=["A1"],
        quantity=200,
        unit="ml",
    )
    second = ledger.stocks.add(
        "lot",
        entity=item["id"],
        aliquots=1,
        box="GRID",
        slots=["A2"],
        quantity=200,
        unit="ml",
    )
    whole = first["aliquots"][0]
    explicit = second["aliquots"][0]

    result = ledger.aliquots.use(whole, message="used it all")
    assert result["used_amount"] == "200"
    assert result["used_unit"] == "ml"
    assert result["quantity"] == "0.000000"
    assert result["status"] == "consumed"
    assert result["message"] == "used it all"

    # Passing the remainder explicitly is the very same operation.
    other = ledger.aliquots.use(explicit, "200", "ml", message="used it all")
    assert other["used_amount"] == result["used_amount"]
    assert other["used_unit"] == result["used_unit"]
    assert other["quantity"] == result["quantity"]
    assert other["status"] == result["status"]

    whole_event = events(ledger, whole)[-1]
    explicit_event = events(ledger, explicit)[-1]
    assert whole_event["event"] == "use"
    assert Decimal(str(whole_event["amount"])) == Decimal("200")
    assert whole_event["unit"] == "ml"
    assert whole_event["message"] == "used it all"
    assert whole_event["old_status"] == "stored"
    assert whole_event["new_status"] == "consumed"
    # The emptied tube leaves its box; the event keeps the old position.
    assert whole_event["from_box_id"] == box["id"]
    assert whole_event["from_slot"] == "A1"
    assert whole_event["to_box_id"] is None
    assert whole_event["to_slot"] is None
    assert ledger.driver.query(
        "SELECT `box_id`, `slot` FROM `aliquot` WHERE `id` = %s", (whole,)
    ) == [{"box_id": None, "slot": None}]
    # The explicit use records exactly the same event.
    assert Decimal(str(explicit_event["amount"])) == Decimal("200")
    assert explicit_event["unit"] == "ml"
    assert explicit_event["message"] == "used it all"
    assert explicit_event["from_slot"] == "A2"


def test_aliquot_use_without_a_quantity_uses_it_whole(ledger_factory) -> None:
    """A tube with no quantity can only be used whole (event amount NULL)."""
    ledger = make_ledger(ledger_factory, name="use-no-quantity")
    item = new_item(ledger)
    stock = ledger.stocks.add("lot", entity=item["id"], aliquots=1)
    alq = stock["aliquots"][0]
    assert ledger.aliquots.show(alq)["quantity"] is None

    # A partial use of such a tube is refused.
    with pytest.raises(LedgerError, match="no quantity"):
        ledger.aliquots.use(alq, "1", "ul", message="prep")

    result = ledger.aliquots.use(alq, message="used it all")
    assert result["used_amount"] is None
    assert result["used_unit"] is None
    assert result["quantity"] is None
    assert result["status"] == "consumed"
    assert result["message"] == "used it all"

    event = events(ledger, alq)[-1]
    assert event["event"] == "use"
    assert event["amount"] is None
    assert event["message"] == "used it all"
    assert event["old_status"] == "pending"
    assert event["new_status"] == "consumed"


def test_aliquot_use_works_on_every_status(ledger_factory) -> None:
    """``use`` works on every tube; only a consumed tube is refused."""
    ledger = make_ledger(ledger_factory, name="use-statuses")
    item = new_item(ledger)
    new_box(ledger, "GRID", "grid96")
    stock = ledger.stocks.add(
        "lot", entity=item["id"], aliquots=3, quantity=100, unit="ul"
    )
    pending, stored, missing = stock["aliquots"]
    ledger.aliquots.move(stored, box="GRID", slot="A1", message="placed")
    ledger.aliquots.status(missing, "missing")

    for tube, status in (
        (pending, "pending"),
        (stored, "stored"),
        (missing, "missing"),
    ):
        assert ledger.aliquots.show(tube)["status"] == status
        # A partial use changes nothing but the quantity.
        partial = ledger.aliquots.use(tube, "10", "ul", message="assay")
        assert partial["status"] == status
        assert partial["quantity"] == "90.000000"
        # Emptying the tube consumes it, whatever it was before.
        emptied = ledger.aliquots.use(tube, message="the rest")
        assert emptied["status"] == "consumed"

    # Only an already consumed tube is refused.
    with pytest.raises(LedgerError, match="already consumed"):
        ledger.aliquots.use(pending, "1", "ul", message="again")


def test_aliquot_use_message_is_required_trimmed_and_stored(
    ledger_factory,
) -> None:
    """The message is the reason of an irreversible action (spec §1)."""
    ledger = make_ledger(ledger_factory, name="use-message")
    item = new_item(ledger)
    stock = ledger.stocks.add(
        "lot", entity=item["id"], aliquots=1, quantity=100, unit="ul"
    )
    alq = stock["aliquots"][0]

    # The message is a required keyword argument and must not be blank.
    with pytest.raises(TypeError):
        ledger.aliquots.use(alq, "1", "ul")  # type: ignore[call-arg]
    with pytest.raises(UsageError, match="message is required"):
        ledger.aliquots.use(alq, "1", "ul", message="")
    with pytest.raises(UsageError, match="message is required"):
        ledger.aliquots.use(alq, "1", "ul", message=" \t ")

    # The API takes no ``purpose``/``note`` arguments any more.
    assert list(inspect.signature(ledger.aliquots.use).parameters) == [
        "ref",
        "amount",
        "unit",
        "message",
    ]
    with pytest.raises(TypeError):
        ledger.aliquots.use(  # type: ignore[call-arg]
            alq, "1", "ul", message="x", purpose="old"
        )
    with pytest.raises(TypeError):
        ledger.aliquots.use(  # type: ignore[call-arg]
            alq, "1", "ul", message="x", note="old"
        )

    result = ledger.aliquots.use(alq, "1", "ul", message="  for the assay  ")
    assert result["message"] == "for the assay"
    event = events(ledger, alq)[-1]
    assert event["message"] == "for the assay"


def test_aliquot_move_placement_errors(ledger_factory) -> None:
    ledger = make_full_ledger(ledger_factory)
    item = new_item(ledger)
    new_box(ledger, "GRID", "grid96")
    new_box(ledger, "TWO", "grid9x9")
    new_box(ledger, "FREE", "slotlessbox")
    new_box(ledger, "NAMED", "namedbox")
    new_box(ledger, "CHILD", "rack", parent="NAMED", slot="top")
    stock = ledger.stocks.add("lot", entity=item["id"], aliquots=4)

    with pytest.raises(LedgerError, match="needs --slot"):
        ledger.aliquots.move(stock["aliquots"][0], box="GRID", message="moved")
    with pytest.raises(LedgerError, match="has no slot"):
        ledger.aliquots.move(stock["aliquots"][0], box="GRID", slot="Z99", message="moved")
    with pytest.raises(LedgerError, match="no slots"):
        ledger.aliquots.move(stock["aliquots"][0], box="FREE", slot="A1", message="moved")
    with pytest.raises(LedgerError, match="child box"):
        ledger.aliquots.move(stock["aliquots"][0], box="NAMED", slot="top", message="moved")
    with pytest.raises(LedgerError, match="unknown box"):
        ledger.aliquots.move(stock["aliquots"][0], box="NOPE", message="moved")

    # slot_capacity 2 on grid9x9
    ledger.aliquots.move(stock["aliquots"][0], box="TWO", slot="B1", message="moved")
    ledger.aliquots.move(stock["aliquots"][1], box="TWO", slot="B1", message="moved")
    with pytest.raises(LedgerError, match="full"):
        ledger.aliquots.move(stock["aliquots"][2], box="TWO", slot="B1", message="moved")


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

    # A slotless box has no slot requirement; moving restores the in-stock
    # status in the same write (spec 5).
    new_box(ledger, "FREE", "slotlessbox")
    moved = ledger.aliquots.move(alq, box="FREE", message="moved")
    assert moved["status"] == "stored"
    assert moved["old_status"] == "pending"
    assert ledger.driver.query(
        "SELECT `status`, `box_id`, `slot` FROM `aliquot` WHERE `id` = %s", (alq,)
    )[0]["status"] == "stored"


def test_aliquot_status_leaves_stock_clears_position_and_event_keeps_it(
    ledger_factory,
) -> None:
    """Acceptance: consumed clears the position, the event keeps the old one."""
    ledger = make_ledger(ledger_factory, name="clear-position")
    item = new_item(ledger)
    box = new_box(ledger, "GRID", "grid96")
    stock = ledger.stocks.add(
        "lot", entity=item["id"], aliquots=1, box="GRID", slots=["A1"]
    )
    alq = stock["aliquots"][0]

    before = commit_count(ledger)
    changed = ledger.aliquots.status(alq, "missing")
    assert changed["status"] == "missing"
    # The status and the cleared position are one write, one commit.
    assert commit_count(ledger) == before + 1
    assert ledger.driver.query(
        "SELECT `status`, `box_id`, `slot` FROM `aliquot` WHERE `id` = %s", (alq,)
    ) == [{"status": "missing", "box_id": None, "slot": None}]

    last = events(ledger, alq)[-1]
    assert last["event"] == "status"
    assert last["from_box_id"] == box["id"]
    assert last["from_slot"] == "A1"
    assert last["to_box_id"] is None
    assert last["to_slot"] is None
    assert last["old_status"] == "stored"
    assert last["new_status"] == "missing"

    # Going back to an in-stock status without a position is refused with the
    # move hint, and writes nothing.
    before = commit_count(ledger)
    with pytest.raises(LedgerError, match="tm aliquot move"):
        ledger.aliquots.status(alq, "stored")
    assert commit_count(ledger) == before
    assert ledger.driver.query(
        "SELECT `status` FROM `aliquot` WHERE `id` = %s", (alq,)
    ) == [{"status": "missing"}]


def test_aliquot_use_to_zero_clears_the_position(ledger_factory) -> None:
    ledger = make_ledger(ledger_factory, name="use-clears")
    item = new_item(ledger)
    box = new_box(ledger, "GRID", "grid96")
    stock = ledger.stocks.add(
        "lot",
        entity=item["id"],
        aliquots=1,
        box="GRID",
        slots=["B2"],
        quantity=10,
        unit="ul",
    )
    alq = stock["aliquots"][0]

    # A partial use keeps the position.
    ledger.aliquots.use(alq, "4", "ul", message="prep")
    assert ledger.driver.query(
        "SELECT `box_id`, `slot`, `status` FROM `aliquot` WHERE `id` = %s", (alq,)
    ) == [{"box_id": box["id"], "slot": "B2", "status": "stored"}]

    before = commit_count(ledger)
    emptied = ledger.aliquots.use(alq, "6", "ul", message="used up")
    assert emptied["status"] == "consumed"
    assert commit_count(ledger) == before + 1
    assert ledger.driver.query(
        "SELECT `box_id`, `slot`, `status` FROM `aliquot` WHERE `id` = %s", (alq,)
    ) == [{"box_id": None, "slot": None, "status": "consumed"}]

    last = events(ledger, alq)[-1]
    assert last["event"] == "use"
    assert last["from_box_id"] == box["id"]
    assert last["from_slot"] == "B2"
    assert last["to_box_id"] is None
    assert last["to_slot"] is None
    assert last["old_status"] == "stored"
    assert last["new_status"] == "consumed"


def test_aliquot_move_restores_the_single_in_stock_status(ledger_factory) -> None:
    ledger = make_ledger(ledger_factory, name="move-restores")
    item = new_item(ledger)
    box = new_box(ledger, "GRID", "grid96")
    stock = ledger.stocks.add("lot", entity=item["id"], aliquots=1)
    alq = stock["aliquots"][0]
    assert ledger.driver.query(
        "SELECT `status`, `box_id` FROM `aliquot` WHERE `id` = %s", (alq,)
    ) == [{"status": "pending", "box_id": None}]

    moved = ledger.aliquots.move(alq, box="GRID", slot="A1", message="moved")
    assert moved["old_status"] == "pending"
    assert moved["status"] == "stored"
    assert moved["box_id"] == box["id"] and moved["slot"] == "A1"
    assert ledger.driver.query(
        "SELECT `status`, `box_id`, `slot` FROM `aliquot` WHERE `id` = %s", (alq,)
    ) == [{"status": "stored", "box_id": box["id"], "slot": "A1"}]
    last = events(ledger, alq)[-1]
    assert last["event"] == "move"
    assert last["old_status"] == "pending"
    assert last["new_status"] == "stored"
    assert last["to_slot"] == "A1"

    # An aliquot already in stock keeps its status on a plain move.
    again = ledger.aliquots.move(alq, box="GRID", slot="A2", message="moved")
    assert again["old_status"] == "stored" and again["status"] == "stored"


def test_aliquot_move_requires_and_records_a_trimmed_message(
    ledger_factory,
) -> None:
    """The move message is required, trimmed, and stored in the event note."""
    ledger = make_ledger(ledger_factory, name="move-message")
    item = new_item(ledger)
    box = new_box(ledger, "GRID", "grid96")
    stock = ledger.stocks.add("lot", entity=item["id"], aliquots=1)
    alq = stock["aliquots"][0]

    before = commit_count(ledger)
    for blank in ("", "   ", "\t\n "):
        with pytest.raises(UsageError, match="message"):
            ledger.aliquots.move(alq, box="GRID", slot="A1", message=blank)
    assert commit_count(ledger) == before
    assert ledger.driver.query(
        "SELECT `status`, `box_id`, `slot` FROM `aliquot` WHERE `id` = %s", (alq,)
    ) == [{"status": "pending", "box_id": None, "slot": None}]

    moved = ledger.aliquots.move(
        alq, box="GRID", slot="A1", message="  moved to the cold room  "
    )
    assert moved["old_status"] == "pending"
    assert moved["status"] == "stored"
    assert moved["box_id"] == box["id"] and moved["slot"] == "A1"

    event = events(ledger, alq)[-1]
    assert event["event"] == "move"
    assert event["message"] == "moved to the cold room"
    assert event["from_slot"] is None and event["to_slot"] == "A1"

    history = ledger.history(alq)
    assert history["events"][-1]["event"] == "move"
    assert history["events"][-1]["message"] == "moved to the cold room"


def test_aliquot_move_with_several_in_stock_statuses_needs_status(
    ledger_factory,
) -> None:
    # The repository presets are the spec-6 fixed model (one in-stock status),
    # so a kind with two in-stock statuses only exists in a legacy closure.
    from conftest import PRESETS

    ledger = ledger_factory(
        [PRESETS / "legacy"], name="worm-statuses"
    )
    strain = ledger.entities.add("worm_strain", name="N2")
    new_box(ledger, "GRID", "grid9x9")
    # Creation is not a transition, so a non-in-stock status starts unplaced.
    stock = ledger.stocks.add(
        "worm_culture", entity=strain["id"], aliquots=1, status="contaminated"
    )
    alq = stock["aliquots"][0]

    # ``worm`` has two in-stock statuses: the bare move is ambiguous.
    with pytest.raises(LedgerError, match="several in-stock statuses"):
        ledger.aliquots.move(alq, box="GRID", slot="A1", message="moved")
    with pytest.raises(UsageError, match="not in stock"):
        ledger.aliquots.move(
            alq, box="GRID", slot="A1", status="contaminated", message="moved"
        )
    with pytest.raises(UsageError, match="unknown status"):
        ledger.aliquots.move(alq, box="GRID", slot="A1", status="nope", message="moved")

    # --status picks the in-stock status to restore; no transition check runs
    # for a tube that was not in stock.
    restored = ledger.aliquots.move(
        alq, box="GRID", slot="A1", status="growing", message="moved"
    )
    assert restored["old_status"] == "contaminated"
    assert restored["status"] == "growing"
    assert restored["slot"] == "A1"

    # An in-stock move with --status follows the normal transition rule.
    starred = ledger.aliquots.move(
        alq, box="GRID", slot="A2", status="starved", message="moved"
    )
    assert starred["old_status"] == "growing"
    assert starred["status"] == "starved"
    # Naming the current status is a plain move, not a transition error.
    same = ledger.aliquots.move(
        alq, box="GRID", slot="A3", status="starved", message="moved"
    )
    assert same["status"] == "starved"


def test_aliquot_creation_enforces_the_position_invariant(ledger_factory) -> None:
    ledger = make_ledger(ledger_factory, name="creation-position")
    item = new_item(ledger)
    new_box(ledger, "GRID", "grid96")

    # An in-stock creation status needs a position...
    before = commit_count(ledger)
    with pytest.raises(LedgerError, match="must be placed"):
        ledger.stocks.add("lot", entity=item["id"], aliquots=1, status="stored")
    assert commit_count(ledger) == before
    # ...and a placed aliquot needs an in-stock status.
    with pytest.raises(LedgerError, match="placed aliquot must be in stock"):
        ledger.stocks.add(
            "lot",
            entity=item["id"],
            aliquots=1,
            box="GRID",
            slots=["A1"],
            status="pending",
        )
    stock = ledger.stocks.add(
        "lot", entity=item["id"], aliquots=1, status="missing"
    )
    with pytest.raises(LedgerError, match="placed aliquot must be in stock"):
        ledger.aliquots.add(
            stock["id"], count=1, box="GRID", slots=["A1"], status="consumed"
        )
    with pytest.raises(LedgerError, match="must be placed"):
        ledger.aliquots.add(stock["id"], count=1, status="stored")

    assert ledger.driver.query(
        "SELECT `status`, `box_id` FROM `aliquot` WHERE `id` = %s",
        (stock["aliquots"][0],),
    ) == [{"status": "missing", "box_id": None}]
    # A placed creation defaults to the in-stock status; that is valid.
    placed = ledger.aliquots.add(stock["id"], count=1, box="GRID", slots=["A1"])
    assert ledger.driver.query(
        "SELECT `status`, `slot` FROM `aliquot` WHERE `id` = %s",
        (placed["ids"][0],),
    ) == [{"status": "stored", "slot": "A1"}]


def test_aliquot_purge_removes_events(ledger_factory) -> None:
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
    ledger.aliquots.use(alq, "10", "ul", message="prep")
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


def test_aliquot_purge_removes_its_aliases(ledger_factory) -> None:
    ledger = make_ledger(ledger_factory)
    item = new_item(ledger)
    stock = ledger.stocks.add("lot", entity=item["id"])
    alq = stock["aliquots"][0]
    ledger.aliases.add("aliquot", alq, "tube-one")

    before = commit_count(ledger)
    purged = ledger.aliquots.purge(alq)

    assert commit_count(ledger) == before + 1
    assert purged["aliases"] == 1
    assert ledger.driver.query(
        "SELECT `alias` FROM `alias` WHERE `object_type` = 'aliquot' "
        "AND `object_id` = %s",
        (alq,),
    ) == []
    assert ledger.driver.query(
        "SELECT `id` FROM `aliquot` WHERE `id` = %s", (alq,)
    ) == []


def test_purge_refuses_a_kind_ref_to_an_aliquot(ledger_factory) -> None:
    """A stock kind's ``ref`` to an aliquot is a reference, not a cascade."""
    ledger = ledger_factory([MOUSE], name="purge-aliquot-kind-ref")
    strain = ledger.entities.add("mouse_strain", name="C57BL/6J")
    source = ledger.stocks.add("mouse_cohort", entity=strain["id"])
    dam = source["aliquots"][0]
    child = ledger.stocks.add(
        "mouse_cohort", entity=strain["id"], values={"dam": dam}
    )

    before = commit_count(ledger)
    with pytest.raises(LedgerError, match="mouse_cohort.dam"):
        ledger.aliquots.purge(dam)
    assert commit_count(ledger) == before
    assert ledger.driver.query(
        "SELECT `id` FROM `aliquot` WHERE `id` = %s", (dam,)
    )
    # The owning stock is refused for the same kind ref until it is cleared.
    with pytest.raises(LedgerError, match="mouse_cohort.dam"):
        ledger.stocks.purge(source["id"])
    ledger.stocks.edit(child["id"], values={"dam": child["aliquots"][0]})
    assert ledger.stocks.purge(source["id"])["aliquots"] == 1


# ---------------------------------------------------------------------------
# Entity / stock / box purge
# ---------------------------------------------------------------------------


def _view_exists(ledger, name: str) -> bool:
    rows = ledger.driver.query(
        "SELECT table_name AS name FROM information_schema.views "
        "WHERE table_schema = DATABASE() AND LOWER(table_name) = %s",
        (name.lower(),),
    )
    return bool(rows)


def test_entity_purge_removes_kind_row_and_aliases(ledger_factory) -> None:
    ledger = make_full_ledger(ledger_factory)
    entity = ledger.entities.add("everything", name="mistake")
    ledger.aliases.add("entity", entity["id"], "oops")

    before = commit_count(ledger)
    purged = ledger.entities.purge(entity["id"])

    assert commit_count(ledger) == before + 1
    assert purged == {
        "object": "entity",
        "id": entity["id"],
        "kind": "everything",
        "purged": True,
        "aliases": 1,
    }
    for table in ("entity", "entity_everything"):
        assert ledger.driver.query(
            f"SELECT `id` FROM `{table}` WHERE `id` = %s", (entity["id"],)
        ) == []
    assert ledger.driver.query(
        "SELECT `alias` FROM `alias` WHERE `object_type` = 'entity' "
        "AND `object_id` = %s",
        (entity["id"],),
    ) == []
    assert ledger.check().passed


def test_entity_purge_refuses_a_stock(ledger_factory) -> None:
    ledger = make_ledger(ledger_factory)
    item = new_item(ledger)
    stock = ledger.stocks.add("lot", entity=item["id"])

    before = commit_count(ledger)
    with pytest.raises(LedgerError, match="purge its stocks first") as caught:
        ledger.entities.purge(item["id"])

    assert stock["id"] in str(caught.value)
    assert commit_count(ledger) == before
    assert ledger.driver.query(
        "SELECT `id` FROM `entity` WHERE `id` = %s", (item["id"],)
    )
    assert ledger.driver.query(
        "SELECT `id` FROM `stock` WHERE `id` = %s", (stock["id"],)
    )


def test_entity_purge_refuses_a_kind_ref_field(ledger_factory) -> None:
    ledger = make_full_ledger(ledger_factory)
    parent = ledger.entities.add("everything", name="parent")
    ledger.entities.add(
        "everything", name="child", values={"parent": parent["id"]}
    )

    before = commit_count(ledger)
    with pytest.raises(LedgerError, match="everything.parent") as caught:
        ledger.entities.purge(parent["id"])
    assert "tm entity edit" in str(caught.value)
    assert commit_count(ledger) == before
    assert ledger.driver.query(
        "SELECT `id` FROM `entity` WHERE `id` = %s", (parent["id"],)
    )


def test_stock_purge_cascades_to_its_aliquots(ledger_factory) -> None:
    ledger = make_ledger(ledger_factory)
    item = new_item(ledger)
    box = new_box(ledger, "BOX", "grid96")
    stock = ledger.stocks.add(
        "lot",
        entity=item["id"],
        aliquots=2,
        box="BOX",
        slots=["A1", "A2"],
        quantity=10,
        unit="ul",
    )
    first, second = stock["aliquots"]
    ledger.aliquots.use(first, "1", "ul", message="prep")
    ledger.aliases.add("stock", stock["id"], "lot-one")
    ledger.aliases.add("aliquot", second, "tube-two")
    removed_events = len(events(ledger, first)) + len(events(ledger, second))
    assert removed_events >= 3

    before = commit_count(ledger)
    purged = ledger.stocks.purge(stock["id"])

    assert commit_count(ledger) == before + 1
    assert purged["object"] == "stock"
    assert purged["kind"] == "lot"
    assert purged["aliquots"] == 2
    assert purged["events"] == removed_events
    assert purged["aliases"] == 1
    assert ledger.driver.query(
        "SELECT `id` FROM `stock` WHERE `id` = %s", (stock["id"],)
    ) == []
    assert ledger.driver.query(
        "SELECT `id` FROM `stock_lot` WHERE `id` = %s", (stock["id"],)
    ) == []
    assert ledger.driver.query(
        "SELECT `id` FROM `aliquot` WHERE `stock_id` = %s", (stock["id"],)
    ) == []
    assert (
        sum(len(events(ledger, alq)) for alq in (first, second)) == 0
    )
    assert ledger.driver.query(
        "SELECT `alias` FROM `alias` WHERE "
        "(`object_type` = 'stock' AND `object_id` = %s) OR "
        "(`object_type` = 'aliquot' AND `object_id` = %s)",
        (stock["id"], second),
    ) == []
    # The box and its slots are untouched: purge never empties a box.
    assert ledger.driver.query(
        "SELECT `box_id` FROM `slot` WHERE `box_id` = %s", (box["id"],)
    )
    assert ledger.check().passed


def test_stock_purge_refuses_a_derived_stock(ledger_factory) -> None:
    ledger = make_ledger(ledger_factory)
    item = new_item(ledger)
    first = ledger.stocks.add("lot", entity=item["id"])
    second = ledger.stocks.add("lot", entity=item["id"], parent=first["id"])

    before = commit_count(ledger)
    with pytest.raises(LedgerError, match="parent_stock_id") as caught:
        ledger.stocks.purge(first["id"])
    assert second["id"] in str(caught.value)
    assert commit_count(ledger) == before
    assert ledger.driver.query(
        "SELECT `id` FROM `stock` WHERE `id` = %s", (first["id"],)
    )


def test_stock_purge_refuses_a_consuming_stock(ledger_factory) -> None:
    ledger = make_ledger(ledger_factory)
    item = new_item(ledger)
    first = ledger.stocks.add("lot", entity=item["id"])
    second = ledger.stocks.add(
        "lot", entity=item["id"], source_aliquot=first["aliquots"][0]
    )

    before = commit_count(ledger)
    with pytest.raises(LedgerError, match="source_aliquot_id") as caught:
        ledger.stocks.purge(first["id"])
    assert second["id"] in str(caught.value)
    assert commit_count(ledger) == before
    assert ledger.driver.query(
        "SELECT `id` FROM `aliquot` WHERE `id` = %s", (first["aliquots"][0],)
    )


def test_box_purge_removes_slots_kind_row_aliases_and_view(ledger_factory) -> None:
    ledger = make_ledger(ledger_factory)
    box = new_box(ledger, "BOX", "grid96")
    ledger.aliases.add("box", box["id"], "b1")
    assert _view_exists(ledger, "v_box_BOX")

    before = commit_count(ledger)
    purged = ledger.boxes.purge("BOX")

    assert commit_count(ledger) == before + 1
    assert purged == {
        "object": "box",
        "id": box["id"],
        "kind": "grid96",
        "name": "BOX",
        "purged": True,
        "slots": 96,
        "aliases": 1,
    }
    for table in ("box", "box_grid96"):
        assert ledger.driver.query(
            f"SELECT `id` FROM `{table}` WHERE `id` = %s", (box["id"],)
        ) == []
    assert ledger.driver.query(
        "SELECT `label` FROM `slot` WHERE `box_id` = %s", (box["id"],)
    ) == []
    assert ledger.driver.query(
        "SELECT `alias` FROM `alias` WHERE `object_type` = 'box' "
        "AND `object_id` = %s",
        (box["id"],),
    ) == []
    assert not _view_exists(ledger, "v_box_BOX")
    assert ledger.check().passed


def test_box_purge_refuses_a_held_aliquot(ledger_factory) -> None:
    ledger = make_ledger(ledger_factory)
    box = new_box(ledger, "BOX", "grid96")
    item = new_item(ledger)
    ledger.stocks.add(
        "lot", entity=item["id"], box="BOX", slots=["A1"], quantity=1, unit="ml"
    )

    before = commit_count(ledger)
    with pytest.raises(LedgerError, match="tm aliquot move") as caught:
        ledger.boxes.purge("BOX")
    assert "tm box purge" in str(caught.value)
    assert commit_count(ledger) == before
    assert ledger.driver.query(
        "SELECT `id` FROM `box` WHERE `id` = %s", (box["id"],)
    )
    assert ledger.driver.query(
        "SELECT `box_id` FROM `slot` WHERE `box_id` = %s", (box["id"],)
    )
    assert _view_exists(ledger, "v_box_BOX")


def test_box_purge_refuses_a_child_box(ledger_factory) -> None:
    ledger = make_ledger(ledger_factory)
    parent = ledger.boxes.add("shelf", "SHELF")
    child = ledger.boxes.add("rack", "RACK", parent="SHELF")

    before = commit_count(ledger)
    with pytest.raises(LedgerError, match="parent_box_id") as caught:
        ledger.boxes.purge("SHELF")
    assert child["id"] in str(caught.value)
    assert "tm box move" in str(caught.value)
    assert commit_count(ledger) == before
    assert ledger.driver.query(
        "SELECT `id` FROM `box` WHERE `id` = %s", (parent["id"],)
    )


def _ref_preset(tmp_path):
    """A minimal ledger whose kinds carry ``ref`` fields at each target form."""
    path = tmp_path / "purge_refs.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "preset": "purge_refs",
                "version": 1,
                "mixins": {
                    "named_fields": {
                        "name": {"type": "string", "required": True, "unique": True}
                    }
                },
                "kinds": {
                    "item": {"object": "entity", "mixins": ["named_fields"]},
                    "lot": {"object": "stock", "entity_kinds": ["item"]},
                    "lot_holder": {
                        "object": "stock",
                        "entity_kinds": ["item"],
                        "fields": {
                            "parent_lot": {"type": "ref", "to": "lot"}
                        },
                    },
                    "rack": {
                        "object": "box",
                        "geometry": {"type": "named", "slots": ["1", "2", "3"]},
                    },
                    "box_holder": {
                        "object": "box",
                        "geometry": {"type": "slotless", "capacity": 2},
                        "fields": {
                            "item_ref": {"type": "ref", "to": "item"},
                            "rack_ref": {"type": "ref", "to": "rack"},
                        },
                    },
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def test_purge_refuses_a_box_kind_ref_with_a_real_remedy(
    tmp_path, ledger_factory
) -> None:
    """A ref field on a box kind blocks the target, but no ``tm box edit`` exists."""
    ledger = ledger_factory(
        [_ref_preset(tmp_path)], name="purge-box-kind-ref", real_init=True
    )
    item = ledger.entities.add("item", name="target")
    stock = ledger.stocks.add("lot", entity=item["id"])
    holder = ledger.boxes.add("box_holder", "HOLDER")
    ledger.driver.execute(
        "UPDATE `box_box_holder` SET `item_ref` = %s WHERE `id` = %s",
        (item["id"], holder["id"]),
    )

    before = commit_count(ledger)
    with pytest.raises(LedgerError, match="box_holder.item_ref") as caught:
        ledger.entities.purge(item["id"])
    message = str(caught.value)
    # Mixed referents: both remedies are stated up front.
    assert f"tm stock purge {stock['id']}" in message
    assert "tm box edit" not in message
    assert "tm box purge" in message
    assert commit_count(ledger) == before
    assert ledger.driver.query(
        "SELECT `id` FROM `entity` WHERE `id` = %s", (item["id"],)
    )


def test_box_purge_refuses_a_box_kind_ref(tmp_path, ledger_factory) -> None:
    ledger = ledger_factory(
        [_ref_preset(tmp_path)], name="purge-box-ref", real_init=True
    )
    target = ledger.boxes.add("rack", "TARGET")
    holder = ledger.boxes.add("box_holder", "HOLDER")
    ledger.driver.execute(
        "UPDATE `box_box_holder` SET `rack_ref` = %s WHERE `id` = %s",
        (target["id"], holder["id"]),
    )

    before = commit_count(ledger)
    with pytest.raises(LedgerError, match="box_holder.rack_ref") as caught:
        ledger.boxes.purge("TARGET")
    message = str(caught.value)
    assert "tm box edit" not in message
    assert holder["id"] in message
    assert commit_count(ledger) == before
    assert ledger.driver.query(
        "SELECT `id` FROM `box` WHERE `id` = %s", (target["id"],)
    )


def test_stock_purge_refuses_a_kind_ref_to_another_stock(
    tmp_path, ledger_factory
) -> None:
    ledger = ledger_factory(
        [_ref_preset(tmp_path)], name="purge-stock-ref", real_init=True
    )
    item = ledger.entities.add("item", name="target")
    parent = ledger.stocks.add("lot", entity=item["id"])
    holder = ledger.stocks.add("lot_holder", entity=item["id"])
    ledger.driver.execute(
        "UPDATE `stock_lot_holder` SET `parent_lot` = %s WHERE `id` = %s",
        (parent["id"], holder["id"]),
    )

    before = commit_count(ledger)
    with pytest.raises(LedgerError, match="lot_holder.parent_lot") as caught:
        ledger.stocks.purge(parent["id"])
    message = str(caught.value)
    assert f"tm stock edit {holder['id']}" in message
    assert f"tm stock purge {holder['id']}" in message
    assert commit_count(ledger) == before
    assert ledger.driver.query(
        "SELECT `id` FROM `stock` WHERE `id` = %s", (parent["id"],)
    )


def test_purge_refusal_caps_a_long_referent_list(ledger_factory) -> None:
    ledger = make_ledger(ledger_factory)
    item = new_item(ledger)
    new_box(ledger, "BIG", "grid96")
    ledger.stocks.add(
        "lot",
        entity=item["id"],
        aliquots=12,
        box="BIG",
        slots=[f"A{number}" for number in range(1, 13)],
    )

    with pytest.raises(LedgerError, match="and 2 more") as caught:
        ledger.boxes.purge("BIG")
    assert "12 other objects still reference it" in str(caught.value)


def test_box_purge_keeps_aliquot_event_box_ids(ledger_factory) -> None:
    """An event's old position is history, not a foreign key (spec §6)."""
    ledger = make_ledger(ledger_factory)
    source = new_box(ledger, "SOURCE", "grid96")
    new_box(ledger, "TARGET", "grid96")
    item = new_item(ledger)
    stock = ledger.stocks.add(
        "lot", entity=item["id"], box="SOURCE", slots=["A1"]
    )
    ledger.aliquots.move(
        stock["aliquots"][0], box="TARGET", slot="A2", message="relocated"
    )

    ledger.boxes.purge("SOURCE")

    events = ledger.driver.query(
        "SELECT `from_box_id` FROM `aliquot_event` WHERE `from_box_id` = %s",
        (source["id"],),
    )
    assert events, "the move event keeps the old box id"
    assert ledger.driver.query(
        "SELECT `id` FROM `box` WHERE `id` = %s", (source["id"],)
    ) == []
    assert ledger.check().passed


def test_purge_accepts_alias_and_unique_prefix_refs(ledger_factory) -> None:
    ledger = make_ledger(ledger_factory)
    item = new_item(ledger)
    ledger.aliases.add("entity", item["id"], "alias-item")
    stock = ledger.stocks.add("lot", entity=item["id"])
    alq = stock["aliquots"][0]
    ledger.aliases.add("aliquot", alq, "tube-alias")
    box = new_box(ledger, "BOX", "rack")
    ledger.aliases.add("box", box["id"], "box-alias")

    assert ledger.aliquots.purge("tube-alias")["id"] == alq
    assert ledger.stocks.purge(stock["id"][:5])["id"] == stock["id"]
    assert ledger.entities.purge("alias-item")["id"] == item["id"]
    assert ledger.boxes.purge("box-alias")["id"] == box["id"]


def test_purge_honours_no_commit(ledger_factory) -> None:
    ledger = make_ledger(ledger_factory)
    item = new_item(ledger)
    before = commit_count(ledger)

    ledger.no_commit = True
    try:
        assert ledger.entities.purge(item["id"])["purged"] is True
    finally:
        ledger.no_commit = False

    assert commit_count(ledger) == before
    assert dirty(ledger), "the deletion is executed but left uncommitted"


def test_purge_dry_run_writes_nothing(ledger_factory) -> None:
    ledger = make_ledger(ledger_factory)
    item = new_item(ledger)
    box = new_box(ledger, "BOX", "grid96")
    stock = ledger.stocks.add("lot", entity=item["id"], box="BOX", slots=["A1"])
    spare_entity = ledger.entities.add("item", name="spare")
    spare_box = ledger.boxes.add("rack", "SPARE")
    before = commit_count(ledger)

    ledger.dry_run = True
    try:
        entity_result = ledger.entities.purge(spare_entity["id"])
        stock_result = ledger.stocks.purge(stock["id"])
        box_result = ledger.boxes.purge("SPARE")
        assert entity_result["purged"] and stock_result["purged"]
        assert box_result["purged"]
        assert commit_count(ledger) == before
        assert dirty(ledger) == []
        assert ledger.last_write is not None
        assert "DELETE FROM `box`" in ledger.last_write.sql
        assert "DROP VIEW IF EXISTS `v_box_SPARE`" in ledger.last_write.sql
    finally:
        ledger.dry_run = False

    assert ledger.driver.query(
        "SELECT `id` FROM `entity` WHERE `id` = %s", (spare_entity["id"],)
    )
    assert ledger.driver.query(
        "SELECT `id` FROM `stock` WHERE `id` = %s", (stock["id"],)
    )
    assert ledger.driver.query(
        "SELECT `id` FROM `box` WHERE `id` = %s", (spare_box["id"],)
    )
    assert ledger.driver.query(
        "SELECT `box_id` FROM `slot` WHERE `box_id` = %s", (spare_box["id"],)
    )
    assert _view_exists(ledger, "v_box_SPARE")
    assert box["id"] != spare_box["id"]



# ---------------------------------------------------------------------------
# Aliquot reads
# ---------------------------------------------------------------------------


def test_aliquot_show_reads_the_tube_its_stock_and_its_path(
    ledger_factory,
) -> None:
    """``show`` answers the questions about one tube in one read."""
    ledger = make_ledger(ledger_factory)
    item = new_item(ledger)
    new_box(ledger, "BOX", "grid96")
    stock = ledger.stocks.add("lot", entity=item["id"], quantity=100, unit="ul")
    alq = stock["aliquots"][0]
    ledger.aliquots.move(alq, box="BOX", slot="A3", message="moved")

    shown = ledger.aliquots.show(alq)

    assert shown["id"] == alq
    assert shown["stock_id"] == stock["id"]
    assert shown["seq"] == 1
    assert shown["status"] == "stored"
    assert Decimal(str(shown["quantity"])) == Decimal("100")
    assert shown["unit"] == "ul"
    assert shown["label"] is None
    assert shown["notes"] is None
    assert shown["created_at"] is not None
    assert shown["aliases"] == []
    assert shown["stock_kind"] == "lot"
    assert shown["entity_id"] == item["id"]
    assert shown["box_id"] is not None
    assert shown["slot"] == "A3"
    assert shown["path"] == "BOX:A3"
    assert shown["in_stock"] is True
    assert shown["object"] == "aliquot"


def test_aliquot_show_of_a_fresh_tube_carries_its_create_event(
    ledger_factory,
) -> None:
    """A tube that was only created has exactly one event: its creation."""
    ledger = make_ledger(ledger_factory)
    item = new_item(ledger)
    new_box(ledger, "BOX", "grid96")
    stock = ledger.stocks.add(
        "lot", entity=item["id"], quantity=100, unit="ul", box="BOX", slots=["A3"]
    )
    alq = stock["aliquots"][0]

    shown = ledger.aliquots.show(alq)

    assert len(shown["events"]) == 1
    create = shown["events"][0]
    assert create["event"] == "create"
    assert Decimal(str(create["amount"])) == Decimal("100")
    assert create["unit"] == "ul"
    assert create["from_box"] is None
    assert create["from_box_id"] is None
    assert create["to_box"] == "BOX"
    assert create["to_slot"] == "A3"
    assert create["old_status"] is None
    assert create["new_status"] == "stored"
    assert create["forced"] is False


def test_aliquot_show_carries_the_tube_event_trail(ledger_factory) -> None:
    """The record's events are the tube's trail, oldest first, boxes named."""
    ledger = make_ledger(ledger_factory)
    item = new_item(ledger)
    new_box(ledger, "BOX", "grid96")
    new_box(ledger, "OTHER", "grid96")
    stock = ledger.stocks.add(
        "lot", entity=item["id"], quantity=100, unit="ul", box="BOX", slots=["A3"]
    )
    alq = stock["aliquots"][0]
    ledger.aliquots.move(alq, box="OTHER", slot="B1", message="moved")
    ledger.aliquots.use(alq, "50", "ul", message="used some")
    ledger.aliquots.status(alq, "missing", message="nowhere")

    shown = ledger.aliquots.show(alq)

    assert [event["event"] for event in shown["events"]] == [
        "create",
        "move",
        "use",
        "status",
    ]
    create, move, use, status = shown["events"]
    assert create["to_box"] == "BOX" and create["to_slot"] == "A3"
    assert create["new_status"] == "stored"
    assert move["from_box"] == "BOX" and move["from_slot"] == "A3"
    assert move["to_box"] == "OTHER" and move["to_slot"] == "B1"
    assert move["old_status"] == "stored" and move["new_status"] == "stored"
    assert move["message"] == "moved"
    assert Decimal(str(use["amount"])) == Decimal("50") and use["unit"] == "ul"
    assert use["old_status"] == "stored" and use["new_status"] == "stored"
    assert use["message"] == "used some"
    assert status["from_box"] == "OTHER" and status["from_slot"] == "B1"
    assert status["old_status"] == "stored" and status["new_status"] == "missing"
    assert status["forced"] is False
    # Every event carries the shape the record promises.
    assert set(create) == {
        "id",
        "event",
        "at",
        "amount",
        "unit",
        "from_box_id",
        "from_box",
        "from_slot",
        "to_box_id",
        "to_box",
        "to_slot",
        "old_status",
        "new_status",
        "forced",
        "actor",
        "message",
    }


def test_aliquot_list_records_carry_no_events(ledger_factory) -> None:
    """A list never reads a tube's event trail: the rows stay flat."""
    ledger = make_ledger(ledger_factory)
    item = new_item(ledger)
    new_box(ledger, "BOX", "grid96")
    stock = ledger.stocks.add("lot", entity=item["id"], quantity=100, unit="ul")
    alq = stock["aliquots"][0]
    ledger.aliquots.move(alq, box="BOX", slot="A3", message="moved")

    rows = ledger.aliquots.ls(all_statuses=True)

    assert len(rows) == 1
    assert "events" not in rows[0]
    assert rows[0]["id"] == alq
    # The show record of the same tube does carry them.
    assert ledger.aliquots.show(alq)["events"]


def test_aliquot_show_of_a_tube_that_is_not_in_stock(ledger_factory) -> None:
    """A tube that left the stock has no position and no path."""
    ledger = make_ledger(ledger_factory)
    item = new_item(ledger)
    stock = ledger.stocks.add("lot", entity=item["id"])
    alq = stock["aliquots"][0]

    shown = ledger.aliquots.show(alq)

    assert shown["status"] == "pending"
    assert shown["in_stock"] is False
    assert shown["box_id"] is None
    assert shown["slot"] is None
    assert shown["path"] is None


def test_aliquot_show_resolves_an_alias(ledger_factory) -> None:
    """A tube is found by its alias, like every other object."""
    ledger = make_ledger(ledger_factory)
    item = new_item(ledger)
    stock = ledger.stocks.add("lot", entity=item["id"])
    alq = stock["aliquots"][0]
    ledger.aliases.add("aliquot", alq, "tube-1")

    shown = ledger.aliquots.show("tube-1")

    assert shown["id"] == alq
    assert shown["aliases"] == ["tube-1"]


def test_aliquot_show_resolves_the_old_id_of_a_renumbered_tube(
    ledger_factory,
) -> None:
    """``tm stock renumber`` keeps the old tube id as an alias; it still shows."""
    ledger = make_ledger(ledger_factory)
    item = new_item(ledger)
    stock = ledger.stocks.add("lot", entity=item["id"])
    alq = stock["aliquots"][0]

    renamed = ledger.stocks.renumber([stock["id"]], auto=True)
    new_stock = renamed["renames"][0]["new"]
    new_alq = f"{new_stock}.1"
    assert new_alq != alq

    shown = ledger.aliquots.show(alq)

    assert shown["id"] == new_alq
    assert shown["stock_id"] == new_stock
    assert alq in shown["aliases"]


def test_aliquot_show_refuses_an_unknown_or_empty_ref(ledger_factory) -> None:
    """An unknown or empty ref is a not-found error, like every other show."""
    ledger = make_ledger(ledger_factory)
    item = new_item(ledger)
    stock = ledger.stocks.add("lot", entity=item["id"])

    with pytest.raises(LedgerError, match="unknown aliquot 'nope'"):
        ledger.aliquots.show("nope")
    with pytest.raises(LedgerError, match="unknown aliquot ''"):
        ledger.aliquots.show("")
    # A prefix of an aliquot id is not a handle: the id is never shortened.
    with pytest.raises(LedgerError, match="unknown aliquot"):
        ledger.aliquots.show(stock["aliquots"][0][:6])


# ---------------------------------------------------------------------------
# Wrong-type hints: the not-found error names the other show verb (spec §6)
# ---------------------------------------------------------------------------


def test_aliquot_show_of_a_stock_hints_at_stock_show(ledger_factory) -> None:
    """``tm aliquot show <stock>`` says the ref is a stock, by id or alias."""
    ledger = make_ledger(ledger_factory)
    item = new_item(ledger)
    stock = ledger.stocks.add("lot", entity=item["id"])
    ledger.aliases.add("stock", stock["id"], "LOT1")

    with pytest.raises(LedgerError) as by_id:
        ledger.aliquots.show(stock["id"])
    assert str(by_id.value) == (
        f"unknown aliquot {stock['id']!r}: {stock['id']} is a stock; "
        f"try tm stock show {stock['id']}"
    )
    # The not-found exit code is unchanged: a business error, not usage.
    assert by_id.value.exit_code == 1

    with pytest.raises(LedgerError) as by_alias:
        ledger.aliquots.show("LOT1")
    assert str(by_alias.value) == (
        "unknown aliquot 'LOT1': LOT1 is a stock; try tm stock show LOT1"
    )
    assert by_alias.value.exit_code == 1


def test_stock_show_of_an_aliquot_hints_at_aliquot_show(ledger_factory) -> None:
    """``tm stock show <aliquot>`` says the ref is an aliquot, by id or alias."""
    ledger = make_ledger(ledger_factory)
    item = new_item(ledger)
    stock = ledger.stocks.add("lot", entity=item["id"])
    alq = stock["aliquots"][0]
    ledger.aliases.add("aliquot", alq, "tube-1")

    with pytest.raises(LedgerError) as by_id:
        ledger.stocks.show(alq)
    assert str(by_id.value) == (
        f"unknown stock {alq!r}: {alq} is an aliquot; "
        f"try tm aliquot show {alq}"
    )
    assert by_id.value.exit_code == 1

    with pytest.raises(LedgerError) as by_alias:
        ledger.stocks.show("tube-1")
    assert str(by_alias.value) == (
        "unknown stock 'tube-1': tube-1 is an aliquot; "
        "try tm aliquot show tube-1"
    )
    assert by_alias.value.exit_code == 1


def test_wrong_type_hints_stay_off_for_other_misses(ledger_factory) -> None:
    """Only another show type adds a type hint; a plain miss points at find."""
    ledger = make_ledger(ledger_factory)
    item = new_item(ledger)

    with pytest.raises(
        LedgerError, match=r"^unknown aliquot 'nope'; try tm find nope$"
    ):
        ledger.aliquots.show("nope")
    with pytest.raises(LedgerError, match=r"^unknown aliquot ''$"):
        ledger.aliquots.show("")
    with pytest.raises(
        LedgerError,
        match=rf"^unknown aliquot {item['id']!r}; try tm find {item['id']}$",
    ):
        ledger.aliquots.show(item["id"])

    with pytest.raises(
        LedgerError, match=r"^unknown stock 'nope'; try tm find nope$"
    ):
        ledger.stocks.show("nope")
    with pytest.raises(LedgerError, match=r"^unknown stock ''$"):
        ledger.stocks.show("")
    with pytest.raises(
        LedgerError,
        match=rf"^unknown stock {item['id']!r}; try tm find {item['id']}$",
    ):
        ledger.stocks.show(item["id"])


# ---------------------------------------------------------------------------
# Aliquot listing: tm aliquot list (Tetramark-i93.9)
# ---------------------------------------------------------------------------


def test_aliquot_list_defaults_to_the_tubes_an_experiment_can_use(
    ledger_factory,
) -> None:
    """Only in-stock tubes by default; --all and the complement show the rest."""
    ledger = make_ledger(ledger_factory)
    item = new_item(ledger)
    new_box(ledger, "BOX", "grid96")
    stock = ledger.stocks.add("lot", entity=item["id"], aliquots=3)
    first, second, third = stock["aliquots"]
    ledger.aliquots.move(first, box="BOX", slot="A1", message="moved")
    ledger.aliquots.status(second, "missing", message="not in the box")

    listed = ledger.aliquots.ls()
    assert [row["id"] for row in listed] == [first]
    # A list row is the show record: same keys, same values - except the
    # events, which a list never reads.
    shown = ledger.aliquots.show(first)
    assert listed[0] == {key: value for key, value in shown.items() if key != "events"}
    assert "events" not in listed[0]
    assert listed[0]["in_stock"] is True

    every = ledger.aliquots.ls(all_statuses=True)
    assert [row["id"] for row in every] == [first, second, third]
    assert [row["in_stock"] for row in every] == [True, False, False]

    left = ledger.aliquots.ls(in_stock=False)
    assert [row["id"] for row in left] == [second, third]
    assert all(row["path"] is None for row in left)


def test_aliquot_list_status_filter_widens_the_default_scope(ledger_factory) -> None:
    """Naming a status is a deliberate choice: it turns the in-stock scope off."""
    ledger = make_ledger(ledger_factory)
    item = new_item(ledger)
    new_box(ledger, "BOX", "grid96")
    stock = ledger.stocks.add("lot", entity=item["id"], aliquots=3)
    first, second, third = stock["aliquots"]
    ledger.aliquots.move(first, box="BOX", slot="A1", message="moved")
    ledger.aliquots.status(second, "missing", message="gone")
    ledger.aliquots.status(third, "consumed", message="used up")

    assert [row["id"] for row in ledger.aliquots.ls(status=("missing",))] == [second]
    assert [
        row["id"] for row in ledger.aliquots.ls(status=("stored", "missing"))
    ] == [first, second]
    # --all with a status is redundant but legal.
    assert [
        row["id"]
        for row in ledger.aliquots.ls(status=("missing",), all_statuses=True)
    ] == [second]
    # --not-in-stock still intersects with an explicit status filter.
    assert [
        row["id"]
        for row in ledger.aliquots.ls(status=("missing",), in_stock=False)
    ] == [second]
    assert ledger.aliquots.ls(status=("stored",), in_stock=False) == []


def test_aliquot_list_box_filter_includes_every_nested_box(ledger_factory) -> None:
    """--box covers the box itself and every box nested inside it."""
    ledger = make_ledger(ledger_factory)
    item = new_item(ledger)
    ledger.boxes.add("shelf", "SH-1")
    rack = ledger.boxes.add("rack", "R-1", parent="SH-1")
    inner = ledger.boxes.add("grid96", "G-2", parent="R-1", slot="middle")
    ledger.boxes.add("grid96", "G-1")
    ledger.aliases.add("box", rack["id"], "RACK-A")
    stock = ledger.stocks.add("lot", entity=item["id"], aliquots=3)
    first, second, third = stock["aliquots"]
    ledger.aliquots.move(first, box="R-1", slot="top", message="moved")
    ledger.aliquots.move(second, box="G-2", slot="B2", message="moved")
    # A slotless box holds a tube directly as well as child boxes.
    ledger.aliquots.move(third, box="SH-1", message="moved")

    assert [row["id"] for row in ledger.aliquots.ls(box="SH-1")] == [
        first,
        second,
        third,
    ]
    assert [row["id"] for row in ledger.aliquots.ls(box="R-1")] == [first, second]
    assert [row["id"] for row in ledger.aliquots.ls(box=rack["id"])] == [first, second]
    assert [row["id"] for row in ledger.aliquots.ls(box="RACK-A")] == [first, second]
    assert [row["id"] for row in ledger.aliquots.ls(box=str(inner["id"]))] == [second]
    assert ledger.aliquots.ls(box="G-1") == []
    # Every filter at once: the parameters of the box tree, the stock, the kind,
    # the statuses, --where and the in-stock scope must line up in SQL order.
    assert [
        row["id"]
        for row in ledger.aliquots.ls(
            box="SH-1",
            stock=stock["id"],
            kind="lot",
            status=("stored",),
            where=["seq=2"],
        )
    ] == [second]
    # A valid box with no tube in it is an empty list, not an error.
    ledger.boxes.add("shelf", "EMPTY")
    assert ledger.aliquots.ls(box="EMPTY") == []


def test_aliquot_list_filters_by_stock_kind_stock_and_where(ledger_factory) -> None:
    """A stock, a stock kind and --where narrow the same listing."""
    ledger = make_full_ledger(ledger_factory)
    item = new_item(ledger)
    everything = ledger.entities.add("everything", name="thing")
    new_box(ledger, "BOX", "grid96")
    lot = ledger.stocks.add("lot", entity=item["id"], aliquots=2)
    prep = ledger.stocks.add("prep", entity=everything["id"])
    first, second = lot["aliquots"]
    only = prep["aliquots"][0]
    ledger.aliquots.move(first, box="BOX", slot="A1", message="moved")
    ledger.aliquots.move(only, box="BOX", slot="A2", message="moved")
    ledger.driver.execute(
        "UPDATE `aliquot` SET `label` = 'first tube' WHERE `id` = %s", (first,)
    )

    assert [row["id"] for row in ledger.aliquots.ls(kind="lot")] == [first]
    assert [row["id"] for row in ledger.aliquots.ls(kind="prep")] == [only]
    assert [row["id"] for row in ledger.aliquots.ls(stock=lot["id"])] == [first]
    # An id prefix follows the same rule as every other reference: it must
    # name exactly one stock, or the command refuses with the candidates.
    with pytest.raises(UsageError, match="ambiguous"):
        ledger.aliquots.ls(stock=lot["id"][:6])
    assert [
        row["id"] for row in ledger.aliquots.ls(where=["label=first tube"])
    ] == [first]
    assert [
        row["id"] for row in ledger.aliquots.ls(all_statuses=True, where=["seq=2"])
    ] == [second]

    # A child stock is its own listing: a stock filter never follows lineage.
    child = ledger.stocks.add("lot", entity=item["id"], parent=lot["id"])
    child_tube = child["aliquots"][0]
    ledger.aliquots.move(child_tube, box="BOX", slot="A3", message="moved")
    assert [row["id"] for row in ledger.aliquots.ls(stock=lot["id"])] == [first]
    assert child_tube in [row["id"] for row in ledger.aliquots.ls()]


def test_aliquot_list_refuses_unknown_filters_and_empty_values(ledger_factory) -> None:
    """Every miss keeps its class: not-found from the resolver, usage from the CLI."""
    ledger = make_ledger(ledger_factory)
    item = new_item(ledger)
    new_box(ledger, "BOX", "grid96")
    stock = ledger.stocks.add("lot", entity=item["id"])
    alq = stock["aliquots"][0]
    ledger.aliquots.move(alq, box="BOX", slot="A1", message="moved")

    with pytest.raises(LedgerError, match=r"^unknown stock 'NOPE'$"):
        ledger.aliquots.ls("NOPE")
    with pytest.raises(LedgerError, match=r"^unknown box 'NOPE'$"):
        ledger.aliquots.ls(box="NOPE")
    # An empty ref must not fall through to the unique-prefix branch and match
    # the ledger's only stock/box.
    with pytest.raises(LedgerError, match=r"^unknown stock ''$"):
        ledger.aliquots.ls("")
    with pytest.raises(LedgerError, match=r"^unknown box ''$"):
        ledger.aliquots.ls(box="")
    with pytest.raises(LedgerError) as hinted:
        ledger.aliquots.ls(alq)
    assert str(hinted.value) == (
        f"unknown stock {alq!r}: {alq} is an aliquot; try tm aliquot show {alq}"
    )
    assert hinted.value.exit_code == 1
    # The pinned-status path resolves the stock too, and keeps the same hint.
    with pytest.raises(LedgerError) as hinted_status:
        ledger.aliquots.ls(alq, status=("stored",))
    assert "is an aliquot; try tm aliquot show" in str(hinted_status.value)

    with pytest.raises(UsageError, match=r"unknown status 'bogus'"):
        ledger.aliquots.ls(status=("bogus",))
    with pytest.raises(UsageError, match="a --status value must not be empty"):
        ledger.aliquots.ls(status=("",))
    with pytest.raises(UsageError, match="filter the status with --status"):
        ledger.aliquots.ls(where=["status=stored"])
    with pytest.raises(UsageError, match="unknown field"):
        ledger.aliquots.ls(where=["nope=1"])
    with pytest.raises(UsageError, match="--all and --not-in-stock"):
        ledger.aliquots.ls(all_statuses=True, in_stock=False)


def test_aliquot_list_status_validation_follows_the_pinned_stock_kind(
    ledger_factory,
) -> None:
    """A pinned stock/kind validates against its own set, not the whole ledger."""
    # A legacy closure: the fixed model gives every kind the same set, so the
    # per-kind restriction this test pins only exists in a spec-4/5 preset.
    ledger = ledger_factory([PRESETS / "legacy"], name="list-status", id_prefix="77")
    entity = ledger.entities.add("cell_line", name="HeLa")
    new_box(ledger, "BOX", "grid8x12")
    vial = ledger.stocks.add("cell_vial", entity=entity["id"], status="missing")
    pellet = ledger.stocks.add("cell_pellet", entity=entity["id"], status="pending")
    vial_tube = vial["aliquots"][0]
    pellet_tube = pellet["aliquots"][0]

    # `thawed` is a cryo status, `pending` is a tube status.
    with pytest.raises(UsageError, match="for stock kind 'cell_pellet'"):
        ledger.aliquots.ls(kind="cell_pellet", status=("thawed",))
    with pytest.raises(UsageError, match="for stock kind 'cell_vial'"):
        ledger.aliquots.ls(kind="cell_vial", status=("pending",))
    with pytest.raises(UsageError, match="for stock '"):
        ledger.aliquots.ls(vial["id"], status=("pending",))
    with pytest.raises(UsageError, match="for stock '"):
        ledger.aliquots.ls(pellet["id"], status=("thawed",))

    # Without a pin, the ledger-wide vocabulary accepts both and each row's
    # own kind still decides whether it is in stock.
    assert ledger.aliquots.ls(status=("thawed",)) == []
    assert [
        row["id"] for row in ledger.aliquots.ls(status=("missing", "pending"))
    ] == [vial_tube, pellet_tube]


def test_aliquot_list_scope_covers_every_stock_kind(ledger_factory) -> None:
    """The in-stock scope ORs each kind's set: `alive` counts for an animal."""
    # Legacy per-kind sets again (see the fixed-model note above).
    ledger = ledger_factory([PRESETS / "legacy"], name="list-animal", id_prefix="77")
    line = ledger.entities.add("fish_line", name="AB")
    ledger.boxes.add("rack", "R-1")
    ledger.boxes.add("freezer", "FR-1")
    alive = ledger.stocks.add(
        "fish_group", entity=line["id"], status="alive", box="R-1", slots=("1",)
    )
    frozen = ledger.stocks.add(
        "fish_sperm_cryo", entity=line["id"], status="stored", box="FR-1"
    )
    planned = ledger.stocks.add("fish_group", entity=line["id"], status="planned")

    assert [row["id"] for row in ledger.aliquots.ls()] == [
        alive["aliquots"][0],
        frozen["aliquots"][0],
    ]
    assert [row["id"] for row in ledger.aliquots.ls(in_stock=False)] == [
        planned["aliquots"][0]
    ]
    assert [row["id"] for row in ledger.aliquots.ls(status=("alive",))] == [
        alive["aliquots"][0]
    ]
    # Pinned to the animal kind, a cryo status is refused.
    with pytest.raises(UsageError, match="for stock kind 'fish_group'"):
        ledger.aliquots.ls(kind="fish_group", status=("stored",))


def test_aliquot_list_scope_with_no_in_stock_status(
    ledger_factory, tmp_path
) -> None:
    """A kind whose set has no in-stock status is never on the shelf."""
    preset = tmp_path / "noinstock.yaml"
    preset.write_text(
        "preset: noinstock\n"
        "version: 1\n"
        "status_sets:\n"
        "  archived:\n"
        "    file_only: {in_stock: false, to: [gone]}\n"
        "    gone: {in_stock: false}\n"
        "mixins:\n"
        "  named_fields:\n"
        "    name: {type: string, required: true, unique: true}\n"
        "kinds:\n"
        "  item:\n"
        "    object: entity\n"
        "    mixins: [named_fields]\n"
        "  archived_lot:\n"
        "    object: stock\n"
        "    entity_kinds: [item]\n"
        "    statuses: archived\n",
        encoding="utf-8",
    )
    ledger = ledger_factory([preset], name="list-noinstock", id_prefix="77")
    item = new_item(ledger)
    stock = ledger.stocks.add("archived_lot", entity=item["id"], status="file_only")
    alq = stock["aliquots"][0]

    assert ledger.aliquots.ls() == []
    assert [row["id"] for row in ledger.aliquots.ls(in_stock=False)] == [alq]
    assert [row["id"] for row in ledger.aliquots.ls(status=("file_only",))] == [alq]


# ---------------------------------------------------------------------------
# Boxes
# ---------------------------------------------------------------------------


def test_box_add_materialises_slots_and_lists_paths(ledger_factory) -> None:
    ledger = make_full_ledger(ledger_factory)
    before = commit_count(ledger)
    grid = ledger.boxes.add("grid9x9", "GRID82")
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

    # A named box is a valid parent; a grid box is not (spec 5).
    child = ledger.boxes.add("grid96", "RACK-1", parent="TUBES", slot="top")
    paths = {row["name"]: row["path"] for row in ledger.boxes.ls()}
    assert paths["GRID82"] == "GRID82"
    assert paths["RACK-1"] == "TUBES/RACK-1"
    assert child["parent_slot"] == "top"
    with pytest.raises(LedgerError, match="grid boxes hold tubes only"):
        ledger.boxes.add("grid96", "NESTED", parent="GRID82", slot="A1")

    with pytest.raises(LedgerError, match="already in use"):
        ledger.boxes.add("grid96", "GRID82")
    with pytest.raises(UsageError, match="requires --in"):
        ledger.boxes.add("grid96", "ORPHAN", slot="A1")


def test_box_add_creates_its_view_and_refuses_view_unsafe_names(
    ledger_factory,
) -> None:
    """Spec 5: every box gets its view in the add commit; names must be safe."""
    ledger = make_ledger(ledger_factory, name="box-names")
    before = commit_count(ledger)
    box = ledger.boxes.add("grid96", "WFL-10-01")
    # The box row, its slot rows and its view are one commit.
    assert commit_count(ledger) == before + 1
    views = {
        str(row["name"]).lower()
        for row in ledger.driver.query(
            "SELECT table_name AS name FROM information_schema.views "
            "WHERE table_schema = DATABASE()"
        )
    }
    assert "v_box_wfl-10-01" in views
    assert "v_box_grid96" not in views  # kind views are gone (spec 5)
    # The grid pivot: one row per row label (A-H), a column per column label.
    columns = [
        str(row["name"])
        for row in ledger.driver.query(
            "SELECT column_name AS name FROM information_schema.columns "
            "WHERE table_schema = DATABASE() "
            "AND table_name = 'v_box_wfl-10-01' ORDER BY ordinal_position"
        )
    ]
    assert columns == ["row", *[str(number) for number in range(1, 13)]]
    assert len(
        ledger.driver.query("SELECT * FROM `v_box_WFL-10-01`")
    ) == 8
    assert ledger.driver.query(
        "SELECT * FROM `v_box_wfl-10-01` WHERE `row` = 'A'"
    )[0]["1"] is None

    # (a) a core view collision, (b) the 58-character limit, (c) a case-only
    # duplicate of an existing box name.
    with pytest.raises(UsageError, match="core view"):
        ledger.boxes.add("rack", "slots")
    with pytest.raises(UsageError, match="58"):
        ledger.boxes.add("rack", "x" * 59)
    with pytest.raises(UsageError, match="differs only in case"):
        ledger.boxes.add("rack", "wfl-10-01")
    # An exact duplicate keeps its own (clearer) error.
    with pytest.raises(LedgerError, match="already in use"):
        ledger.boxes.add("rack", "WFL-10-01")
    # A name at the limit is fine and gets its own view.
    longest = "y" * 58
    ledger.boxes.add("rack", longest)
    assert f"v_box_{longest}" in {
        str(row["name"]).lower()
        for row in ledger.driver.query(
            "SELECT table_name AS name FROM information_schema.views "
            "WHERE table_schema = DATABASE()"
        )
    }
    assert box["id"]


def _view_names(ledger) -> set[str]:
    return {
        str(row["name"]).lower()
        for row in ledger.driver.query(
            "SELECT table_name AS name FROM information_schema.views "
            "WHERE table_schema = DATABASE()"
        )
    }


def test_entity_rename_keeps_the_old_name_as_an_alias(ledger_factory) -> None:
    ledger = make_ledger(ledger_factory, name="rename-entity")
    created = new_item(ledger, "alpha", alias="A")
    other = new_item(ledger, "other", alias="gamma")

    before = commit_count(ledger)
    renamed = ledger.entities.rename("A", "beta")
    assert commit_count(ledger) == before + 1
    assert dirty(ledger) == []
    assert renamed == {
        "object": "entity",
        "kind": "item",
        "id": created["id"],
        "name": "beta",
        "old_name": "alpha",
        "aliases": ["A", "alpha"],
    }
    shown = ledger.entities.show("alpha")
    assert shown["id"] == created["id"]
    assert shown["name"] == "beta"
    # Core and kind-table names stay in sync.
    assert ledger.driver.query(
        "SELECT `name` FROM `entity_item` WHERE `id` = %s", (created["id"],)
    ) == [{"name": "beta"}]
    assert ledger.entities.ls("item", where={"name": "beta"})[0]["id"] == created["id"]
    message = ledger.driver.query("SELECT message FROM dolt_log LIMIT 1")[0]["message"]
    assert message == "rename(entity): alpha -> beta"

    # Renaming back reuses the existing alias instead of failing on it.
    again = ledger.entities.rename(created["id"], "alpha")
    assert again["aliases"] == ["A", "alpha", "beta"]
    assert ledger.entities.show("beta")["name"] == "alpha"

    # Same name, empty name, an old name owned by another entity: refused, no commit.
    before = commit_count(ledger)
    with pytest.raises(UsageError, match="already named"):
        ledger.entities.rename(created["id"], "alpha")
    with pytest.raises(UsageError, match="must not be empty"):
        ledger.entities.rename(created["id"], "  ")
    ledger.entities.rename(other["id"], "gamma-2")  # 'other' -> alias of other
    before = commit_count(ledger)
    third = new_item(ledger, "gamma")
    with pytest.raises(LedgerError, match="already an alias of entity"):
        ledger.entities.rename(third["id"], "delta")
    assert ledger.entities.show(third["id"])["name"] == "gamma"
    assert commit_count(ledger) == before + 1  # only the add committed
    assert dirty(ledger) == []


def test_box_rename_moves_its_view_and_keeps_the_old_name(ledger_factory) -> None:
    ledger = make_ledger(ledger_factory, name="rename-box")
    box = new_box(ledger, "WFL-10-01")
    other = new_box(ledger, "OTHER", kind="rack")
    ledger.aliases.add("box", other["id"], "taken")
    entity = new_item(ledger)
    stock = ledger.stocks.add("lot", entity=entity["id"], box="WFL-10-01", slots=["A1"])
    tube = stock["aliquots"][0]

    before = commit_count(ledger)
    renamed = ledger.boxes.rename("WFL-10-01", "WFL-20-01")
    assert commit_count(ledger) == before + 1
    assert dirty(ledger) == []
    assert renamed["id"] == box["id"]
    assert renamed["old_name"] == "WFL-10-01"
    assert renamed["aliases"] == ["WFL-10-01"]
    views = _view_names(ledger)
    assert "v_box_wfl-20-01" in views
    assert "v_box_wfl-10-01" not in views
    # The new view shows the same contents, read by box id.
    assert ledger.driver.query(
        "SELECT `1` FROM `v_box_WFL-20-01` WHERE `row` = 'A'"
    ) == [{"1": tube}]
    # The old name still resolves (as an alias); the new one is the name.
    shown = ledger.boxes.show("WFL-10-01")
    assert shown["box"] == "WFL-20-01"
    assert shown["aliases"] == ["WFL-10-01"]
    assert box["id"] in {row["id"] for row in ledger.find("WFL-10-01")}
    assert ledger.check().passed

    # A case-only change of its own name is allowed and keeps one view.
    ledger.boxes.rename(box["id"], "wfl-20-01")
    assert "v_box_wfl-20-01" in _view_names(ledger)
    assert ledger.check().passed

    before = commit_count(ledger)
    with pytest.raises(LedgerError, match="already in use"):
        ledger.boxes.rename(box["id"], "OTHER")
    with pytest.raises(LedgerError, match="already an alias of box"):
        ledger.boxes.rename(box["id"], "taken")
    with pytest.raises(UsageError, match="differs only in case"):
        ledger.boxes.rename(box["id"], "other")
    with pytest.raises(UsageError, match="core view"):
        ledger.boxes.rename(box["id"], "slots")
    with pytest.raises(UsageError, match="already named"):
        ledger.boxes.rename(box["id"], "wfl-20-01")
    assert commit_count(ledger) == before
    assert dirty(ledger) == []
    assert "v_box_wfl-20-01" in _view_names(ledger)


def test_box_nesting_exclusivity_acyclicity_and_slotless_capacity(
    ledger_factory,
) -> None:
    ledger = make_full_ledger(ledger_factory)
    ledger.boxes.add("namedbox", "TUBES")
    ledger.boxes.add("rack", "INNER", parent="TUBES", slot="top")
    ledger.boxes.add("grid96", "SHELF")
    ledger.boxes.add("slotlessbox", "DRAWER")

    with pytest.raises(LedgerError, match="already holds child box"):
        ledger.boxes.add("grid96", "CLASH", parent="TUBES", slot="top")

    item = new_item(ledger)
    with pytest.raises(LedgerError, match="child box"):
        ledger.stocks.add("lot", entity=item["id"], box="TUBES", slots=["top"])

    with pytest.raises(LedgerError, match="inside itself"):
        ledger.boxes.move("TUBES", parent="INNER", slot="top")
    with pytest.raises(LedgerError, match="has no slot"):
        ledger.boxes.move("SHELF", parent="TUBES", slot="Z99")
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
    ledger.boxes.add("namedbox", "TUBES")
    ledger.boxes.add("grid9x9", "GRID")
    ledger.boxes.add("grid96", "SHELF", parent="TUBES", slot="top")
    before = commit_count(ledger)
    moved = ledger.boxes.move("SHELF", parent="TUBES", slot="bottom")
    assert commit_count(ledger) == before + 1
    assert moved["parent_slot"] == "bottom"
    paths = {row["name"]: row["path"] for row in ledger.boxes.ls()}
    assert paths["SHELF"] == "TUBES/SHELF"
    # Grid boxes hold tubes only (spec 5), so they cannot become a parent.
    with pytest.raises(LedgerError, match="grid boxes hold tubes only"):
        ledger.boxes.move("SHELF", parent="GRID", slot="A1")
    with pytest.raises(LedgerError, match="unknown box"):
        ledger.boxes.move("NOPE", parent="TUBES", slot="top")


# ---------------------------------------------------------------------------
# Edit: allowlist split, rename-only names, clear-empty (Tetramark-p1j)
# ---------------------------------------------------------------------------


def _stock_name_preset(tmp_path):
    """A preset whose stock kind declares a field literally named ``name``.

    The rename-only guard covers entity and box names; a stock has no core
    name, so a stock kind may keep such a field and edit it like any other.
    """
    path = tmp_path / "stock_name.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "preset": "stock_name",
                "version": 1,
                "mixins": {
                    "named_fields": {
                        "name": {"type": "string", "required": True, "unique": True}
                    }
                },
                "kinds": {
                    "item": {"object": "entity", "mixins": ["named_fields"]},
                    "named_lot": {
                        "object": "stock",
                        "entity_kinds": ["item"],
                        "mixins": ["named_fields"],
                    },
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def test_core_allowlists_split_add_from_edit() -> None:
    """AC7: add/import may fill ``name``; edit is notes-only for every object."""
    assert _CORE_ON_ADD == {
        "entity": frozenset({"name", "notes"}),
        "stock": frozenset({"notes"}),
        "box": frozenset({"name", "notes"}),
    }
    assert _CORE_EDITABLE == {
        "entity": frozenset({"notes"}),
        "stock": frozenset({"notes"}),
        "box": frozenset({"notes"}),
    }
    for allowlist in _CORE_EDITABLE.values():
        assert "name" not in allowlist


def test_box_edit_sets_and_clears_notes_and_kind_fields(ledger_factory) -> None:
    """AC1/AC2: ``box edit`` loads the record; values are coerced like add."""
    ledger = make_boxfields_ledger(ledger_factory, name="box-edit")
    box = ledger.boxes.add("fieldbox", "BOX-1")
    assert box["slots"] == 0

    before = commit_count(ledger)
    edited = ledger.boxes.edit(
        "BOX-1", values={"notes": "first", "code": "abc", "count": "7"}
    )
    assert commit_count(ledger) == before + 1
    assert dirty(ledger) == []
    assert edited["object"] == "box"
    assert set(edited) == {
        "id",
        "kind",
        "name",
        "parent_box_id",
        "parent_slot",
        "created_at",
        "notes",
        "code",
        "count",
        "label",
        "object",
    }
    assert edited["id"] == box["id"]
    assert edited["kind"] == "fieldbox"
    assert edited["name"] == "BOX-1"
    assert edited["notes"] == "first"
    assert edited["code"] == "abc"
    assert edited["count"] == 7
    # The required field with a default is filled by the database at add time.
    assert edited["label"] == "unnamed"
    message = ledger.driver.query("SELECT message FROM dolt_log LIMIT 1")[0]["message"]
    assert message.startswith("edit(box): ")
    assert message == f"edit(box): fieldbox {box['id']}"

    before = commit_count(ledger)
    cleared = ledger.boxes.edit(
        "BOX-1", values={"notes": "", "code": "", "count": ""}
    )
    assert commit_count(ledger) == before + 1
    assert cleared["notes"] is None
    assert cleared["code"] is None
    assert cleared["count"] is None
    assert ledger.driver.query(
        "SELECT `notes` FROM `box` WHERE `id` = %s", (box["id"],)
    ) == [{"notes": None}]
    assert ledger.driver.query(
        "SELECT `code`, `count`, `label` FROM `box_fieldbox` WHERE `id` = %s",
        (box["id"],),
    ) == [{"code": None, "count": None, "label": "unnamed"}]


def test_box_edit_coerces_like_add_and_failures_write_nothing(ledger_factory) -> None:
    """AC2: invalid values are usage errors and leave the row untouched."""
    ledger = make_boxfields_ledger(ledger_factory, name="box-coerce")
    box = ledger.boxes.add("fieldbox", "BOX-1")
    before = commit_count(ledger)

    cases = [
        ({"code": "ABC"}, "pattern"),
        ({"code": "a" * 33}, "32"),
        ({"count": "x"}, "integer"),
        # Whitespace-only is not empty: it still goes through coercion.
        ({"code": "   "}, "pattern"),
        ({"label": ""}, "field 'label' is required and cannot be cleared"),
    ]
    for values, message in cases:
        with pytest.raises(UsageError, match=message):
            ledger.boxes.edit("BOX-1", values=values)

    assert commit_count(ledger) == before
    assert dirty(ledger) == []
    assert ledger.driver.query(
        "SELECT `code`, `count`, `label` FROM `box_fieldbox` WHERE `id` = %s",
        (box["id"],),
    ) == [{"code": None, "count": None, "label": "unnamed"}]

    coerced = ledger.boxes.edit("BOX-1", values={"code": "abc", "count": " 7 "})
    assert coerced["code"] == "abc"
    assert coerced["count"] == 7
    assert isinstance(coerced["count"], int)


def test_edit_refusals_fire_before_resolution(ledger_factory) -> None:
    """AC3: names and structural fields are refused on the raw ``--set`` keys."""
    ledger = make_ledger(ledger_factory, name="edit-guards")
    item = new_item(ledger)
    stock = ledger.stocks.add("lot", entity=item["id"])
    new_box(ledger, "BOX", "grid96")
    before = commit_count(ledger)

    # ``item`` declares its own ``name`` field; the guard still fires.
    with pytest.raises(UsageError) as name_error:
        ledger.entities.edit(item["id"], values={"name": "beta"})
    assert str(name_error.value) == (
        f"name is rename-only; use tm entity rename {item['id']} NEW_NAME"
    )
    # ...and on an unresolvable ref, naming the raw ref as typed.
    with pytest.raises(UsageError) as missing_name:
        ledger.boxes.edit("NOT-A-BOX", values={"name": "X"})
    assert str(missing_name.value) == (
        "name is rename-only; use tm box rename NOT-A-BOX NEW_NAME"
    )
    with pytest.raises(UsageError) as parent:
        ledger.boxes.edit("BOX", values={"parent_box_id": "SHELF"})
    assert str(parent.value) == (
        "parent_box_id is structural; move a box with tm box move BOX "
        "--in PARENT [--slot S]"
    )
    with pytest.raises(UsageError) as slot:
        ledger.boxes.edit("BOX", values={"parent_slot": "top"})
    assert str(slot.value) == (
        "parent_slot is structural; move a box with tm box move BOX "
        "--in PARENT [--slot S]"
    )
    for edit, ref in (
        (ledger.entities.edit, item["id"]),
        (ledger.stocks.edit, stock["id"]),
        (ledger.boxes.edit, "BOX"),
    ):
        for field in ("id", "kind", "created_at"):
            with pytest.raises(UsageError) as structural:
                edit(ref, values={field: "x"})
            assert str(structural.value) == (
                f"{field} is structural and cannot be edited"
            )
    assert commit_count(ledger) == before
    assert dirty(ledger) == []


def test_box_edit_unknown_field_lists_kind_fields_and_notes_only(
    ledger_factory,
) -> None:
    """AC4: the valid list is kind fields plus ``notes`` -- never core ``name``."""
    ledger = make_boxfields_ledger(ledger_factory, name="box-unknown")
    ledger.boxes.add("fieldbox", "BOX-1")
    before = commit_count(ledger)
    with pytest.raises(UsageError) as unknown:
        ledger.boxes.edit("BOX-1", values={"nope": "1"})
    assert str(unknown.value) == (
        "unknown field(s) 'nope' for box kind 'fieldbox'; "
        "valid fields: code, count, label, notes"
    )

    plain = make_ledger(ledger_factory, name="box-unknown-plain")
    new_box(plain, "BOX", "grid96")
    with pytest.raises(UsageError) as unknown_plain:
        plain.boxes.edit("BOX", values={"nope": "1"})
    assert str(unknown_plain.value) == (
        "unknown field(s) 'nope' for box kind 'grid96'; valid fields: notes"
    )
    assert commit_count(ledger) == before


def test_edit_empty_value_clears_optional_fields_of_any_type(ledger_factory) -> None:
    """AC5: an exactly empty value clears to NULL; required/computed are refused."""
    ledger = make_full_ledger(ledger_factory, name="clear-empty")
    parent = ledger.entities.add("everything", name="parent", values={"code": "AAA"})
    entity = ledger.entities.add(
        "everything",
        name="alpha",
        values={
            "code": "BBB",
            "blurb": "some text",
            "count": "3",
            "born": "2024-01-31",
            "seen_at": "2024-01-01 12:34:56",
            "color": "green",
            "parent": parent["id"],
            "notes": "keep?",
        },
    )
    before = commit_count(ledger)
    cleared = ledger.entities.edit(
        entity["id"],
        values={
            "code": "",
            "blurb": "",
            "count": "",
            "born": "",
            "seen_at": "",
            "color": "",
            "parent": "",
            "notes": "",
        },
    )
    assert commit_count(ledger) == before + 1
    assert dirty(ledger) == []
    shown = ledger.entities.show(entity["id"])
    for field in (
        "code",
        "blurb",
        "count",
        "born",
        "seen_at",
        "color",
        "parent",
        "notes",
    ):
        assert cleared[field] is None, field
        assert shown[field] is None, field

    # A computed field keeps its own error; a required field is refused.
    item = new_item(ledger)
    before = commit_count(ledger)
    with pytest.raises(UsageError, match="computed and cannot be set"):
        ledger.entities.edit(entity["id"], values={"display": ""})
    bacterial = ledger.stocks.add(
        "bacterial", entity=item["id"], values={"strain": "DH5a"}
    )
    with pytest.raises(UsageError) as required:
        ledger.stocks.edit(bacterial["id"], values={"strain": ""})
    assert str(required.value) == "field 'strain' is required and cannot be cleared"
    assert commit_count(ledger) == before + 1  # only the bacterial add committed
    assert dirty(ledger) == []

    # Whitespace-only is not empty: it is coerced as before, not cleared.
    before = commit_count(ledger)
    with pytest.raises(UsageError, match="integer"):
        ledger.entities.edit(entity["id"], values={"count": "   "})
    kept = ledger.entities.edit(entity["id"], values={"blurb": "   "})
    assert kept["blurb"] == "   "
    assert commit_count(ledger) == before + 1


def test_stock_edit_clears_values_and_a_kind_name_field_is_not_guarded(
    tmp_path, ledger_factory
) -> None:
    """AC6: stock edit keeps working, clears values, and may edit a ``name`` field."""
    ledger = make_ledger(ledger_factory, name="stock-edit")
    item = new_item(ledger)
    stock = ledger.stocks.add(
        "lot",
        entity=item["id"],
        values={"supplier": "acme", "received_on": "2024-01-02", "notes": "first"},
    )
    before = commit_count(ledger)
    edited = ledger.stocks.edit(
        stock["id"], values={"supplier": "", "received_on": "", "notes": ""}
    )
    assert commit_count(ledger) == before + 1
    assert edited["supplier"] is None
    assert edited["received_on"] is None
    assert edited["notes"] is None

    named = ledger_factory(
        [_stock_name_preset(tmp_path)], name="stock-name", real_init=True
    )
    named_item = named.entities.add("item", name="alpha")
    named_stock = named.stocks.add(
        "named_lot", entity=named_item["id"], values={"name": "first"}
    )
    before = commit_count(named)
    renamed_field = named.stocks.edit(named_stock["id"], values={"name": "second"})
    assert commit_count(named) == before + 1
    assert renamed_field["name"] == "second"


def test_edit_dry_run_reports_new_values_and_writes_nothing(ledger_factory) -> None:
    """AC9: a dry-run edit buffers the UPDATE and still reports the new record."""
    ledger = make_boxfields_ledger(ledger_factory, name="edit-dry")
    box = ledger.boxes.add("fieldbox", "BOX-1")
    before = commit_count(ledger)

    ledger.dry_run = True
    try:
        planned = ledger.boxes.edit(
            "BOX-1", values={"notes": "planned", "code": "abc", "count": "7"}
        )
        assert planned["notes"] == "planned"
        assert planned["code"] == "abc"
        assert planned["count"] == 7
        assert commit_count(ledger) == before
        assert dirty(ledger) == []
        assert ledger.last_write is not None
        assert "UPDATE `box`" in ledger.last_write.sql
    finally:
        ledger.dry_run = False

    assert ledger.driver.query(
        "SELECT `notes` FROM `box` WHERE `id` = %s", (box["id"],)
    ) == [{"notes": None}]
    assert ledger.driver.query(
        "SELECT `code`, `count` FROM `box_fieldbox` WHERE `id` = %s", (box["id"],)
    ) == [{"code": None, "count": None}]

    applied = ledger.boxes.edit(
        "BOX-1", values={"notes": "planned", "code": "abc", "count": "7"}
    )
    assert commit_count(ledger) == before + 1
    assert applied["notes"] == "planned"
    assert applied["code"] == "abc"


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


def test_a_unique_id_prefix_resolves_but_an_ambiguous_one_is_a_usage_error(
    ledger_factory,
) -> None:
    """An incomplete id is accepted when it matches exactly one object."""
    ledger = make_ledger(ledger_factory)
    first = new_item(ledger, "first", id="aaaaaaaa-1111-4111-8111-111111111111")
    second = new_item(ledger, "second", id="aaaaaaaa-1111-4111-8111-222222222222")

    # A prefix that matches one object is enough for show and for alias add.
    assert ledger.entities.show(first["id"][:26])["id"] == first["id"]
    added = ledger.aliases.add("entity", second["id"][:30], "second-old")
    assert added["object_id"] == second["id"]
    assert ledger.entities.show("second-old")["id"] == second["id"]

    # A prefix that matches two objects writes nothing and names both.
    before = commit_count(ledger)
    with pytest.raises(UsageError, match="ambiguous") as excinfo:
        ledger.aliases.add("entity", "aaaaaaaa", "clash")
    message = str(excinfo.value)
    assert first["id"] in message and second["id"] in message
    assert commit_count(ledger) == before
    assert ledger.driver.query(
        "SELECT `alias` FROM `alias` WHERE `alias` = %s", ("clash",)
    ) == []

    # An exact alias wins over a prefix of other objects' ids, just like find.
    ledger.aliases.add("entity", first["id"], "aaaaaaaa")
    assert ledger.entities.show("aaaaaaaa")["id"] == first["id"]

    # No match at all keeps the established unknown-object error.
    with pytest.raises(LedgerError, match="unknown entity"):
        ledger.entities.show("99999999")


def test_alias_add_resolves_a_box_by_its_name(ledger_factory) -> None:
    """``aliases.add("box", ...)`` accepts a box name, like every box command."""
    ledger = make_ledger(ledger_factory, name="alias-box-name")
    box = new_box(ledger, "rack-a", "rack")

    added = ledger.aliases.add("box", "rack-a", "ra")

    assert added["object_type"] == "box"
    assert added["object_id"] == box["id"]
    assert ledger.boxes.show("ra")["id"] == box["id"]
    assert ledger.boxes.show("rack-a")["aliases"] == ["ra"]


def test_alias_add_box_name_beats_another_boxes_alias(ledger_factory) -> None:
    """A box name outranks another box's alias, following ``_resolve_box``."""
    ledger = make_ledger(ledger_factory, name="alias-box-precedence")
    named = new_box(ledger, "Shared", "rack")
    other = new_box(ledger, "OTHER", "rack")
    ledger.aliases.add("box", other["id"], "Shared")

    added = ledger.aliases.add("box", "Shared", "x")

    assert added["object_id"] == named["id"]
    assert ledger.boxes.show(named["id"])["aliases"] == ["x"]
    assert ledger.boxes.show(other["id"])["aliases"] == ["Shared"]


def test_alias_add_unknown_box_ref_keeps_the_hint_and_writes_nothing(
    ledger_factory,
) -> None:
    """A box miss names the ref and points at ``tm find``; no alias row lands."""
    ledger = make_ledger(ledger_factory, name="alias-box-miss")
    new_box(ledger, "rack-a", "rack")
    new_item(ledger, "alpha")

    before = commit_count(ledger)
    with pytest.raises(LedgerError) as excinfo:
        ledger.aliases.add("box", "no-such-box", "x")
    message = str(excinfo.value)
    assert "unknown box 'no-such-box'" in message
    assert "; try tm find no-such-box" in message
    assert commit_count(ledger) == before
    assert ledger.driver.query(
        "SELECT `alias` FROM `alias` WHERE `alias` = %s", ("x",)
    ) == []

    # Only boxes gain the name lookup: an entity is still resolved by
    # id/alias/prefix, never by its (nullable, non-unique) name.
    with pytest.raises(LedgerError, match="unknown entity 'alpha'"):
        ledger.aliases.add("entity", "alpha", "y")


def test_alias_add_box_ambiguous_id_prefix_stays_a_usage_error(
    ledger_factory,
) -> None:
    """An ambiguous box prefix is exit 2 with no hint, exactly as before."""
    ledger = make_ledger(ledger_factory, name="alias-box-ambiguous")
    # Box ids are uuid4 and box add takes no id, so seed two rows that share a
    # prefix by hand; the resolver only reads the box table.
    for suffix, name in (("1", "prefix-one"), ("2", "prefix-two")):
        ledger.driver.execute(
            "INSERT INTO `box` (`id`, `name`, `kind`) VALUES (%s, %s, 'rack')",
            (f"aaaaaaaa-0000-0000-0000-0000000000{suffix}", name),
        )

    before = commit_count(ledger)
    with pytest.raises(UsageError) as excinfo:
        ledger.aliases.add("box", "aaaaaaaa", "x")
    message = str(excinfo.value)
    assert excinfo.value.exit_code == 2
    assert "ambiguous" in message
    assert "try tm find" not in message
    assert commit_count(ledger) == before
    assert ledger.driver.query(
        "SELECT `alias` FROM `alias` WHERE `alias` = %s", ("x",)
    ) == []


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
    step(lambda: ledger.boxes.add("rack", "RACK"))
    step(lambda: ledger.boxes.move("BOX", parent="RACK", slot="top"))
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
    step(lambda: ledger.aliquots.move(alq, box="BOX", slot="A3", message="moved"))
    step(lambda: ledger.aliquots.use(alq, "1", "ul", message="prep"))
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
    opened = open_ledger(ledger.path, no_commit=True)
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


def test_stock_show_aliquot_rows_carry_label_path_aliases_and_in_stock(
    ledger_factory,
) -> None:
    """Each embedded tube row answers "how much and where" without a second read."""
    ledger = make_ledger(ledger_factory)
    item = new_item(ledger)
    new_box(ledger, "BOX", "grid96")
    stock = ledger.stocks.add(
        "lot", entity=item["id"], aliquots=2, quantity="5", unit="ul"
    )
    first, second = stock["aliquots"]
    ledger.aliquots.move(first, box="BOX", slot="A1", message="placed")
    ledger.aliases.add("aliquot", first, "tube-A")
    ledger.driver.execute(
        "UPDATE `aliquot` SET `label` = 'first tube' WHERE `id` = %s", (first,)
    )

    rows = ledger.stocks.show(stock["id"])["aliquots"]

    assert set(rows[0]) == {
        "id",
        "seq",
        "status",
        "quantity",
        "unit",
        "label",
        "box_id",
        "slot",
        "path",
        "aliases",
        "in_stock",
    }
    assert rows[0]["id"] == first
    assert rows[0]["seq"] == 1
    assert rows[0]["status"] == "stored"
    assert Decimal(str(rows[0]["quantity"])) == Decimal("5")
    assert rows[0]["unit"] == "ul"
    assert rows[0]["label"] == "first tube"
    assert rows[0]["box_id"] is not None
    assert rows[0]["slot"] == "A1"
    assert rows[0]["path"] == "BOX:A1"
    assert rows[0]["aliases"] == ["tube-A"]
    assert rows[0]["in_stock"] is True

    assert rows[1]["id"] == second
    assert rows[1]["status"] == "pending"
    assert rows[1]["label"] is None
    assert rows[1]["path"] is None
    assert rows[1]["aliases"] == []
    assert rows[1]["in_stock"] is False


# ---------------------------------------------------------------------------
# Status sets and transitions (spec 4)
# ---------------------------------------------------------------------------


def test_tube_status_transitions_follow_the_to_rules(ledger_factory) -> None:
    ledger = make_ledger(ledger_factory, name="transitions")
    item = new_item(ledger)
    box = new_box(ledger, "BOX", "grid96")
    stock = ledger.stocks.add(
        "lot", entity=item["id"], aliquots=1, box="BOX", slots=["A1"]
    )
    alq = stock["aliquots"][0]
    assert ledger.driver.query(
        "SELECT `status`, `box_id` FROM `aliquot` WHERE `id` = %s", (alq,)
    ) == [{"status": "stored", "box_id": box["id"]}]

    consumed = ledger.aliquots.status(alq, "consumed")
    assert consumed["old_status"] == "stored"
    assert consumed["status"] == "consumed"
    assert consumed["forced"] is False

    # ``consumed`` is final: the unforced change is refused, nothing is written.
    before = commit_count(ledger)
    with pytest.raises(LedgerError, match="final status"):
        ledger.aliquots.status(alq, "stored")
    assert commit_count(ledger) == before
    assert ledger.driver.query(
        "SELECT `status` FROM `aliquot` WHERE `id` = %s", (alq,)
    ) == [{"status": "consumed"}]

    # Forcing the change does not bypass the position invariant: ``stored`` is
    # in stock, so the tube needs a position, and ``consumed`` cleared it.
    with pytest.raises(LedgerError, match="tm aliquot move"):
        ledger.aliquots.status(alq, "stored", force=True, message="found the tube")
    assert ledger.driver.query(
        "SELECT `status` FROM `aliquot` WHERE `id` = %s", (alq,)
    ) == [{"status": "consumed"}]

    # --force overrides a transition rule into a status that is not in stock.
    forced = ledger.aliquots.status(alq, "missing", force=True, message="found the tube")
    assert forced["forced"] is True
    last = events(ledger, alq)[-1]
    assert last["event"] == "status"
    assert last["old_status"] == "consumed"
    assert last["new_status"] == "missing"
    assert last["forced"] in (1, True)
    assert last["message"] == "found the tube"

    # ``missing`` may go back to ``stored`` by the transition rules, but only
    # after the tube is physically placed again.
    with pytest.raises(LedgerError, match="tm aliquot move"):
        ledger.aliquots.status(alq, "stored")
    moved = ledger.aliquots.move(alq, box="BOX", slot="A1", message="moved")
    assert moved["status"] == "stored"
    assert moved["old_status"] == "missing"


def test_force_requires_a_message(ledger_factory) -> None:
    ledger = make_ledger(ledger_factory, name="force-message")
    item = new_item(ledger)
    new_box(ledger, "BOX", "grid96")
    stock = ledger.stocks.add(
        "lot", entity=item["id"], aliquots=1, box="BOX", slots=["A1"]
    )
    alq = stock["aliquots"][0]
    ledger.aliquots.status(alq, "consumed")
    with pytest.raises(UsageError, match="--force requires -m/--message"):
        ledger.aliquots.status(alq, "stored", force=True)
    with pytest.raises(UsageError, match="--force requires -m/--message"):
        ledger.aliquots.status(alq, "stored", force=True, message="   ")


def test_status_message_is_optional_trimmed_and_note_is_gone(ledger_factory) -> None:
    """The status message is optional, trimmed, refused blank, and ``note=`` is gone."""
    ledger = make_ledger(ledger_factory, name="status-message")
    item = new_item(ledger)
    new_box(ledger, "BOX", "grid96")
    stock = ledger.stocks.add(
        "lot", entity=item["id"], aliquots=2, box="BOX", slots=["A1", "A2"]
    )
    first, second = stock["aliquots"]

    # Omitting the message is legal and stores NULL.
    ledger.aliquots.status(first, "missing")
    event = events(ledger, first)[-1]
    assert event["event"] == "status"
    assert event["message"] is None

    # A given message is trimmed before it is stored.
    ledger.aliquots.status(first, "consumed", message="  used up  ")
    event = events(ledger, first)[-1]
    assert event["message"] == "used up"

    # A whitespace-only unforced message is a usage error and writes nothing.
    before = commit_count(ledger)
    with pytest.raises(UsageError, match="a status message must not be empty"):
        ledger.aliquots.status(second, "missing", message="   ")
    assert commit_count(ledger) == before

    # The old keyword is gone: no shim and no **kwargs.
    with pytest.raises(TypeError):
        ledger.aliquots.status(second, "missing", note="old")  # type: ignore[call-arg]


def test_creation_accepts_any_status_in_the_set(ledger_factory) -> None:
    ledger = make_ledger(ledger_factory, name="created-consumed")
    item = new_item(ledger)
    stock = ledger.stocks.add(
        "lot", entity=item["id"], aliquots=2, status="consumed"
    )
    statuses = ledger.driver.query(
        "SELECT `status` FROM `aliquot` WHERE `stock_id` = %s ORDER BY `seq`",
        (stock["id"],),
    )
    assert statuses == [{"status": "consumed"}, {"status": "consumed"}]
    # A later change away from ``consumed`` still needs --force.
    with pytest.raises(LedgerError, match="final status"):
        ledger.aliquots.status(stock["aliquots"][0], "stored")
    # ``aliquot add`` takes a creation status too.
    added = ledger.aliquots.add(stock["id"], count=1, status="missing")
    assert ledger.driver.query(
        "SELECT `status` FROM `aliquot` WHERE `id` = %s", (added["ids"][0],)
    ) == [{"status": "missing"}]
    # A status outside the stock kind's set is a usage error.
    with pytest.raises(UsageError, match="unknown status 'quarantined'"):
        ledger.aliquots.add(stock["id"], count=1, status="quarantined")
    with pytest.raises(UsageError, match="unknown status 'quarantined'"):
        ledger.stocks.add(
            "lot", entity=item["id"], aliquots=1, status="quarantined"
        )


def test_cryo_set_uses_stored_on_creation_and_has_no_consumed(ledger_factory) -> None:
    # A legacy ``cryo`` set (no ``pending``, no ``consumed``) - the fixed model
    # replaced it, so the fixture comes from tests/data/presets/legacy.
    from conftest import PRESETS

    ledger = ledger_factory([PRESETS / "legacy"], name="cryo")
    line = ledger.entities.add("cell_line", name="HEK293T")
    new_box(ledger, "CRYO", "freezer")
    # The cryo set has no ``pending``: creation without a box would default to
    # ``stored``, which is in stock, so it needs a position (spec 5).
    with pytest.raises(LedgerError, match="must be placed"):
        ledger.stocks.add("cell_vial", entity=line["id"], aliquots=1)
    vial = ledger.stocks.add(
        "cell_vial", entity=line["id"], aliquots=1, box="CRYO"
    )
    alq = vial["aliquots"][0]
    row = ledger.driver.query(
        "SELECT `status`, `box_id`, `slot` FROM `aliquot` WHERE `id` = %s", (alq,)
    )[0]
    assert row["status"] == "stored"
    assert row["box_id"] is not None and row["slot"] is None
    with pytest.raises(UsageError, match="unknown status 'pending'"):
        ledger.aliquots.status(alq, "pending")
    # The cryo set has no ``consumed`` either: a zero quantity keeps the status
    # and the position.
    ledger.driver.execute(
        "UPDATE `aliquot` SET `quantity` = 1, `unit` = 'ml' WHERE `id` = %s", (alq,)
    )
    ledger.driver.commit("test: give the vial a quantity")
    used = ledger.aliquots.use(alq, "1", "ml", message="thawed for the assay")
    assert used["status"] == "stored"
    assert Decimal(used["quantity"]) == Decimal("0")
    assert ledger.driver.query(
        "SELECT `box_id` FROM `aliquot` WHERE `id` = %s", (alq,)
    )[0]["box_id"] is not None
    # A thawed cryo vial is a plain unforced transition, and it leaves stock,
    # so the position is cleared.
    thawed = ledger.aliquots.status(alq, "thawed")
    assert thawed["status"] == "thawed"
    assert ledger.driver.query(
        "SELECT `box_id`, `slot` FROM `aliquot` WHERE `id` = %s", (alq,)
    ) == [{"box_id": None, "slot": None}]
