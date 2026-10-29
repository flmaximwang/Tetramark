"""Generate and run Dolt's ``dolt_tests`` for a ledger (spec §4).

``dolt_tests`` is Dolt's own versioned test table; ``dolt_test_run()`` is a
table function that executes every row and reports ``PASS``/``FAIL``.  The
same rows feed Workbench's Tests page, so nothing bespoke has to be executed
by the app to decide whether a ledger is valid.

Three groups of tests are generated from the effective schema:

``core``
    The invariants the DDL cannot express (spec §3, §5): slot capacity,
    aliquot-vs-child-box exclusivity, box nesting acyclicity, position
    follows in stock (a not-in-stock aliquot has no position; an in-stock one
    is placed), grid boxes hold tubes only, box names that can name their
    view, and slotless-box capacity.
``schema:<kind>``
    Field patterns, required refs and the ``entity_kinds`` restriction.
``rules:<preset|local>[:info]``
    The custom rules of spec §2.3.  ``severity: info`` rules carry the
    ``:info`` group suffix and never change the exit code.

Every generated test also carries a *detail* query, used by ``tm check -v``
to print the offending rows.  Detail queries are not stored in Dolt: they are
derived from the same schema when the check runs, so ``dolt_tests`` stays
exactly the six-column assertion table Dolt defines.

The generated rows are derived state: :func:`install_tests` replaces the whole
table (``DELETE`` + ``INSERT``).  ``tm init`` calls it, and ``tm schema
apply`` (ticket 09) calls it after changing the physical schema.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from tetramark.driver.base import Driver, literal
from tetramark.errors import LedgerError
from tetramark.schema.ddl import (
    _in_stock_sql,
    box_view_statements,
    is_managed_view,
    table_name,
    view_body,
)
from tetramark.schema.model import (
    BOX_NAME_MAX_LENGTH,
    BOX_VIEW_PREFIX,
    CORE_VIEW_NAMES,
    EffectiveSchema,
    Field,
    FieldType,
    GeometryType,
    ObjectType,
    Rule,
    RuleExpect,
    RuleSeverity,
)

__all__ = [
    "CORE_GROUP",
    "INFO_SUFFIX",
    "MAX_NESTING_DEPTH",
    "RULES_GROUP",
    "SCHEMA_GROUP",
    "TEST_COLUMNS",
    "CheckReport",
    "TestDefinition",
    "TestResult",
    "box_view_drift",
    "generate_tests",
    "install_tests",
    "run_check",
]

CORE_GROUP = "core"
SCHEMA_GROUP = "schema"
RULES_GROUP = "rules"
INFO_SUFFIX = "info"

#: How many parent links the acyclicity test follows.  Deeper nests are not a
#: realistic freezer layout, and a bound keeps the recursive CTE finite even on
#: a corrupt (cyclic) database.
MAX_NESTING_DEPTH = 32

#: Columns of ``dolt_tests`` on Dolt 2.3.5, in insert order.
TEST_COLUMNS: tuple[str, ...] = (
    "test_name",
    "test_group",
    "test_query",
    "assertion_type",
    "assertion_comparator",
    "assertion_value",
)

#: ``dolt_tests.assertion_type`` values (CHECK constraint in Dolt 2.3.5).
SINGLE_VALUE = "expected_single_value"
EXPECTED_ROWS = "expected_rows"


# ---------------------------------------------------------------------------
# Test definitions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TestDefinition:
    """One row of ``dolt_tests`` plus the detail query used by ``check -v``."""

    name: str
    group: str
    query: str
    assertion_type: str = SINGLE_VALUE
    comparator: str = "=="
    value: int = 0
    detail: str | None = None
    description: str = ""

    def params(self) -> tuple[str, ...]:
        """Values for :data:`TEST_COLUMNS`, all rendered as text."""
        return (
            self.name,
            self.group,
            self.query,
            self.assertion_type,
            self.comparator,
            str(self.value),
        )


# ---------------------------------------------------------------------------
# Small SQL helpers
# ---------------------------------------------------------------------------


def _q(name: str) -> str:
    """Backtick-quote an identifier."""
    return "`" + name.replace("`", "``") + "`"


def _lit(value: str) -> str:
    return literal(value)


def _count(query: str) -> str:
    """Wrap a row-producing query in ``SELECT COUNT(*)``."""
    return f"SELECT COUNT(*) FROM (\n{query}\n) AS {_q('bad')}"


def _slotted_box_kinds(schema: EffectiveSchema) -> dict[str, int]:
    return {
        name: schema.kinds[name].slot_capacity
        for name in sorted(schema.box_kinds)
        if schema.kinds[name].geometry is not None
        and schema.kinds[name].geometry.is_slotted
    }


# ---------------------------------------------------------------------------
# Core invariants (spec §3)
# ---------------------------------------------------------------------------


def _slot_capacity_test(schema: EffectiveSchema) -> TestDefinition | None:
    capacities = _slotted_box_kinds(schema)
    if not capacities:
        return None
    branches = " ".join(
        f"WHEN {_lit(name)} THEN {capacity}"
        for name, capacity in capacities.items()
    )
    limit = f"CASE b.{_q('kind')} {branches} ELSE 1 END"
    source = (
        f"FROM {_q('slot')} s\n"
        f"JOIN {_q('box')} b ON b.{_q('id')} = s.{_q('box_id')}\n"
        f"LEFT JOIN {_q('aliquot')} a ON a.{_q('box_id')} = s.{_q('box_id')} "
        f"AND a.{_q('slot')} = s.{_q('label')}"
    )
    grouping = (
        f"GROUP BY s.{_q('box_id')}, s.{_q('label')}, b.{_q('kind')}, "
        f"b.{_q('name')}, s.{_q('ordinal')}\n"
        f"HAVING COUNT(DISTINCT a.{_q('id')}) > {limit}"
    )
    query = _count(
        "SELECT\n"
        f"  s.{_q('box_id')}, s.{_q('label')}, b.{_q('kind')}\n"
        f"{source}\n{grouping}"
    )
    detail = (
        "SELECT\n"
        f"  b.{_q('name')} AS {_q('box')}, s.{_q('label')} AS {_q('slot')},\n"
        f"  b.{_q('kind')} AS {_q('box_kind')},\n"
        f"  COUNT(DISTINCT a.{_q('id')}) AS {_q('aliquots')}\n"
        f"{source}\n{grouping}\n"
        f"ORDER BY b.{_q('name')}, s.{_q('ordinal')}"
    )
    return TestDefinition(
        name="core_slot_capacity",
        group=CORE_GROUP,
        query=query,
        detail=detail,
        description="no slot holds more aliquots than its box kind's slot_capacity",
    )


def _slot_exclusive_test() -> TestDefinition:
    body = (
        f"SELECT s.{_q('box_id')}, s.{_q('label')}\n"
        f"FROM {_q('slot')} s\n"
        f"LEFT JOIN {_q('aliquot')} a ON a.{_q('box_id')} = s.{_q('box_id')} "
        f"AND a.{_q('slot')} = s.{_q('label')}\n"
        f"LEFT JOIN {_q('box')} c ON c.{_q('parent_box_id')} = s.{_q('box_id')} "
        f"AND c.{_q('parent_slot')} = s.{_q('label')}\n"
        f"GROUP BY s.{_q('box_id')}, s.{_q('label')}\n"
        f"HAVING COUNT(DISTINCT a.{_q('id')}) > 0 "
        f"AND COUNT(DISTINCT c.{_q('id')}) > 0"
    )
    detail = (
        f"SELECT b.{_q('name')} AS {_q('box')}, s.{_q('label')} AS {_q('slot')},\n"
        f"  GROUP_CONCAT(DISTINCT a.{_q('id')}) AS {_q('aliquots')},\n"
        f"  GROUP_CONCAT(DISTINCT c.{_q('name')}) AS {_q('child_boxes')}\n"
        f"FROM {_q('slot')} s\n"
        f"JOIN {_q('box')} b ON b.{_q('id')} = s.{_q('box_id')}\n"
        f"LEFT JOIN {_q('aliquot')} a ON a.{_q('box_id')} = s.{_q('box_id')} "
        f"AND a.{_q('slot')} = s.{_q('label')}\n"
        f"LEFT JOIN {_q('box')} c ON c.{_q('parent_box_id')} = s.{_q('box_id')} "
        f"AND c.{_q('parent_slot')} = s.{_q('label')}\n"
        f"GROUP BY s.{_q('box_id')}, s.{_q('label')}, b.{_q('name')}, "
        f"s.{_q('ordinal')}\n"
        f"HAVING COUNT(DISTINCT a.{_q('id')}) > 0 "
        f"AND COUNT(DISTINCT c.{_q('id')}) > 0\n"
        f"ORDER BY b.{_q('name')}, s.{_q('ordinal')}"
    )
    return TestDefinition(
        name="core_slot_exclusive",
        group=CORE_GROUP,
        query=_count(body),
        detail=detail,
        description="a slot holds aliquots or one child box, never both",
    )


def _single_child_test() -> TestDefinition:
    body = (
        f"SELECT c.{_q('parent_box_id')}, c.{_q('parent_slot')}\n"
        f"FROM {_q('box')} c\n"
        f"WHERE c.{_q('parent_box_id')} IS NOT NULL "
        f"AND c.{_q('parent_slot')} IS NOT NULL\n"
        f"GROUP BY c.{_q('parent_box_id')}, c.{_q('parent_slot')}\n"
        f"HAVING COUNT(*) > 1"
    )
    detail = (
        f"SELECT p.{_q('name')} AS {_q('box')}, c.{_q('parent_slot')} AS {_q('slot')},\n"
        f"  GROUP_CONCAT(c.{_q('name')}) AS {_q('child_boxes')}\n"
        f"FROM {_q('box')} c\n"
        f"JOIN {_q('box')} p ON p.{_q('id')} = c.{_q('parent_box_id')}\n"
        f"WHERE c.{_q('parent_box_id')} IS NOT NULL "
        f"AND c.{_q('parent_slot')} IS NOT NULL\n"
        f"GROUP BY c.{_q('parent_box_id')}, c.{_q('parent_slot')}, p.{_q('name')}\n"
        f"HAVING COUNT(*) > 1\n"
        f"ORDER BY p.{_q('name')}, c.{_q('parent_slot')}"
    )
    return TestDefinition(
        name="core_slot_single_child_box",
        group=CORE_GROUP,
        query=_count(body),
        detail=detail,
        description="a slot holds at most one child box",
    )


def _acyclic_test() -> TestDefinition:
    walk = (
        f"WITH RECURSIVE {_q('walk')} "
        f"({_q('root_id')}, {_q('current_id')}, {_q('depth')}) AS (\n"
        f"  SELECT {_q('id')}, {_q('id')}, 0 FROM {_q('box')}\n"
        "  UNION ALL\n"
        f"  SELECT w.{_q('root_id')}, b.{_q('parent_box_id')}, w.{_q('depth')} + 1\n"
        f"  FROM {_q('walk')} w\n"
        f"  JOIN {_q('box')} b ON b.{_q('id')} = w.{_q('current_id')}\n"
        f"  WHERE w.{_q('depth')} < {MAX_NESTING_DEPTH} "
        f"AND b.{_q('parent_box_id')} IS NOT NULL\n"
        ")\n"
    )
    query = (
        walk
        + f"SELECT COUNT(DISTINCT {_q('root_id')}) FROM {_q('walk')}\n"
        f"WHERE {_q('current_id')} = {_q('root_id')} AND {_q('depth')} > 0"
    )
    detail = (
        walk
        + f"SELECT w.{_q('root_id')} AS {_q('box_id')}, "
        f"b.{_q('name')} AS {_q('box')}, "
        f"MIN(w.{_q('depth')}) AS {_q('cycle_length')}\n"
        f"FROM {_q('walk')} w\n"
        f"JOIN {_q('box')} b ON b.{_q('id')} = w.{_q('root_id')}\n"
        f"WHERE w.{_q('current_id')} = w.{_q('root_id')} AND w.{_q('depth')} > 0\n"
        f"GROUP BY w.{_q('root_id')}, b.{_q('name')}\n"
        f"ORDER BY b.{_q('name')}"
    )
    return TestDefinition(
        name="core_box_nesting_acyclic",
        group=CORE_GROUP,
        query=query,
        detail=detail,
        description=f"box nesting has no cycle up to depth {MAX_NESTING_DEPTH}",
    )


def _in_stock_position_test(schema: EffectiveSchema) -> TestDefinition | None:
    """An **in-stock** aliquot is physically placed (spec 5).

    ``box_id`` is required, and ``slot`` is required when the box is slotted:
    a stored tube that is in a box has to name its slot, while a slotless box
    (freezer/shelf/drawer) has none.
    """
    if not schema.stock_kinds:  # pragma: no cover - a ledger always has stock
        return None
    in_stock = _in_stock_sql(
        schema, kind=f"st.{_q('kind')}", status=f"a.{_q('status')}"
    )
    if in_stock == "FALSE":  # pragma: no cover - status sets define in-stock
        return None
    slotted = _slotted_box_kinds(schema)
    missing: list[str] = [f"a.{_q('box_id')} IS NULL"]
    if slotted:
        missing.append(
            f"(b.{_q('kind')} IN ("
            + ", ".join(_lit(name) for name in slotted)
            + f") AND a.{_q('slot')} IS NULL)"
        )
    condition = (
        f"({in_stock})\n"
        "  AND (\n    " + "\n    OR ".join(missing) + "\n  )"
    )
    source = (
        f"FROM {_q('aliquot')} a\n"
        f"JOIN {_q('stock')} st ON st.{_q('id')} = a.{_q('stock_id')}\n"
        f"LEFT JOIN {_q('box')} b ON b.{_q('id')} = a.{_q('box_id')}"
    )
    body = f"SELECT a.{_q('id')}\n{source}\nWHERE {condition}"
    detail = (
        f"SELECT a.{_q('id')} AS {_q('aliquot_id')}, "
        f"a.{_q('status')} AS {_q('status')}, "
        f"b.{_q('name')} AS {_q('box')}, "
        f"b.{_q('kind')} AS {_q('box_kind')}, "
        f"a.{_q('slot')} AS {_q('slot')}\n"
        f"{source}\nWHERE {condition}\nORDER BY a.{_q('id')}"
    )
    return TestDefinition(
        name="core_aliquot_in_stock_position",
        group=CORE_GROUP,
        query=_count(body),
        detail=detail,
        description=(
            "an in-stock aliquot has a box and, in a slotted box, a slot"
        ),
    )


def _not_in_stock_no_position_test(
    schema: EffectiveSchema,
) -> TestDefinition | None:
    """An aliquot that is **not** in stock has no box position (spec 5).

    A tube that left stock is no longer physically in its box: ``box_id`` and
    ``slot`` must be NULL, and ``aliquot_event.from_box_id``/``from_slot`` keep
    where it was.
    """
    if not schema.stock_kinds:  # pragma: no cover - a ledger always has stock
        return None
    in_stock = _in_stock_sql(
        schema, kind=f"st.{_q('kind')}", status=f"a.{_q('status')}"
    )
    if in_stock == "FALSE":  # pragma: no cover - status sets define in-stock
        return None
    condition = (
        f"NOT ({in_stock})\n"
        f"  AND (a.{_q('box_id')} IS NOT NULL "
        f"OR a.{_q('slot')} IS NOT NULL)"
    )
    source = (
        f"FROM {_q('aliquot')} a\n"
        f"JOIN {_q('stock')} st ON st.{_q('id')} = a.{_q('stock_id')}"
    )
    body = f"SELECT a.{_q('id')}\n{source}\nWHERE {condition}"
    detail = (
        f"SELECT a.{_q('id')} AS {_q('aliquot_id')}, "
        f"a.{_q('status')} AS {_q('status')}, "
        f"a.{_q('box_id')} AS {_q('box_id')}, "
        f"a.{_q('slot')} AS {_q('slot')}\n"
        f"{source}\nWHERE {condition}\nORDER BY a.{_q('id')}"
    )
    return TestDefinition(
        name="core_aliquot_not_in_stock_no_position",
        group=CORE_GROUP,
        query=_count(body),
        detail=detail,
        description="an aliquot that is not in stock has no box position",
    )


def _grid_box_no_child_test(schema: EffectiveSchema) -> TestDefinition | None:
    """Grid boxes hold tubes only, never a child box (spec 5)."""
    grids = [
        name
        for name in sorted(schema.box_kinds)
        if schema.kinds[name].geometry is not None
        and schema.kinds[name].geometry.type is GeometryType.grid
    ]
    if not grids:  # pragma: no cover - bundled presets always have a grid
        return None
    condition = (
        f"p.{_q('kind')} IN (" + ", ".join(_lit(name) for name in grids) + ")"
    )
    source = (
        f"FROM {_q('box')} c\n"
        f"JOIN {_q('box')} p ON p.{_q('id')} = c.{_q('parent_box_id')}"
    )
    body = f"SELECT c.{_q('id')}\n{source}\nWHERE {condition}"
    detail = (
        f"SELECT c.{_q('name')} AS {_q('child_box')}, "
        f"p.{_q('name')} AS {_q('parent_box')}, "
        f"p.{_q('kind')} AS {_q('parent_kind')}\n"
        f"{source}\nWHERE {condition}\n"
        f"ORDER BY p.{_q('name')}, c.{_q('name')}"
    )
    return TestDefinition(
        name="core_grid_box_no_child_box",
        group=CORE_GROUP,
        query=_count(body),
        detail=detail,
        description="a grid box holds tubes, never a child box",
    )


def _box_name_test() -> TestDefinition:
    """Every box name can name its generated view (spec 5's name rule)."""
    reserved = ", ".join(
        _lit(core.lower())
        for core in CORE_VIEW_NAMES
        if core.startswith(BOX_VIEW_PREFIX)
    )
    view_name = f"CONCAT({_lit(BOX_VIEW_PREFIX)}, b.{_q('name')})"
    collides = f"LOWER({view_name}) IN ({reserved})"
    too_long = f"CHAR_LENGTH(b.{_q('name')}) > {BOX_NAME_MAX_LENGTH}"
    case_duplicate = (
        f"EXISTS (SELECT 1 FROM {_q('box')} o\n"
        f"    WHERE o.{_q('id')} <> b.{_q('id')}\n"
        f"      AND LOWER(o.{_q('name')}) = LOWER(b.{_q('name')})\n"
        f"      AND BINARY o.{_q('name')} <> BINARY b.{_q('name')})"
    )
    condition = f"({collides})\n  OR ({too_long})\n  OR ({case_duplicate})"
    body = (
        f"SELECT b.{_q('id')}\n"
        f"FROM {_q('box')} b\n"
        f"WHERE {condition}"
    )
    detail = (
        f"SELECT b.{_q('name')} AS {_q('box')},\n"
        f"  CASE\n"
        f"    WHEN {collides} THEN 'collides with a core view'\n"
        f"    WHEN {too_long} THEN 'longer than {BOX_NAME_MAX_LENGTH} characters'\n"
        f"    ELSE 'differs only in case from another box'\n"
        f"  END AS {_q('problem')}\n"
        f"FROM {_q('box')} b\n"
        f"WHERE {condition}\n"
        f"ORDER BY b.{_q('name')}"
    )
    return TestDefinition(
        name="core_box_name_safe",
        group=CORE_GROUP,
        query=_count(body),
        detail=detail,
        description=(
            "every box name yields a usable view name (not a core view, at most "
            f"{BOX_NAME_MAX_LENGTH} characters, unique ignoring case)"
        ),
    )


def _slotless_capacity_tests(schema: EffectiveSchema) -> list[TestDefinition]:
    tests: list[TestDefinition] = []
    for kind_name in sorted(schema.box_kinds):
        kind = schema.kinds[kind_name]
        geometry = kind.geometry
        if geometry is None or geometry.capacity is None:
            continue
        if geometry.is_slotted:  # capacity is only valid on slotless geometry
            continue
        condition = f"b.{_q('kind')} = {_lit(kind_name)}"
        counts = (
            f"COUNT(DISTINCT a.{_q('id')}) + COUNT(DISTINCT c.{_q('id')})"
        )
        grouping = (
            f"FROM {_q('box')} b\n"
            f"LEFT JOIN {_q('aliquot')} a ON a.{_q('box_id')} = b.{_q('id')}\n"
            f"LEFT JOIN {_q('box')} c ON c.{_q('parent_box_id')} = b.{_q('id')}\n"
            f"WHERE {condition}\n"
            f"GROUP BY b.{_q('id')}\n"
            f"HAVING {counts} > {geometry.capacity}"
        )
        query = _count(f"SELECT b.{_q('id')}\n" + grouping)
        detail = (
            f"SELECT b.{_q('name')} AS {_q('box')}, "
            f"COUNT(DISTINCT a.{_q('id')}) AS {_q('aliquots')}, "
            f"COUNT(DISTINCT c.{_q('id')}) AS {_q('child_boxes')}\n"
            f"FROM {_q('box')} b\n"
            f"LEFT JOIN {_q('aliquot')} a ON a.{_q('box_id')} = b.{_q('id')}\n"
            f"LEFT JOIN {_q('box')} c ON c.{_q('parent_box_id')} = b.{_q('id')}\n"
            f"WHERE {condition}\n"
            f"GROUP BY b.{_q('id')}, b.{_q('name')}\n"
            f"HAVING {counts} > {geometry.capacity}\n"
            f"ORDER BY b.{_q('name')}"
        )
        tests.append(
            TestDefinition(
                name=f"core_slotless_capacity_{kind_name}",
                group=CORE_GROUP,
                query=query,
                detail=detail,
                description=(
                    f"slotless box kind {kind_name!r} holds at most "
                    f"{geometry.capacity} aliquots and child boxes"
                ),
            )
        )
    return tests


def core_tests(schema: EffectiveSchema) -> list[TestDefinition]:
    """The app-enforced invariants of spec §3/§5, as ``core`` tests."""
    tests: list[TestDefinition] = []
    capacity = _slot_capacity_test(schema)
    if capacity is not None:
        tests.append(capacity)
    tests.append(_slot_exclusive_test())
    tests.append(_single_child_test())
    tests.append(_acyclic_test())
    grid_children = _grid_box_no_child_test(schema)
    if grid_children is not None:
        tests.append(grid_children)
    in_stock = _in_stock_position_test(schema)
    if in_stock is not None:
        tests.append(in_stock)
    not_in_stock = _not_in_stock_no_position_test(schema)
    if not_in_stock is not None:
        tests.append(not_in_stock)
    tests.append(_box_name_test())
    tests.extend(_slotless_capacity_tests(schema))
    status = _status_known_test(schema)
    if status is not None:
        tests.append(status)
    transitions = _status_transition_test(schema)
    if transitions is not None:
        tests.append(transitions)
    return tests


# ---------------------------------------------------------------------------
# Status sets (spec §4)
# ---------------------------------------------------------------------------


def _status_known_test(schema: EffectiveSchema) -> TestDefinition | None:
    """Every aliquot's status belongs to its stock kind's status set."""
    body = (
        f"SELECT a.{_q('id')}\n"
        f"FROM {_q('aliquot')} a\n"
        f"JOIN {_q('stock')} st ON st.{_q('id')} = a.{_q('stock_id')}\n"
        "WHERE NOT (\n"
        f"  {_in_stock_case(schema)}\n"
        ")"
    )
    detail = (
        f"SELECT a.{_q('id')} AS {_q('aliquot_id')}, "
        f"a.{_q('stock_id')} AS {_q('stock_id')}, "
        f"a.{_q('status')} AS {_q('status')}, "
        f"st.{_q('kind')} AS {_q('stock_kind')}\n"
        f"FROM {_q('aliquot')} a\n"
        f"JOIN {_q('stock')} st ON st.{_q('id')} = a.{_q('stock_id')}\n"
        f"WHERE NOT (\n  {_in_stock_case(schema)}\n)\n"
        f"ORDER BY a.{_q('id')}"
    )
    return TestDefinition(
        name="core_aliquot_status_known",
        group=CORE_GROUP,
        query=_count(body),
        detail=detail,
        description="every aliquot's status belongs to its stock kind's status set",
    )


def _in_stock_case(schema: EffectiveSchema) -> str:
    """``CASE`` over ``st.kind`` testing ``a.status`` against the kind's set.

    Unlike :func:`_in_stock_sql` (which only knows the *in stock* statuses),
    this tests membership in the whole set.  Generated from the schema, so the
    rule follows every status set change without hand-written SQL.
    """
    branches: list[str] = []
    for kind_name in sorted(schema.stock_kinds):
        names = schema.statuses_for(kind_name)
        values = ", ".join(_lit(name) for name in names)
        branches.append(
            f"    WHEN {_lit(kind_name)} THEN a.{_q('status')} IN ({values})"
        )
    if not branches:  # pragma: no cover - caller skips a kind-less schema
        return "FALSE"
    return (
        f"CASE st.{_q('kind')}\n" + "\n".join(branches) + "\n    ELSE FALSE\n  END"
    )


def _transition_pairs(schema: EffectiveSchema) -> dict[str, list[str]]:
    """``kind`` → ``old>new`` strings the kind's status set allows."""
    pairs: dict[str, list[str]] = {}
    for kind_name in sorted(schema.stock_kinds):
        status_set = schema.status_set(kind_name)
        assert status_set is not None
        allowed: list[str] = []
        for old in status_set.names:
            for new in status_set.root[old].to:
                allowed.append(f"{old}>{new}")
        pairs[kind_name] = allowed
    return pairs


def _status_transition_test(schema: EffectiveSchema) -> TestDefinition | None:
    """Every unforced ``status`` event follows its stock kind's ``to:`` rules."""
    pairs = _transition_pairs(schema)
    if not pairs:  # pragma: no cover - caller skips a kind-less schema
        return None
    branches: list[str] = []
    for kind_name in sorted(pairs):
        allowed = pairs[kind_name]
        pair_expr = ", ".join(_lit(pair) for pair in allowed)
        branches.append(
            f"    WHEN {_lit(kind_name)} THEN "
            f"CONCAT(e.{_q('old_status')}, '>', e.{_q('new_status')}) "
            f"IN ({pair_expr})"
        )
    case = (
        f"CASE st.{_q('kind')}\n" + "\n".join(branches) + "\n    ELSE FALSE\n  END"
    )
    condition = (
        f"e.{_q('event')} = {_lit('status')} "
        f"AND e.{_q('forced')} = FALSE "
        f"AND NOT ({case})"
    )
    body = (
        f"SELECT e.{_q('id')}\n"
        f"FROM {_q('aliquot_event')} e\n"
        f"JOIN {_q('aliquot')} a ON a.{_q('id')} = e.{_q('aliquot_id')}\n"
        f"JOIN {_q('stock')} st ON st.{_q('id')} = a.{_q('stock_id')}\n"
        f"WHERE {condition}"
    )
    detail = (
        f"SELECT e.{_q('id')} AS {_q('event_id')}, "
        f"e.{_q('aliquot_id')} AS {_q('aliquot_id')}, "
        f"st.{_q('kind')} AS {_q('stock_kind')}, "
        f"e.{_q('old_status')} AS {_q('old_status')}, "
        f"e.{_q('new_status')} AS {_q('new_status')}\n"
        f"FROM {_q('aliquot_event')} e\n"
        f"JOIN {_q('aliquot')} a ON a.{_q('id')} = e.{_q('aliquot_id')}\n"
        f"JOIN {_q('stock')} st ON st.{_q('id')} = a.{_q('stock_id')}\n"
        f"WHERE {condition}\n"
        f"ORDER BY e.{_q('id')}"
    )
    return TestDefinition(
        name="core_status_event_allowed",
        group=CORE_GROUP,
        query=_count(body),
        detail=detail,
        description=(
            "every unforced status event follows its stock kind's allowed "
            "transitions (--force records forced = TRUE)"
        ),
    )


# ---------------------------------------------------------------------------
# Per-kind schema tests (spec §4)
# ---------------------------------------------------------------------------


def _pattern_test(
    *, kind_name: str, table: str, field_name: str, schema_field: Field
) -> TestDefinition:
    assert schema_field.pattern is not None  # callers check
    column = _q(field_name)
    condition = (
        f"{column} IS NOT NULL "
        f"AND NOT ({column} REGEXP {_lit(schema_field.pattern)})"
    )
    description = (
        schema_field.description or f"{field_name} matches {schema_field.pattern}"
    )
    return TestDefinition(
        name=f"schema_{kind_name}_pattern_{field_name}"
        if table.startswith(("entity_", "stock_", "box_"))
        else f"schema_{kind_name}_aliquot_pattern_{field_name}",
        group=f"{SCHEMA_GROUP}:{kind_name}",
        query=f"SELECT COUNT(*) FROM {_q(table)} WHERE {condition}",
        detail=(
            f"SELECT {_q('id')} AS {_q('id')}, {column} AS {_q('value')} "
            f"FROM {_q(table)} WHERE {condition} ORDER BY {_q('id')}"
        ),
        description=description,
    )


def _required_ref_test(
    *, kind_name: str, table: str, field_name: str
) -> TestDefinition:
    column = _q(field_name)
    suffix = (
        "required_ref"
        if table.startswith(("entity_", "stock_", "box_"))
        else "aliquot_required_ref"
    )
    return TestDefinition(
        name=f"schema_{kind_name}_{suffix}_{field_name}",
        group=f"{SCHEMA_GROUP}:{kind_name}",
        query=f"SELECT COUNT(*) FROM {_q(table)} WHERE {column} IS NULL",
        detail=(
            f"SELECT {_q('id')} AS {_q('id')} FROM {_q(table)} "
            f"WHERE {column} IS NULL ORDER BY {_q('id')}"
        ),
        description=f"required ref {field_name} is populated",
    )


def _entity_kinds_test(schema: EffectiveSchema, kind_name: str) -> TestDefinition | None:
    kind = schema.kinds[kind_name]
    if kind.object is not ObjectType.stock or kind.entity_kinds is None:
        return None
    if not kind.entity_kinds:
        # ``entity_kinds: []`` permits no entity kind at all: no stock of this
        # kind may exist.  A separate query keeps the SQL valid (``NOT IN ()``
        # is a syntax error) and the message explicit.
        body = (
            f"SELECT s.{_q('id')}\n"
            f"FROM {_q('stock')} s\n"
            f"WHERE s.{_q('kind')} = {_lit(kind_name)}"
        )
        return TestDefinition(
            name=f"schema_{kind_name}_entity_kinds",
            group=f"{SCHEMA_GROUP}:{kind_name}",
            query=_count(body),
            detail=(
                f"SELECT s.{_q('id')} AS {_q('stock_id')}, "
                f"s.{_q('entity_id')} AS {_q('entity_id')}\n"
                f"FROM {_q('stock')} s\n"
                f"WHERE s.{_q('kind')} = {_lit(kind_name)}\n"
                f"ORDER BY s.{_q('id')}"
            ),
            description=f"no stocks of kind {kind_name!r} (entity_kinds is empty)",
        )
    allowed = ", ".join(_lit(name) for name in kind.entity_kinds)
    body = (
        f"SELECT s.{_q('id')}\n"
        f"FROM {_q('stock')} s\n"
        f"JOIN {_q('entity')} e ON e.{_q('id')} = s.{_q('entity_id')}\n"
        f"WHERE s.{_q('kind')} = {_lit(kind_name)} "
        f"AND e.{_q('kind')} NOT IN ({allowed})"
    )
    detail = (
        f"SELECT s.{_q('id')} AS {_q('stock_id')}, "
        f"s.{_q('entity_id')} AS {_q('entity_id')}, "
        f"e.{_q('kind')} AS {_q('entity_kind')}\n"
        f"FROM {_q('stock')} s\n"
        f"JOIN {_q('entity')} e ON e.{_q('id')} = s.{_q('entity_id')}\n"
        f"WHERE s.{_q('kind')} = {_lit(kind_name)} "
        f"AND e.{_q('kind')} NOT IN ({allowed})\n"
        f"ORDER BY s.{_q('id')}"
    )
    return TestDefinition(
        name=f"schema_{kind_name}_entity_kinds",
        group=f"{SCHEMA_GROUP}:{kind_name}",
        query=_count(body),
        detail=detail,
        description=(
            f"stocks of kind {kind_name!r} reference entities of kinds "
            f"{', '.join(kind.entity_kinds)}"
        ),
    )


def schema_tests(schema: EffectiveSchema) -> list[TestDefinition]:
    """Patterns, required refs and ``entity_kinds`` restrictions."""
    tests: list[TestDefinition] = []
    for kind_name in sorted(schema.kinds):
        kind = schema.kinds[kind_name]
        group = f"{SCHEMA_GROUP}:{kind_name}"
        table = table_name(kind.object, kind_name)

        for field_name in sorted(kind.fields):
            schema_field = kind.fields[field_name]
            if schema_field.pattern is not None:
                tests.append(
                    _pattern_test(
                        kind_name=kind_name,
                        table=table,
                        field_name=field_name,
                        schema_field=schema_field,
                    )
                )
            if schema_field.type is FieldType.ref and schema_field.required:
                tests.append(
                    _required_ref_test(
                        kind_name=kind_name, table=table, field_name=field_name
                    )
                )

        restriction = _entity_kinds_test(schema, kind_name)
        if restriction is not None:
            tests.append(restriction)
    return tests


# ---------------------------------------------------------------------------
# Custom rules (spec §2.3, §4)
# ---------------------------------------------------------------------------


def _rule_test(rule: Rule, source: str) -> TestDefinition:
    expect: RuleExpect = rule.expect
    if expect.kind == "single_value":
        assertion_type, comparator = SINGLE_VALUE, "=="
    elif expect.kind == "rows":
        assertion_type, comparator = EXPECTED_ROWS, "=="
    else:  # rows_at_most
        assertion_type, comparator = EXPECTED_ROWS, "<="
    group = f"{RULES_GROUP}:{source}"
    if rule.severity is RuleSeverity.info:
        group = f"{group}:{INFO_SUFFIX}"
    # Dolt's table function rejects a trailing semicolon in the test query.
    query = rule.sql.strip().rstrip(";").strip()
    return TestDefinition(
        name=f"rule_{rule.id}",
        group=group,
        query=query,
        assertion_type=assertion_type,
        comparator=comparator,
        value=expect.value,
        detail=query,
        description=rule.description,
    )


def rule_tests(schema: EffectiveSchema) -> list[TestDefinition]:
    """Custom rules, grouped by origin and carrying the ``:info`` suffix."""
    return [
        _rule_test(rule, schema.rule_sources.get(rule.id, "preset"))
        for rule in sorted(schema.rules, key=lambda item: item.id)
    ]


# ---------------------------------------------------------------------------
# Everything
# ---------------------------------------------------------------------------


def generate_tests(schema: EffectiveSchema) -> list[TestDefinition]:
    """Every ``dolt_tests`` row for ``schema``, in stable order."""
    definitions = [
        *core_tests(schema),
        *schema_tests(schema),
        *rule_tests(schema),
    ]
    seen: set[str] = set()
    for definition in definitions:
        if definition.name in seen:  # pragma: no cover - defensive
            raise ValueError(f"duplicate generated test name: {definition.name}")
        seen.add(definition.name)
    return definitions


def install_tests(
    driver: Driver,
    schema: EffectiveSchema,
    *,
    tests: Sequence[TestDefinition] | None = None,
) -> list[TestDefinition]:
    """Replace ``dolt_tests`` wholesale with ``schema``'s generated rows.

    Generated tests are derived state (spec §4), so replacement is the only
    supported operation.  Call inside a :meth:`Driver.transaction` together
    with the rest of a write to make the change part of one Dolt commit.

    ``tests`` overrides the generated list (the migration runner omits tests
    that depend on core tables a not-yet-upgraded ledger does not have).
    """
    definitions = list(tests) if tests is not None else generate_tests(schema)
    driver.execute("DELETE FROM `dolt_tests`")
    columns = ", ".join(_q(column) for column in TEST_COLUMNS)
    placeholders = ", ".join("%s" for _ in TEST_COLUMNS)
    statement = f"INSERT INTO `dolt_tests` ({columns}) VALUES ({placeholders})"
    for definition in definitions:
        driver.execute(statement, definition.params())
    return definitions


# ---------------------------------------------------------------------------
# Running the tests
# ---------------------------------------------------------------------------


@dataclass
class TestResult:
    """One row of ``dolt_test_run()``, enriched for ``check -v``."""

    name: str
    group: str
    query: str
    status: str
    message: str
    detail: str | None = None
    detail_rows: list[dict[str, Any]] = field(default_factory=list)
    detail_error: str | None = None

    @property
    def failed(self) -> bool:
        return self.status.upper() == "FAIL"

    @property
    def is_info(self) -> bool:
        """Info tests are reported but never fail ``tm check`` (spec §4)."""
        return self.group.endswith(f":{INFO_SUFFIX}")

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "group": self.group,
            "query": self.query,
            "status": self.status,
            "message": self.message,
        }
        if self.detail_rows or self.detail_error:
            payload["details"] = self.detail_rows
            if self.detail_error:
                payload["detail_error"] = self.detail_error
        return payload


