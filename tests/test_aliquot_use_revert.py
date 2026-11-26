"""``tm aliquot use --revert-last`` at the core layer: plans, previews, reversals.

Tetramark-3w6.2.  The core owns the whole decision: :func:`plan_aliquot_use`
reads one ledger and describes the write (what changes, from what state to
what, and for a reversal which ``use`` event it undoes), and
:func:`apply_aliquot_use` writes that plan after re-deriving it inside the
write session.  The CLI only prints the plan and asks the one ``[y/N]``
question, so everything the spec fixes is proved here:

* a partial, a whole and an unknown-quantity use all restore the exact
  pre-use quantity, unit, status and placement (rules 1 and 3);
* the target is the latest original ``use``, ordered ``at`` then ``id``, and
  a second reversal of the same use never steps back to an earlier one;
* a later quantity/unit/status/placement change blocks — even a change that
  put the value back — while label and alias changes are allowed;
* a history that cannot establish the pre-use state (a use whose commit
  changed no row, two events in one commit, a phantom use) is refused
  instead of guessed (rule 2);
* the restored row is validated, a slot somebody else took is named, and the
  retry succeeds once it is free (rule 4);
* a plan re-derived inside the write session differs -> "rerun", nothing
  written (rule 6), and an injected write failure leaves the row, the events
  and the commit history exactly as they were (rule 5);
* ``--dry-run`` collects the statements and writes nothing, ``--no-commit``
  keeps the row and the event together in the working set.

The spec-8 fixture comes from ``tests/test_migrate.py`` because the migration
step is what makes a pre-upgrade ``use`` reversible; the contradictory-evidence
case of that fixture lives there too.
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal
from typing import Any

import pytest

from tetramark.core.events import record_event
from tetramark.core.ledger import Ledger, open as open_ledger
from tetramark.core.objects import (
    AliquotUsePlan,
    apply_aliquot_use,
    plan_aliquot_use,
)
from tetramark.core.writelock import write_lock_holder
from tetramark.errors import LedgerError, UsageError

from conftest import PRESETS

BASE = PRESETS / "base"


@pytest.fixture
def script_env(dolt_env: dict[str, str]) -> dict[str, str]:
    """The environment a ``migration/…/migrate.py`` subprocess runs with."""
    import os

    return {**os.environ, **dolt_env}

#: The keys a reversal result carries: the ordinary use keeps its old shape.
REVERT_RESULT_KEYS = {
    "object",
    "id",
    "reverts_event_id",
    "message",
    "old_status",
    "status",
    "quantity",
    "unit",
    "box_id",
    "slot",
    "restored_amount",
    "target_amount",
    "target_unit",
    "target_event_at",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_ledger(ledger_factory, name: str = "use-revert") -> Ledger:
    return ledger_factory([BASE], name=name, id_prefix="77")


def build(ledger_factory, *, name: str = "use-revert", tubes: int = 2, **stock: Any):
    """Two 200 ml tubes in ``BOX`` A1/A2 plus a stored item to draw from."""
    ledger = make_ledger(ledger_factory, name=name)
    item = ledger.entities.add("item", name="alpha")
    box = ledger.boxes.add("grid96", "BOX")
    slots = [f"A{index}" for index in range(1, tubes + 1)]
    quantity = stock.pop("quantity", 200)
    unit = stock.pop("unit", "ml")
    created = ledger.stocks.add(
        "lot",
        entity=item["id"],
        aliquots=tubes,
        box="BOX",
        slots=slots,
        quantity=quantity,
        unit=unit,
        values={"supplier": "test"},
        **stock,
    )
    return ledger, box, created["id"], created["aliquots"]


def commit_count(ledger: Ledger) -> int:
    return int(ledger.driver.query("SELECT COUNT(*) AS n FROM dolt_log")[0]["n"])


def dirty_tables(ledger: Ledger) -> set[str]:
    return {
        str(row["table_name"])
        for row in ledger.driver.query("SELECT * FROM dolt_status")
    }


def events(ledger: Ledger, aliquot_id: str) -> list[dict[str, Any]]:
    return ledger.driver.query(
        "SELECT `id`, `event`, `reverts_event_id`, `amount`, `unit`, "
        "`from_box_id`, `from_slot`, `to_box_id`, `to_slot`, `old_status`, "
        "`new_status`, `forced`, `actor`, `message`, `at` "
        "FROM `aliquot_event` WHERE `aliquot_id` = %s ORDER BY `id`",
        (aliquot_id,),
    )


def row(ledger: Ledger, aliquot_id: str) -> dict[str, Any]:
    rows = ledger.driver.query(
        "SELECT `quantity`, `unit`, `status`, `box_id`, `slot` "
        "FROM `aliquot` WHERE `id` = %s",
        (aliquot_id,),
    )
    assert len(rows) == 1
    return rows[0]


def tube(ledger: Ledger, stock_id: str, *, status: str = "stored", **kwargs: Any) -> str:
    """One extra tube on ``stock_id``; ``quantity``/``unit`` default to unknown."""
    added = ledger.aliquots.add(stock_id, status=status, **kwargs)
    return str(added["ids"][0])


# ---------------------------------------------------------------------------
# The plan: what the preview shows, decided in one read
# ---------------------------------------------------------------------------


def test_plan_describes_a_partial_use_without_writing(ledger_factory) -> None:
    """The plan is read-only: quantity, unit and status before and after."""
    ledger, box, _stock, (first, _second) = build(ledger_factory, name="plan-partial")
    before = commit_count(ledger)

    plan = plan_aliquot_use(ledger, first, "50", "ml", message="  prep  ")

    assert isinstance(plan, AliquotUsePlan)
    assert plan.object == "aliquot"
    assert plan.id == first
    assert plan.ref == first
    assert plan.operation == "use"
    assert plan.is_revert is False
    assert plan.message == "prep"
    assert plan.amount == Decimal("50")
    assert plan.unit == "ml"
    assert plan.quantity_before == Decimal("200")
    assert plan.unit_before == "ml"
    assert plan.status_before == "stored"
    assert plan.box_before == box["id"]
    assert plan.box_name_before == "BOX"
    assert plan.slot_before == "A1"
    assert plan.quantity_after == Decimal("150.000000")
    assert plan.unit_after == "ml"
    assert plan.status_after == "stored"
    assert plan.box_after == box["id"]
    assert plan.slot_after == "A1"
    assert plan.target_event_id is None
    assert plan.target_event_at is None
    assert plan.branch
    assert commit_count(ledger) == before
    assert dirty_tables(ledger) == set()
    # Reading a plan never writes: the row is untouched.
    assert Decimal(str(row(ledger, first)["quantity"])) == Decimal("200")


def test_plan_describes_a_whole_use_of_a_tube_without_a_quantity(ledger_factory) -> None:
    """An unknown quantity stays unknown; the plan only promises the status."""
    ledger, box, stock_id, _tubes = build(ledger_factory, name="plan-unknown", tubes=1)
    unknown = tube(ledger, stock_id, box="BOX", slots=["A9"])

    plan = plan_aliquot_use(ledger, unknown, message="used it all")

    assert plan.amount is None
    assert plan.unit is None
    assert plan.quantity_before is None
    assert plan.quantity_after is None
    assert plan.unit_after is None
    assert plan.status_before == "stored"
    assert plan.status_after == "consumed"
    assert plan.box_before == box["id"]
    assert plan.box_after is None
    assert plan.slot_before == "A9"
    assert plan.slot_after is None


def test_plan_keeps_the_historical_validation_errors(ledger_factory) -> None:
    """The plan is where the old ``use`` errors now live."""
    ledger, _box, stock_id, (first, _second) = build(ledger_factory, name="plan-errors")

    with pytest.raises(UsageError, match="message is required"):
        plan_aliquot_use(ledger, first, "1", "ml", message="   ")
    with pytest.raises(UsageError, match="must be given together"):
        plan_aliquot_use(ledger, first, "1", message="prep")
    with pytest.raises(UsageError, match="must be given together"):
        plan_aliquot_use(ledger, first, unit="ml", message="prep")
    with pytest.raises(UsageError, match="positive|decimal"):
        plan_aliquot_use(ledger, first, "nope", "ml", message="prep")
    with pytest.raises(UsageError, match="positive"):
        plan_aliquot_use(ledger, first, "0", "ml", message="prep")
    with pytest.raises(LedgerError, match="unit mismatch"):
        plan_aliquot_use(ledger, first, "1", "ul", message="prep")
    with pytest.raises(LedgerError, match="only"):
        plan_aliquot_use(ledger, first, "201", "ml", message="prep")
    with pytest.raises(UsageError, match="cannot be combined"):
        plan_aliquot_use(ledger, first, "1", "ml", message="prep", revert_last=True)
    with pytest.raises(LedgerError, match="unknown aliquot"):
        plan_aliquot_use(ledger, "nope.1", "1", "ml", message="prep")

    unknown = tube(ledger, stock_id, status="pending")
    with pytest.raises(LedgerError, match="no quantity"):
        plan_aliquot_use(ledger, unknown, "1", "ml", message="prep")


def test_apply_rejects_something_that_is_not_a_plan(ledger_factory) -> None:
    """The write path takes a plan, not loose arguments."""
    ledger, _box, _stock, (first, _second) = build(ledger_factory, name="apply-not-plan")
    before = commit_count(ledger)

    with pytest.raises(UsageError, match="plan_aliquot_use"):
        apply_aliquot_use(ledger, "not a plan")  # type: ignore[arg-type]

    assert commit_count(ledger) == before


# ---------------------------------------------------------------------------
# Restoration: partial, whole, unknown, zero
# ---------------------------------------------------------------------------


def test_revert_restores_a_partial_use(ledger_factory) -> None:
    """Rule 3: the row, the status and the event of a partial use."""
    ledger, box, _stock, (first, _second) = build(ledger_factory, name="revert-partial")
    used = ledger.aliquots.use(first, "50", "ml", message="prep")
    assert used["quantity"] == "150.000000"
    use_event = events(ledger, first)[-1]
    assert use_event["event"] == "use"

    plan = plan_aliquot_use(ledger, first, message="  undo  ", revert_last=True)
    assert plan.is_revert is True
    assert plan.operation == "revert_use"
    assert plan.message == "undo"
    assert plan.quantity_before == Decimal("150.000000")
    assert plan.quantity_after == Decimal("200")
    assert plan.status_before == "stored"
    assert plan.status_after == "stored"
    assert plan.amount == Decimal("50")
    assert plan.unit == "ml"
    assert plan.target_event_id == use_event["id"]
    assert plan.target_event_message == "prep"
    assert plan.target_amount == Decimal("50")
    assert plan.target_unit == "ml"
    assert plan.target_event_at == use_event["at"]
    assert plan.box_after == box["id"]
    assert plan.slot_after == "A1"

    before = commit_count(ledger)
    revoked = apply_aliquot_use(ledger, plan)

    assert commit_count(ledger) == before + 1
    assert set(revoked) == REVERT_RESULT_KEYS
    assert revoked["object"] == "aliquot"
    assert revoked["id"] == first
    assert revoked["reverts_event_id"] == use_event["id"]
    assert revoked["message"] == "undo"
    assert revoked["old_status"] == "stored"
    assert revoked["status"] == "stored"
    assert revoked["quantity"] == "200.000000"
    assert revoked["unit"] == "ml"
    assert revoked["box_id"] == box["id"]
    assert revoked["slot"] == "A1"
    assert revoked["restored_amount"] == "50"
    assert revoked["target_amount"] == "50"
    assert revoked["target_unit"] == "ml"
    assert revoked["target_event_at"] == use_event["at"]

    # The row is exactly the pre-use one again.
    current = row(ledger, first)
    assert Decimal(str(current["quantity"])) == Decimal("200.000000")
    assert current["status"] == "stored"
    assert current["box_id"] == box["id"]
    assert current["slot"] == "A1"

    # The audit: a new ``revert_use`` row that names the use, the original
    # untouched, one commit with the reversal's message.
    reversal = events(ledger, first)[-1]
    assert reversal["event"] == "revert_use"
    assert reversal["reverts_event_id"] == use_event["id"]
    assert Decimal(str(reversal["amount"])) == Decimal("50")
    assert reversal["unit"] == "ml"
    # The placement did not move, so the reversal names no position.
    assert reversal["from_box_id"] is None
    assert reversal["from_slot"] is None
    assert reversal["to_box_id"] is None
    assert reversal["to_slot"] is None
    assert reversal["old_status"] == "stored"
    assert reversal["new_status"] == "stored"
    assert reversal["forced"] in (0, False)
    assert reversal["message"] == "undo"
    assert events(ledger, first)[-2] == use_event
    assert dirty_tables(ledger) == set()
    assert ledger.check().passed


def test_revert_restores_a_whole_use_status_and_placement(ledger_factory) -> None:
    """Rule 3: an emptied tube comes back with its old status and slot."""
    ledger, box, _stock, (first, _second) = build(ledger_factory, name="revert-whole")
    used = ledger.aliquots.use(first, message="used it all")
    assert used["status"] == "consumed"
    assert row(ledger, first)["box_id"] is None
    use_event = events(ledger, first)[-1]

    plan = plan_aliquot_use(ledger, first, message="undo", revert_last=True)
    assert plan.quantity_before == Decimal("0.000000")
    assert plan.quantity_after == Decimal("200")
    assert plan.status_before == "consumed"
    assert plan.status_after == "stored"
    assert plan.box_before is None
    assert plan.box_after == box["id"]
    assert plan.box_name_after == "BOX"
    assert plan.slot_after == "A1"

    revoked = apply_aliquot_use(ledger, plan)

    assert revoked["quantity"] == "200.000000"
    assert revoked["status"] == "stored"
    assert revoked["box_id"] == box["id"]
    assert revoked["slot"] == "A1"
    assert revoked["old_status"] == "consumed"
    assert revoked["restored_amount"] == "200"

    reversal = events(ledger, first)[-1]
    assert reversal["event"] == "revert_use"
    assert reversal["reverts_event_id"] == use_event["id"]
    # The placement moved, so both sides are recorded.
    assert reversal["from_box_id"] is None
    assert reversal["from_slot"] is None
    assert reversal["to_box_id"] == box["id"]
    assert reversal["to_slot"] == "A1"
    assert reversal["old_status"] == "consumed"
    assert reversal["new_status"] == "stored"
    # ``consumed -> stored`` is not an allowed forward transition, and the
    # reversal is not a ``status`` event, so ``tm check`` accepts it.
    assert ledger.check().passed


def test_revert_restores_an_unknown_quantity_tube(ledger_factory) -> None:
    """A quantity that was unknown before use stays unknown (rule 3)."""
    ledger, box, stock_id, _tubes = build(ledger_factory, name="revert-unknown", tubes=1)
    unknown = tube(ledger, stock_id, box="BOX", slots=["A9"])
    assert row(ledger, unknown)["quantity"] is None
    used = ledger.aliquots.use(unknown, message="used it all")
    assert used["used_amount"] is None
    assert used["quantity"] is None
    assert used["status"] == "consumed"

    plan = plan_aliquot_use(ledger, unknown, message="undo", revert_last=True)
    assert plan.amount is None
    assert plan.quantity_after is None
    assert plan.unit_after is None
    assert plan.status_after == "stored"
    assert plan.slot_after == "A9"

    revoked = apply_aliquot_use(ledger, plan)

    assert revoked["quantity"] is None
    assert revoked["unit"] is None
    assert revoked["restored_amount"] is None
    assert revoked["status"] == "stored"
    assert revoked["slot"] == "A9"
    current = row(ledger, unknown)
    assert current["quantity"] is None
    assert current["status"] == "stored"
    assert current["box_id"] == box["id"]
    assert current["slot"] == "A9"
    assert ledger.check().passed


def test_a_whole_use_of_an_empty_tube_is_still_used_and_reversible(ledger_factory) -> None:
    """A known zero can be used whole; its reversal restores the placement.

    Taking nothing is only meaningless when nothing else changed: an empty tube
    that is in a box is emptied *of its position*, so the use and its reversal
    a real events.  An explicit ``0`` amount stays a usage error either way.
    """
    ledger, box, stock_id, _tubes = build(ledger_factory, name="revert-zero", tubes=1)
    empty = tube(ledger, stock_id, quantity=0, unit="ml", box="BOX", slots=["A5"])
    assert Decimal(str(row(ledger, empty)["quantity"])) == Decimal("0")

    with pytest.raises(UsageError, match="positive"):
        ledger.aliquots.use(empty, "0", "ml", message="nothing")

    used = ledger.aliquots.use(empty, message="nothing left")
    assert used["used_amount"] == "0"
    assert used["quantity"] == "0.000000"
    assert used["status"] == "consumed"
    assert row(ledger, empty)["slot"] is None

    plan = plan_aliquot_use(ledger, empty, message="undo", revert_last=True)
    assert plan.amount == Decimal("0")
    assert plan.target_amount == Decimal("0")
    assert plan.quantity_after == Decimal("0.000000")
    assert plan.status_after == "stored"
    assert plan.slot_after == "A5"

    revoked = apply_aliquot_use(ledger, plan)
    assert revoked["restored_amount"] == "0"
    assert revoked["quantity"] == "0.000000"
    assert revoked["status"] == "stored"
    assert revoked["slot"] == "A5"
    assert ledger.check().passed


def test_an_amount_below_the_stored_precision_keeps_the_ordinary_result(ledger_factory) -> None:
    ledger, _box, _stock, (first, _second) = build(ledger_factory, name="precision")
    before = commit_count(ledger)
    plan = plan_aliquot_use(ledger, first, "0.0000004", "ml", message="tiny")
    assert plan.quantity_before == plan.quantity_after == Decimal("200")

    used = apply_aliquot_use(ledger, plan)
    assert used["used_amount"] == "0.0000004"
    assert used["quantity"] == "199.9999996"
    assert Decimal(str(row(ledger, first)["quantity"])) == Decimal("200")
    assert commit_count(ledger) == before + 1
    assert dirty_tables(ledger) == set()


# ---------------------------------------------------------------------------
# Which use, and only once
# ---------------------------------------------------------------------------


def test_the_latest_use_is_selected_and_a_tie_breaks_by_id(ledger_factory) -> None:
    """Rule 1: ``at`` then ``id``; the earlier use is never touched."""
    ledger, _box, _stock, (first, _second) = build(ledger_factory, name="latest-use")
    ledger.aliquots.use(first, "50", "ml", message="first")
    second_use = ledger.aliquots.use(first, "50", "ml", message="second")
    assert second_use["quantity"] == "100.000000"
    # Same timestamp: the audit order is ``at`` then ``id``.
    ledger.driver.execute(
        "UPDATE `aliquot_event` SET `at` = '2020-01-01 00:00:00' "
        "WHERE `aliquot_id` = %s AND `event` = 'use'",
        (first,),
    )

    plan = plan_aliquot_use(ledger, first, message="undo the second", revert_last=True)
    assert plan.target_event_message == "second"
    assert plan.target_amount == Decimal("50")
    assert plan.quantity_before == Decimal("100.000000")
    assert plan.quantity_after == Decimal("150.000000")

    revoked = apply_aliquot_use(ledger, plan)
    assert revoked["quantity"] == "150.000000"
    assert len([e for e in events(ledger, first) if e["event"] == "use"]) == 2
    # The older use is intact and still the latest *unreversed* one.
    assert [e["message"] for e in events(ledger, first) if e["event"] == "use"] == [
        "first",
        "second",
    ]


def test_a_second_reversal_is_refused_and_never_falls_back(ledger_factory) -> None:
    """One reversal per use; the older use never becomes the new target."""
    ledger, _box, _stock, (first, _second) = build(ledger_factory, name="revert-twice")
    earlier = ledger.aliquots.use(first, "50", "ml", message="first")
    later = ledger.aliquots.use(first, "50", "ml", message="second")
    assert later["quantity"] == "100.000000"

    applied = apply_aliquot_use(
        ledger, plan_aliquot_use(ledger, first, message="undo the second", revert_last=True)
    )
    assert applied["reverts_event_id"] == events(ledger, first)[-2]["id"]
    assert applied["quantity"] == "150.000000"
    before = commit_count(ledger)

    with pytest.raises(
        LedgerError, match="already been reverted by event .*; a revert only undoes"
    ):
        plan_aliquot_use(ledger, first, message="undo again", revert_last=True)

    assert commit_count(ledger) == before
    # The earlier use is exactly as it was recorded and still unreversed.
    assert Decimal(str(row(ledger, first)["quantity"])) == Decimal("150")
    reversals = [e for e in events(ledger, first) if e["event"] == "revert_use"]
    assert len(reversals) == 1
    assert reversals[0]["reverts_event_id"] == events(ledger, first)[-2]["id"]
    assert earlier["message"] == "first"


def test_a_use_without_a_recorded_use_is_refused(ledger_factory) -> None:
    """An untouched tube has nothing to revert."""
    ledger, _box, _stock, (first, _second) = build(ledger_factory, name="no-use")
    before = commit_count(ledger)

    with pytest.raises(LedgerError, match="has no recorded use to revert"):
        plan_aliquot_use(ledger, first, message="undo", revert_last=True)

    assert commit_count(ledger) == before


# ---------------------------------------------------------------------------
# Conflicts: later physical changes block, labels do not
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "change, message",
    [
        (("edit", {"values": {"quantity": "149"}}), "changed the quantity"),
        (("edit", {"values": {"unit": "ul"}}), "changed the unit"),
        (("move", {"box": "BOX", "slot": "B1"}), "changed the placement"),
        (("status", {"status": "missing"}), "set the status to 'missing'"),
    ],
)
def test_a_later_physical_change_blocks_the_revert(
    ledger_factory, change: tuple[str, dict[str, Any]], message: str
) -> None:
    """Rule 2: quantity, unit, status and placement are not reversible."""
    ledger, _box, _stock, (first, _second) = build(
        ledger_factory, name=f"conflict-{change[0]}"
    )
    ledger.aliquots.use(first, "50", "ml", message="prep")
    method, kwargs = change
    getattr(ledger.aliquots, method)(first, **kwargs, message="a later change")
    before = commit_count(ledger)
    rows_before = events(ledger, first)

    with pytest.raises(LedgerError, match=f"cannot revert use event .*{message}"):
        plan_aliquot_use(ledger, first, message="undo", revert_last=True)

    assert commit_count(ledger) == before
    assert events(ledger, first) == rows_before


def test_a_change_that_put_the_value_back_still_blocks(ledger_factory) -> None:
    """A later ``edit`` of quantity blocks even when it restored the old value.

    Dolt's diff tables compare the working set with ``HEAD``, so a change and
    its undo leave no diff row at all; the event trail still records it, and
    rule 2 judges the event, not the current value.
    """
    ledger, _box, _stock, (first, _second) = build(ledger_factory, name="conflict-back")
    ledger.aliquots.use(first, "50", "ml", message="prep")
    ledger.aliquots.edit(first, values={"quantity": "155"}, message="recount")
    ledger.aliquots.edit(first, values={"quantity": "150"}, message="recount back")
    assert Decimal(str(row(ledger, first)["quantity"])) == Decimal("150")

    with pytest.raises(LedgerError, match="a later edit .* changed the quantity"):
        plan_aliquot_use(ledger, first, message="undo", revert_last=True)


def test_an_uncommitted_change_that_was_undone_still_blocks(ledger_factory) -> None:
    """The working set is judged too: no commit, no diff row, an event left."""
    ledger = make_ledger(ledger_factory, name="conflict-working")
    item = ledger.entities.add("item", name="alpha")
    ledger.boxes.add("grid96", "BOX")
    stock = ledger.stocks.add(
        "lot", entity=item["id"], aliquots=2, quantity=200, unit="ml",
        box="BOX", slots=["A1", "A2"], values={"supplier": "test"},
    )
    first = stock["aliquots"][0]
    working = open_ledger(ledger.path, no_commit=True)
    try:
        working.aliquots.use(first, "50", "ml", message="prep")
        working.aliquots.edit(first, values={"quantity": "160"}, message="recount")
        working.aliquots.edit(first, values={"quantity": "150"}, message="recount back")
        # The uncommitted use still differs from HEAD, so the row is dirty too;
        # what matters is that *no diff row* records the edit and its undo.
        assert dirty_tables(working) == {"aliquot", "aliquot_event"}

        with pytest.raises(LedgerError, match="a later edit .* changed the quantity"):
            plan_aliquot_use(working, first, message="undo", revert_last=True)
    finally:
        working.close()


def test_label_and_alias_changes_are_allowed_and_kept(ledger_factory) -> None:
    """Rule 2: only label and alias changes may follow a use."""
    ledger, _box, _stock, (first, _second) = build(ledger_factory, name="label-allowed")
    ledger.aliquots.use(first, "50", "ml", message="prep")
    ledger.aliquots.label(first, "TUBE-7")
    ledger.aliases.add("aliquot", first, "seventh")
    used_id = events(ledger, first)[-1]["id"]

    plan = plan_aliquot_use(ledger, first, message="undo", revert_last=True)
    assert plan.target_event_id == used_id
    revoked = apply_aliquot_use(ledger, plan)

    assert revoked["quantity"] == "200.000000"
    # The label and its alias survive the reversal untouched.
    assert ledger.aliquots.show(first)["label"] == "TUBE-7"
    assert ledger.aliquots.show("seventh")["id"] == first
    assert ledger.check().passed


def test_an_occupied_slot_is_named_and_a_later_retry_succeeds(ledger_factory) -> None:
    """Rule 4: refuse before writing, name the occupant, retry when free."""
    ledger, box, _stock, (first, second) = build(ledger_factory, name="occupant")
    ledger.aliquots.use(first, message="used it all")
    assert row(ledger, first)["slot"] is None
    # Somebody else took the freed position.
    ledger.aliquots.move(second, box="BOX", slot="A1", message="closer")
    before = commit_count(ledger)
    events_before = events(ledger, first)

    with pytest.raises(
        LedgerError,
        match=f"slot 'A1' in box 'BOX' is now occupied by aliquot '{second}'; "
        "move that tube and rerun the revert",
    ):
        plan_aliquot_use(ledger, first, message="undo", revert_last=True)

    assert commit_count(ledger) == before
    assert events(ledger, first) == events_before
    assert dirty_tables(ledger) == set()

    # Free the position, then the same reversal works.
    ledger.aliquots.move(second, box="BOX", slot="A3", message="freed")
    plan = plan_aliquot_use(ledger, first, message="undo", revert_last=True)
    revoked = apply_aliquot_use(ledger, plan)
    assert revoked["slot"] == "A1"
    assert row(ledger, first)["box_id"] == box["id"]
    assert ledger.check().passed


# ---------------------------------------------------------------------------
# Ambiguous evidence is an error, never a guess
# ---------------------------------------------------------------------------


def test_a_use_whose_commit_changed_no_row_is_refused(ledger_factory) -> None:
    """An event without a row change cannot say what it did."""
    ledger, _box, _stock, (first, _second) = build(ledger_factory, name="phantom-use")
    with ledger.write("phantom use", verb="use", object="aliquot") as session:
        record_event(
            session,
            aliquot_id=first,
            event="use",
            amount=1,
            unit="ml",
            old_status="stored",
            new_status="stored",
            actor=ledger.actor,
            message="a use that changed nothing",
        )

    with pytest.raises(
        LedgerError, match="changed no row of the tube, so the ledger cannot establish"
    ):
        plan_aliquot_use(ledger, first, message="undo", revert_last=True)


def test_two_events_of_one_tube_in_one_commit_are_refused(ledger_factory) -> None:
    """One row change cannot be attributed to two uses in the same commit."""
    ledger, _box, _stock, (first, _second) = build(ledger_factory, name="double-use")
    with ledger.write("two uses", verb="use", object="aliquot") as session:
        for amount, left in (("10", "190"), ("10", "180")):
            session.execute(
                "UPDATE `aliquot` SET `quantity` = %s WHERE `id` = %s", (left, first)
            )
            record_event(
                session,
                aliquot_id=first,
                event="use",
                amount=amount,
                unit="ml",
                old_status="stored",
                new_status="stored",
                actor=ledger.actor,
                message="in one commit",
            )

    with pytest.raises(
        LedgerError,
        match=r"recorded 2 events of that tube, so its row change cannot be "
        r"attributed to one use \(refusing to guess\)",
    ):
        plan_aliquot_use(ledger, first, message="undo", revert_last=True)


def test_an_event_that_disagrees_with_the_row_history_is_refused(ledger_factory) -> None:
    """A recorded amount the row did not lose is contradictory evidence."""
    ledger, _box, _stock, (first, _second) = build(ledger_factory, name="disagree")
    with ledger.write("inconsistent use", verb="use", object="aliquot") as session:
        session.execute(
            "UPDATE `aliquot` SET `quantity` = 190 WHERE `id` = %s", (first,)
        )
        record_event(
            session,
            aliquot_id=first,
            event="use",
            amount=20,
            unit="ml",
            old_status="stored",
            new_status="stored",
            actor=ledger.actor,
            message="says 20, took 10",
        )

    with pytest.raises(
        LedgerError,
        match="the use event and the row history do not agree .*refusing to guess",
    ):
        plan_aliquot_use(ledger, first, message="undo", revert_last=True)


# ---------------------------------------------------------------------------
# Revalidation, rollback and the two non-committing modes
# ---------------------------------------------------------------------------


def test_the_write_session_revalidates_and_asks_for_a_rerun(ledger_factory) -> None:
    """Rule 6: a plan is never applied to a row that moved after the preview."""
    ledger, _box, _stock, (first, _second) = build(ledger_factory, name="rerun")
    ledger.aliquots.use(first, "50", "ml", message="prep")
    plan = plan_aliquot_use(ledger, first, message="undo", revert_last=True)
    assert plan.quantity_before == Decimal("150.000000")
    commits = commit_count(ledger)

    # Somebody changed the tube between the preview and the confirmation.
    ledger.aliquots.edit(first, values={"quantity": "160"}, message="concurrent recount")
    assert commit_count(ledger) == commits + 1
    events_after_edit = events(ledger, first)

    with pytest.raises(LedgerError, match="rerun the command"):
        apply_aliquot_use(ledger, plan)

    assert commit_count(ledger) == commits + 1
    assert events(ledger, first) == events_after_edit
    assert Decimal(str(row(ledger, first)["quantity"])) == Decimal("160")
    assert dirty_tables(ledger) == set()


def test_a_concurrent_ordinary_use_also_reruns(ledger_factory) -> None:
    """The same gate protects the ordinary use, not only the reversal."""
    ledger, _box, _stock, (first, second) = build(ledger_factory, name="rerun-use")
    plan = plan_aliquot_use(ledger, first, "50", "ml", message="prep")
    ledger.aliquots.use(first, "100", "ml", message="somebody else first")

    with pytest.raises(LedgerError, match="changed after the preview"):
        apply_aliquot_use(ledger, plan)

    assert Decimal(str(row(ledger, first)["quantity"])) == Decimal("100")
    assert plan.quantity_after == Decimal("150.000000")


def test_a_branch_change_after_the_preview_is_refused(ledger_factory) -> None:
    """The plan is pinned to the branch the preview read."""
    ledger, _box, _stock, (first, _second) = build(ledger_factory, name="branch-pin")
    plan = plan_aliquot_use(ledger, first, "50", "ml", message="prep")
    before = commit_count(ledger)

    pinned = dataclasses.replace(plan, branch="feature")
    with pytest.raises(
        LedgerError, match="branch changed from 'feature' to .*; rerun the command"
    ):
        apply_aliquot_use(ledger, pinned)

    assert commit_count(ledger) == before
    assert dirty_tables(ledger) == set()


def test_an_injected_write_failure_leaves_no_trace(
    ledger_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule 5: the row update and the event commit together, or not at all."""
    ledger, _box, _stock, (first, _second) = build(ledger_factory, name="rollback")
    ledger.aliquots.use(first, "50", "ml", message="prep")
    plan = plan_aliquot_use(ledger, first, message="undo", revert_last=True)
    row_before = row(ledger, first)
    events_before = events(ledger, first)
    commits_before = commit_count(ledger)

    def failing_commit_pending(*args: Any, **kwargs: Any):
        raise RuntimeError("injected commit failure")

    monkeypatch.setattr(ledger.driver, "commit_pending", failing_commit_pending)
    with pytest.raises(RuntimeError, match="injected commit failure"):
        apply_aliquot_use(ledger, plan)
    monkeypatch.undo()

    assert row(ledger, first) == row_before
    assert events(ledger, first) == events_before
    assert commit_count(ledger) == commits_before
    assert dirty_tables(ledger) == set()
    # The write lock was released on the failing path.
    assert write_lock_holder(ledger) is None
    # And the ledger is usable again: the very same plan now applies.
    revoked = apply_aliquot_use(ledger, plan)
    assert revoked["quantity"] == "200.000000"


