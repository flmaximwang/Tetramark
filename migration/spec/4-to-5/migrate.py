"""spec 4 -> 5: one generated view per existing box, positions follow in stock.

Spec 5 (ticket 30) settles git-bug ``20b350c`` ("Box View Specification"): every
box that exists gets its **own** generated view, which shows what is physically
in it.  The per-box-kind ``v_box_<kind>`` views go away.  A tube that is no
longer in stock is no longer in a box, so the views never show tubes that are
gone.  Two shape changes follow:

* the generated box views are replaced: every managed view is dropped and
  recreated, then one ``v_box_<box name>`` view is generated per live ``box``
  row from the **spec-5** DDL (:func:`tetramark.schema.ddl.box_view_statements`).
  The shapes are the spec-5 ones: a grid pivot (``row`` + one column per column
  label, one row per row label, empty rows included), ``slot | occupants`` for
  named boxes and one ``occupant`` column for slotless boxes (zero rows when
  empty).  ``v_box_slots``, ``v_aliquot_location``, ``v_stock_status`` and the
  ``v_entity_<kind>``/``v_stock_<kind>`` views are recreated from
  :func:`tetramark.schema.ddl.view_statements` (which no longer emits box kind
  views).  The runner also regenerates the derived ``dolt_tests`` from the
  spec-5 schema in the same commit, so the position/grid/name rules are
  installed with the views they guard.
* an aliquot has a position **exactly when it is in stock** (spec 4 stored
  positions independently of status).  The step clears ``box_id``/``slot`` of
  every not-in-stock aliquot that still carries a position, and records one
  ``move`` event per cleared row with the old position in
  ``from_box_id``/``from_slot`` and ``to_box_id``/``to_slot`` NULL, so
  ``tm history`` still shows where it was (ordered by aliquot id, note
  ``spec 4-to-5``).

It **refuses** — before writing anything — in three cases, listing the rows and
the command that fixes them:

1. a box name cannot become a view name (spec 5's name rule, owned by
   :func:`tetramark.schema.model.box_name_problem`): ``v_box_<name>`` would
   collide with a core view, the name is longer than 58 characters, or it
   differs only in case from another box;
2. a grid box holds a child box (grid boxes hold tubes only);
3. an in-stock aliquot has **no** position.  The Lead decision for wave 11 is
   that in stock means physically placed, so such a row would fail the new
   position rule immediately; the step does not guess a box and lists
   ``tm aliquot move <id> --box NAME --slot S`` instead.

It also refuses a row whose status is not in its stock kind's status set (spec
4 already required it; without the set the step cannot tell in stock from not in
stock) and a box/stock kind the ledger's own schema does not define (the
precondition below).

Preconditions
-------------
A spec-4 ledger at base **3** with its own ``<ledger>/presets/`` copy of the
spec-4 presets (``grid96`` still a grid kind, ``tube`` status set present).  The
step reads the ledger's own copy, not the repository's, so it is safe to run
before the base 3 -> 4 rename.  **Upgrade order** for a spec-4/base-3 ledger:
this step first, then ``migration/presets/base/3-to-4`` (which requires spec 5).
Do not replace ``<ledger>/presets/`` with the repository's base-4 copy before
this step: the step's per-box views are generated from the kinds the ledger's
schema defines, and a kind the schema no longer knows is refused.

Run it by hand (``--dry-run`` first, as for every step)::

    uv run migration/spec/4-to-5/migrate.py --ledger PATH [--branch B] [--dry-run]

On a real ledger: run it on a Dolt branch, review the dry-run plan, and merge
only after the operator's OK; never push to ``origin`` without that OK.  The
step creates the restore point ``pre-migrate-spec-4`` before writing.
"""

from __future__ import annotations

from typing import Any, Iterable