@dataclass
class CheckReport:
    """The outcome of one ``dolt_test_run()``."""

    results: list[TestResult] = field(default_factory=list)

    @property
    def failures(self) -> list[TestResult]:
        return [result for result in self.results if result.failed]

    @property
    def blocking_failures(self) -> list[TestResult]:
        return [result for result in self.failures if not result.is_info]

    @property
    def info_failures(self) -> list[TestResult]:
        return [result for result in self.failures if result.is_info]

    @property
    def passed(self) -> bool:
        """True when nothing but informational tests failed (spec §4)."""
        return not self.blocking_failures

    @property
    def count(self) -> int:
        return len(self.results)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.passed,
            "count": self.count,
            "failed": len(self.blocking_failures),
            "info_failed": len(self.info_failures),
            "tests": [result.as_dict() for result in self.results],
        }


def _group_matches(group: str, wanted: str) -> bool:
    """``--group rules`` selects ``rules:*``; ``--group schema:plasmid`` is exact."""
    return group == wanted or group.startswith(f"{wanted}:")


# ---------------------------------------------------------------------------
# Per-box view drift (spec 5)
# ---------------------------------------------------------------------------


def box_view_drift(
    driver: Driver, schema: EffectiveSchema
) -> list[dict[str, str]]:
    """Per-box views that are missing, orphaned or out of date (spec 5).

    The expected views are generated from the live ``box`` rows, exactly as
    :func:`tetramark.schema.diff.build_plan` does, and compared with
    ``information_schema.views``.  View names are compared case-insensitively:
    Dolt stores them in lowercase, so the desired ``v_box_WFL-10-01`` and the
    live ``v_box_wfl-10-01`` are the same view.  ``tm check`` turns a non-empty
    result into a blocking failure; ``tm schema apply`` recreates the views.

    A ledger whose ``box`` table does not exist yet (never initialised) has no
    per-box views to check; a box name that cannot become a view name is
    reported here instead of raising, so ``tm check`` can list it.
    """
    rows = driver.query(
        "SELECT table_name AS name, view_definition AS definition "
        "FROM information_schema.views WHERE table_schema = DATABASE()"
    )
    live = {str(row["name"]): str(row["definition"] or "") for row in rows}
    try:
        boxes = driver.query("SELECT `id`, `name`, `kind` FROM `box`")
    except LedgerError:  # a database that was never initialised
        return []
    try:
        statements = box_view_statements(schema, boxes)
    except LedgerError as error:
        return [
            {
                "view": "",
                "problem": "cannot be generated",
                "detail": str(error),
            }
        ]
    desired = {statement.name: view_body(statement) for statement in statements}
    live_by_key = {
        name.lower(): (name, definition) for name, definition in live.items()
    }
    desired_by_key = {
        name.lower(): (name, body) for name, body in desired.items()
    }

    problems: list[dict[str, str]] = []
    for key, (name, body) in desired_by_key.items():
        current = live_by_key.get(key)
        if current is None:
            problems.append(
                {
                    "view": name,
                    "problem": "missing",
                    "detail": "the box exists but its view does not",
                }
            )
        elif current[1] != body:
            problems.append(
                {
                    "view": name,
                    "problem": "out of date",
                    "detail": "the live view body differs from the generated one",
                }
            )
    for key, (name, _definition) in live_by_key.items():
        if name in CORE_VIEW_NAMES or not name.startswith(BOX_VIEW_PREFIX):
            continue
        if not is_managed_view(name):  # pragma: no cover - prefix implies it
            continue
        if key not in desired_by_key:
            problems.append(
                {
                    "view": name,
                    "problem": "orphaned",
                    "detail": "no box of that name exists",
                }
            )
    return problems