def test_a_dry_run_collects_the_statements_and_writes_nothing(ledger_factory) -> None:
    """``--dry-run`` decides everything, prints the SQL, changes no row."""
    ledger, _box, _stock, (first, _second) = build(ledger_factory, name="dry-run")
    ledger.aliquots.use(first, "50", "ml", message="prep")
    commits = commit_count(ledger)
    row_before = row(ledger, first)
    dry = open_ledger(ledger.path, dry_run=True)
    try:
        plan = plan_aliquot_use(dry, first, message="undo", revert_last=True)
        revoked = apply_aliquot_use(dry, plan)
        session = dry.last_write
        assert session is not None
        assert session.dry_run is True
        assert len(session.statements) == 2
        assert "UPDATE `aliquot`" in session.sql
        assert "INSERT INTO `aliquot_event`" in session.sql
        assert revoked["reverts_event_id"] == plan.target_event_id
        assert revoked["quantity"] == "200.000000"
    finally:
        dry.close()

    assert commit_count(ledger) == commits
    assert row(ledger, first) == row_before


def test_no_commit_keeps_the_row_and_the_event_together(ledger_factory) -> None:
    """``--no-commit`` writes the pair into the working set, no Dolt commit."""
    ledger, _box, _stock, (first, _second) = build(ledger_factory, name="no-commit")
    commits = commit_count(ledger)
    working = open_ledger(ledger.path, no_commit=True)
    try:
        working.aliquots.use(first, "50", "ml", message="prep")
        plan = plan_aliquot_use(working, first, message="undo", revert_last=True)
        revoked = apply_aliquot_use(working, plan)
        assert revoked["quantity"] == "200.000000"
        assert revoked["reverts_event_id"] == plan.target_event_id
        assert commit_count(working) == commits
        # The row is back to its committed state, so only the event table is
        # dirty: the reversal row must be reviewed (and committed) later.
        assert dirty_tables(working) == {"aliquot_event"}
        assert [e["event"] for e in events(working, first)][-2:] == [
            "use",
            "revert_use",
        ]

        # A second uncommitted cycle cannot attribute its use to a single row
        # change: Dolt keeps one working-set snapshot per tube, so the ledger
        # refuses rather than guessing (rule 2).
        working.aliquots.use(first, "50", "ml", message="prep again")
        with pytest.raises(
            LedgerError, match=r"events of that tube, so its row change cannot be "
            r"attributed to one use \(refusing to guess\)"
        ):
            plan_aliquot_use(working, first, message="undo again", revert_last=True)
        # Still no Dolt commit: everything sits in the working set.
        assert commit_count(working) == commits
        assert dirty_tables(working) == {"aliquot", "aliquot_event"}
    finally:
        working.close()

    # Nothing was committed, so the committed history is exactly as before and
    # the uncommitted row and events survive the session that wrote them.
    reopened = open_ledger(ledger.path)
    try:
        assert commit_count(reopened) == commits
        assert dirty_tables(reopened) == {"aliquot", "aliquot_event"}
        assert [e["event"] for e in events(reopened, first)][-2:] == [
            "revert_use",
            "use",
        ]
    finally:
        reopened.close()