from tetramark.core.ledger import Ledger
from tetramark.driver.base import literal
from tetramark.errors import LedgerError
from tetramark.migrate import MigrationStep, main
from tetramark.schema.ddl import (
    box_view_statements,
    is_managed_view,
    view_statements,
)
from tetramark.schema.loader import load_ledger_schema
from tetramark.schema.model import (
    EffectiveSchema,
    GeometryType,
    Kind,
    ObjectType,
    StatusSet,
    box_name_problem,
)

#: The note every cleared position's ``move`` event carries (the README lists
#: the cleared rows for the operator).
CLEARED_NOTE = "spec 4-to-5"


# ---------------------------------------------------------------------------
# Reads: the live rows the plan works from
# ---------------------------------------------------------------------------


def box_rows(ledger: Ledger) -> list[dict[str, Any]]:
    """Every live box, ordered by name (the order the views are generated in)."""
    return ledger.driver.query(
        "SELECT `id`, `name`, `kind` FROM `box` ORDER BY `name`"
    )


def _aliquot_rows(ledger: Ledger) -> list[dict[str, Any]]:
    """Every aliquot with the position and status columns the step reads."""
    return ledger.driver.query(
        "SELECT a.`id` AS `aliquot_id`, a.`stock_id`, a.`box_id`, a.`slot`, "
        "a.`status`, s.`kind` AS `stock_kind` "
        "FROM `aliquot` a JOIN `stock` s ON s.`id` = a.`stock_id` "
        "ORDER BY a.`id`"
    )


def _box_kind(ledger: Ledger, kind_name: str) -> Kind:
    """The loaded box kind, or a refusal naming the preset-copy precondition."""
    kind = ledger.schema.kinds.get(kind_name)
    if kind is None or kind.object is not ObjectType.box or kind.geometry is None:
        raise LedgerError(
            f"refusing to migrate spec 4 -> 5: a box names kind "
            f"{kind_name!r}, which the schema the ledger's own preset copy "
            "loads does not define as a box kind. Restore the ledger's "
            "spec-4 <ledger>/presets/ copy (the step reads the ledger's own "
            "copy, and a spec-4 box kind must still be there), then retry. "
            "Nothing was written."
        )
    return kind


def _stock_status_set(
    ledger: Ledger, kind_name: str, aliquot_id: str
) -> StatusSet:
    """The status set of a stock kind, or a refusal naming the precondition."""
    kind = ledger.schema.kinds.get(kind_name)
    status_set = kind.status_set() if kind is not None else None
    if kind is None or kind.object is not ObjectType.stock or status_set is None:
        raise LedgerError(
            f"refusing to migrate spec 4 -> 5: aliquot {aliquot_id!r} belongs "
            f"to a stock of kind {kind_name!r}, which the schema the ledger's "
            "own preset copy loads does not define with a status set. Restore "
            "the ledger's spec-4 <ledger>/presets/ copy, then retry. Nothing "
            "was written."
        )
    return status_set


def _box_is_slotted(ledger: Ledger, box_id: str) -> bool:
    rows = ledger.driver.query(
        "SELECT `kind` FROM `box` WHERE `id` = %s", (box_id,)
    )
    if not rows:
        raise LedgerError(
            f"refusing to migrate spec 4 -> 5: an aliquot is placed in box "
            f"{box_id!r}, but no such box row exists (a dangling box_id). Fix "
            "the row first. Nothing was written."
        )
    return _box_kind(ledger, str(rows[0]["kind"])).geometry.is_slotted  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# The three refusals (plus the status/preset preconditions)
# ---------------------------------------------------------------------------


def box_name_problems(ledger: Ledger) -> list[str]:
    """Box names that cannot become view names (spec 5's name rule)."""
    rows = ledger.driver.query("SELECT `name` FROM `box` ORDER BY `name`")
    names = [str(row["name"]) for row in rows]
    out: list[str] = []
    for name in names:
        problem = box_name_problem(name, names)
        if problem is not None:
            out.append(problem)
    return out