def run_check(
    driver: Driver,
    schema: EffectiveSchema,
    *,
    group: str | None = None,
    verbose: bool = False,
) -> CheckReport:
    """Run every installed test and optionally collect offending rows.

    ``dolt_test_run()`` has no group filter on Dolt 2.3.5, so tests are all
    executed and the result is filtered here.  With ``verbose`` each failing
    test's derived detail query is executed; a broken detail query is reported
    on the result rather than hiding the failure itself.

    Spec 5 adds one check that is not a ``dolt_tests`` row: the per-box views
    are derived state generated from the live ``box`` rows, so
    :func:`box_view_drift` compares them in Python and reports drift as a
    blocking ``core`` failure (missing, orphaned or out-of-date view).  It is
    appended only when there is drift, so a clean ledger's test count is
    unchanged.
    """
    rows = driver.query(
        "SELECT `test_name`, `test_group_name`, `query`, `status`, `message` "
        "FROM `dolt_test_run`()"
    )
    definitions = {definition.name: definition for definition in generate_tests(schema)}

    report = CheckReport()
    for row in rows:
        name = str(row.get("test_name") or "")
        result = TestResult(
            name=name,
            group=str(row.get("test_group_name") or ""),
            query=str(row.get("query") or ""),
            status=str(row.get("status") or ""),
            message=str(row.get("message") or ""),
        )
        if group is not None and not _group_matches(result.group, group):
            continue
        if verbose and result.failed:
            definition = definitions.get(name)
            if definition is not None and definition.detail:
                result.detail = definition.detail
                try:
                    result.detail_rows = driver.query(definition.detail)
                except Exception as error:  # detail must not mask the failure
                    result.detail_error = str(error)
        report.results.append(result)

    if group is None or _group_matches(CORE_GROUP, group):
        problems = box_view_drift(driver, schema)
        if problems:
            report.results.append(
                TestResult(
                    name="core_box_views",
                    group=CORE_GROUP,
                    query="",
                    status="FAIL",
                    message=f"{len(problems)} per-box view problem(s)",
                    detail_rows=problems,
                )
            )
    return report