# ---------------------------------------------------------------------------
# A pre-upgrade (spec 8) use becomes reversible through the migration
# ---------------------------------------------------------------------------


def test_a_pre_upgrade_use_is_reversible_after_migration(
    ledger_factory, script_env: dict[str, str]
) -> None:
    """Rules 1-3 on a real spec-8 fixture: the migration only adds the column."""
    from test_migrate import SPEC_8_TO_9, event_rows, run_python, spec8_ledger

    ledger, alq, _other = spec8_ledger(ledger_factory, name="revert-spec8")
    # Written through the ordinary API, which works on spec 8: only the
    # reversal needs the new column.
    ledger.aliquots.use(alq, "1", "ul", message="a pre-upgrade use")
    rows_before = event_rows(ledger)
    use_id = next(
        int(row["id"])
        for row in rows_before
        if row["event"] == "use" and row["aliquot_id"] == alq
    )

    # Before the migration the core refuses the reversal with the upgrade text.
    with pytest.raises(LedgerError, match="8-to-9/migrate.py"):
        plan_aliquot_use(ledger, alq, message="undo", revert_last=True)

    migrated = run_python(SPEC_8_TO_9, "--ledger", str(ledger.path), env=script_env)
    assert migrated.returncode == 0, migrated.stdout + migrated.stderr
    ledger.read_meta()

    plan = plan_aliquot_use(ledger, alq, message="undo", revert_last=True)
    assert plan.target_event_id == use_id
    assert plan.quantity_before == Decimal("9.000000")
    assert plan.quantity_after == Decimal("10")
    assert plan.amount == Decimal("1")
    assert plan.unit == "ul"
    # The fixture's tubes were created ``pending`` and never placed, so the
    # restored tube is unplaced too.
    assert plan.status_after == "pending"
    assert plan.box_after is None

    revoked = apply_aliquot_use(ledger, plan)
    assert revoked["reverts_event_id"] == use_id
    assert revoked["quantity"] == "10.000000"
    # The original event rows are untouched: the reversal is a new row.
    assert event_rows(ledger)[: len(rows_before)] == rows_before
    assert ledger.check().passed