def grid_held_boxes(ledger: Ledger) -> list[str]:
    """Child boxes nested in a **grid** box (grid boxes hold tubes only)."""
    rows = ledger.driver.query(
        "SELECT c.`name` AS `child`, p.`name` AS `parent`, "
        "p.`kind` AS `parent_kind` "
        "FROM `box` c JOIN `box` p ON p.`id` = c.`parent_box_id` "
        "ORDER BY p.`name`, c.`name`"
    )
    out: list[str] = []
    for row in rows:
        kind = _box_kind(ledger, str(row["parent_kind"]))
        if kind.geometry is not None and kind.geometry.type is GeometryType.grid:
            out.append(
                f"{row['child']} is inside grid box {row['parent']} "
                f"({row['parent_kind']})"
            )
    return out


def out_of_set_aliquots(ledger: Ledger) -> list[str]:
    """Aliquots whose status is not in their stock kind's status set.

    A spec-4 ledger should not have any (``migration/spec/3-to-4`` refuses
    them); without the set the step cannot tell in stock from not in stock, so
    it refuses rather than clearing a position it does not understand.
    """
    out: list[str] = []
    for row in _aliquot_rows(ledger):
        aliquot_id = str(row["aliquot_id"])
        status_set = _stock_status_set(ledger, str(row["stock_kind"]), aliquot_id)
        if not status_set.has(str(row["status"])):
            out.append(
                f"{aliquot_id} ({row['stock_kind']}): {row['status']}"
            )
    return out


def unplaced_in_stock_aliquots(ledger: Ledger) -> list[str]:
    """In-stock aliquots with no (or an incomplete) position, with the fix.

    ``box_id`` NULL is no position; a slotted box without ``slot`` is half a
    position.  Both would fail the spec-5 position rule, so the step refuses
    and points at ``tm aliquot move`` (the Lead decision: in stock means
    physically placed; the step never guesses a location).
    """
    out: list[str] = []
    for row in _aliquot_rows(ledger):
        aliquot_id = str(row["aliquot_id"])
        status_set = _stock_status_set(ledger, str(row["stock_kind"]), aliquot_id)
        if str(row["status"]) not in status_set.in_stock_names:
            continue
        if row["box_id"] is None:
            out.append(
                f"{aliquot_id} ({row['stock_kind']}, status {row['status']}) "
                f"has no box: fix with: tm aliquot move {aliquot_id} "
                "--box NAME --slot S"
            )
        elif not row["slot"] and _box_is_slotted(ledger, str(row["box_id"])):
            out.append(
                f"{aliquot_id} ({row['stock_kind']}, status {row['status']}) "
                "is in a slotted box without a slot: fix with: "
                f"tm aliquot move {aliquot_id} --box NAME --slot S"
            )
    return out


def positioned_not_in_stock_aliquots(ledger: Ledger) -> list[dict[str, Any]]:
    """Not-in-stock aliquots that still carry a position (spec 4 shape)."""
    out: list[dict[str, Any]] = []
    for row in _aliquot_rows(ledger):
        status_set = _stock_status_set(
            ledger, str(row["stock_kind"]), str(row["aliquot_id"])
        )
        if str(row["status"]) in status_set.in_stock_names:
            continue
        if row["box_id"] is not None:
            out.append(row)
    return out


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


def _clear_position_statements(row: dict[str, Any], actor: str | None) -> list[str]:
    """Clear one position and record the ``move`` event that keeps its history."""
    aliquot_id = str(row["aliquot_id"])
    status = str(row["status"])
    return [
        "UPDATE `aliquot` SET `box_id` = NULL, `slot` = NULL "
        f"WHERE `id` = {literal(aliquot_id)}",
        "INSERT INTO `aliquot_event` ("
        "`aliquot_id`, `event`, `from_box_id`, `from_slot`, `to_box_id`, "
        "`to_slot`, `old_status`, `new_status`, `actor`, `note`"
        ") VALUES ("
        f"{literal(aliquot_id)}, 'move', {literal(row['box_id'])}, "
        f"{literal(row['slot'])}, NULL, NULL, {literal(status)}, "
        f"{literal(status)}, {literal(actor)}, {literal(CLEARED_NOTE)}"
        ")",
    ]


def _live_views(ledger: Ledger) -> list[str]:
    rows = ledger.driver.query(
        "SELECT `table_name` AS `name` FROM `information_schema`.`tables` "
        "WHERE `table_schema` = DATABASE() AND `table_type` = 'VIEW' "
        "ORDER BY `table_name`"
    )
    return [str(row["name"]) for row in rows if is_managed_view(str(row["name"]))]


def _refuse(problems: Iterable[str], message: str) -> None:
    listed = list(problems)
    if listed:
        raise LedgerError(
            f"refusing to migrate spec 4 -> 5: {message}:\n  "
            + "\n  ".join(listed)
            + "\nNothing was written."
        )


def plan(ledger: Ledger) -> list[str]:
    """Refuse an unmigratable ledger, else clear positions and rebuild views."""
    # The refusals come first and read only: the runner writes nothing when the
    # plan raises, so an operator sees every offending row before any change.
    _refuse(
        box_name_problems(ledger),
        "these box names cannot become view names (spec 5's name rule: "
        "v_box_<name> must not shadow a core view, must be at most 58 "
        "characters, and must differ from every other box name by more than "
        "case); rename them first (there is no tm box rename yet)",
    )
    _refuse(
        grid_held_boxes(ledger),
        "these child boxes are nested in grid boxes, which hold tubes only "
        "(spec 5); move them out first (tm box move CHILD --in PARENT)",
    )
    _refuse(
        out_of_set_aliquots(ledger),
        "these aliquots have a status that is not in their stock kind's status "
        "set (spec 4 requires one), so the step cannot tell in stock from not "
        "in stock; fix the status first",
    )
    _refuse(
        unplaced_in_stock_aliquots(ledger),
        "these in-stock aliquots have no position (spec 5: an aliquot is in "
        "stock exactly when it is placed, so the generated position rule would "
        "fail immediately); place them first",
    )

    statements: list[str] = []
    # One move event per cleared row, old position in from_*, NULL to_*, and
    # the current status on both sides (the status itself did not change).
    for row in positioned_not_in_stock_aliquots(ledger):
        statements.extend(_clear_position_statements(row, ledger.actor))

    # The spec-4 generated views are not the spec-5 ones: drop every managed
    # view (box kind views and the old per-box views included) and rebuild the
    # spec-5 set from the live rows.  Only managed views are touched; a
    # hand-written view is left alone.
    statements.extend(
        f"DROP VIEW IF EXISTS `{name}`" for name in _live_views(ledger)
    )
    for statement in view_statements(ledger.schema):
        statements.append(statement.sql)
    for statement in box_view_statements(ledger.schema, box_rows(ledger)):
        statements.append(statement.sql)
    return statements


# ---------------------------------------------------------------------------
# The step
# ---------------------------------------------------------------------------


def target_schema(ledger: Ledger) -> EffectiveSchema:
    """The spec-5 schema the ledger opens with.

    The step does not change ``ledger.yaml`` or the preset copies, so the
    schema the ledger opens with is already loaded by the spec-5 tool: the
    module's ``SPEC_VERSION`` (5) is part of its fingerprint.  The runner
    records that fingerprint and regenerates the derived ``dolt_tests`` — the
    position, grid and box-name rules — in the same commit.
    """
    return ledger.schema


def reopened_schema(ledger: Ledger) -> EffectiveSchema:
    """The schema the ledger opens with after the step, from its own files."""
    _config, schema = load_ledger_schema(ledger.path)
    return schema


step = MigrationStep(
    kind="spec",
    from_version=4,
    plan=plan,
    schema=target_schema,
    reopened_schema=reopened_schema,
    description=(
        "clear the positions of not-in-stock aliquots (one move event each), "
        "drop the per-box-kind views, create one spec-5 view per existing box, "
        "recreate the generated views, and regenerate the derived dolt_tests"
    ),
)


if __name__ == "__main__":
    main(step)
