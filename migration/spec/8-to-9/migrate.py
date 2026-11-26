"""spec 8 -> 9: ``aliquot_event.revert_use`` and its ``reverts_event_id``.

Spec 9 (``Tetramark-3w6``) makes an aliquot ``use`` **reversible**: a lab
technician who marks a tube used by mistake can undo it, and the undo has to be
an auditable event of its own rather than a deletion of the original row.  The
generated shape changes in three places, all on ``aliquot_event``:

* the ``event`` ENUM gains ``revert_use``.  The value is **appended** last, so
  every stored value keeps its ENUM index and the column's data needs no
  rewrite: the statement is ``MODIFY COLUMN`` with the same list plus one value;
* a nullable ``reverts_event_id BIGINT`` column names the exact ``use`` event a
  ``revert_use`` undoes (``ALTER TABLE ... ADD COLUMN ... AFTER `event```, the
  position a fresh spec-9 ledger has);
* three constraints that make the reference an invariant rather than a
  convention:

  ===========================================  ==============================
  ``uq_aliquot_event_reverts_event_id``       UNIQUE: a use is reversed once
  ``ck_aliquot_event_reverts_event_id``       CHECK: reference ⇔ ``revert_use``
  ``fk_aliquot_event_reverts_event_id``       self FK, ``ON DELETE CASCADE``
  ===========================================  ==============================

The ``UNIQUE`` index admits any number of ``NULL`` values (every ordinary
event), so it constrains only reversals — and it is what makes two concurrent
reversals of one use resolve to one row: the loser gets a duplicate-key error.
The ``CHECK`` keeps the nullable column honest in both directions: an ordinary
event carries no reference, and a ``revert_use`` carries one.  The self foreign
key keeps the reference pointing at a row that exists, and its ``ON DELETE
CASCADE`` means ``aliquot purge`` (one ``DELETE`` over all of an aliquot's
events) never has to delete the child before the parent.

The *same-aliquot* and *target-is-a-`use`* rules are not expressible in DDL
(a composite ``(reverts_event_id, aliquot_id)`` foreign key does exist, but it
breaks ``aliquot renumber``: the single ``UPDATE`` that moves an aliquot's
events to a new id fails its own parent check, verified on Dolt 2.6.1).  They
are enforced in two places instead: :func:`tetramark.core.events.record_event`
validates the target inside the writing transaction, and the generated
``core_revert_event_allowed``/``core_revert_event_unique`` tests make
``tm check`` report a ledger patched by hand.

Nothing else moves: no other table, no ``ledger_meta`` key, no preset.  Existing
rows and event ids are preserved exactly, and the step is idempotent per
constraint (it reads the live shape through ``information_schema`` and only
adds what is missing), so a half-patched ledger can be finished by re-running
it — as every step in this directory, it refuses a counter other than 8.

The step declares its target schema (``schema=``): the shared runner records
that schema's fingerprint and regenerates the derived ``dolt_tests`` —
including the two reversal rules — in the same commit.

Run it by hand::

    uv run migration/spec/8-to-9/migrate.py --ledger PATH [--branch B] [--dry-run]
"""

from __future__ import annotations

from tetramark.core.ledger import Ledger
from tetramark.driver.base import literal
from tetramark.errors import LedgerError
from tetramark.migrate import MigrationStep, main
from tetramark.schema.ddl import (
    REVERT_CHECK_CONSTRAINT,
    REVERT_EVENT,
    REVERT_FK_CONSTRAINT,
    REVERT_UNIQUE_CONSTRAINT,
    _EVENT_VALUES,
    _core_foreign_keys,
)
from tetramark.schema.loader import load_ledger_schema
from tetramark.schema.model import EffectiveSchema

#: The ``aliquot_event`` column and column order this step produces.
SPEC9_EVENT_COLUMNS: tuple[str, ...] = (
    "id",
    "aliquot_id",
    "event",
    "reverts_event_id",
    "amount",
    "unit",
    "from_box_id",
    "from_slot",
    "to_box_id",
    "to_slot",
    "old_status",
    "new_status",
    "forced",
    "actor",
    "message",
    "at",
)


def _refuse(reason: str) -> LedgerError:
    return LedgerError(
        f"refusing to migrate spec 8 -> 9: {reason}. Nothing was written."
    )


def _column_exists(ledger: Ledger, table: str, column: str) -> bool:
    rows = ledger.driver.query(
        "SELECT `column_name` AS `name` FROM `information_schema`.`columns` "
        "WHERE `table_schema` = DATABASE() AND `table_name` = %s "
        "AND `column_name` = %s",
        (table, column),
    )
    return bool(rows)


def _column_type(ledger: Ledger, table: str, column: str) -> str | None:
    rows = ledger.driver.query(
        "SELECT `column_type` AS `type` FROM `information_schema`.`columns` "
        "WHERE `table_schema` = DATABASE() AND `table_name` = %s "
        "AND `column_name` = %s",
        (table, column),
    )
    return str(rows[0]["type"]) if rows else None


def _index_exists(ledger: Ledger, table: str, name: str) -> bool:
    rows = ledger.driver.query(
        "SELECT `index_name` AS `name` FROM `information_schema`.`statistics` "
        "WHERE `table_schema` = DATABASE() AND `table_name` = %s "
        "AND `index_name` = %s",
        (table, name),
    )
    return bool(rows)


def _check_exists(ledger: Ledger, table: str, name: str) -> bool:
    rows = ledger.driver.query(
        "SELECT tc.`constraint_name` AS `name` "
        "FROM `information_schema`.`table_constraints` tc "
        "WHERE tc.`table_schema` = DATABASE() AND tc.`table_name` = %s "
        "AND tc.`constraint_type` = 'CHECK' AND tc.`constraint_name` = %s",
        (table, name),
    )
    return bool(rows)


def _foreign_key_exists(ledger: Ledger, table: str, name: str) -> bool:
    rows = ledger.driver.query(
        "SELECT rc.`constraint_name` AS `name` "
        "FROM `information_schema`.`referential_constraints` rc "
        "WHERE rc.`constraint_schema` = DATABASE() AND rc.`table_name` = %s "
        "AND rc.`constraint_name` = %s",
        (table, name),
    )
    return bool(rows)


def _revert_foreign_key_sql() -> str:
    """The self foreign key exactly as the generated DDL writes it."""
    for statement in _core_foreign_keys():
        if statement.name == REVERT_FK_CONSTRAINT:
            return statement.sql
    raise AssertionError(  # pragma: no cover - the generator always emits it
        "the core DDL has no aliquot_event self foreign key"
    )


def plan(ledger: Ledger) -> list[str]:
    """Add the missing pieces of the spec-9 shape, in dependency order.

    Each statement is guarded by the live shape, so a ledger that was
    hand-patched half-way is finished rather than double-patched.  The order
    matters: the ENUM must know ``revert_use`` before the ``CHECK`` compares
    against it, and the column must exist before anything references it.
    """
    column_type = _column_type(ledger, "aliquot_event", "event")
    if column_type is None:
        raise _refuse("aliquot_event has no `event` column, so it is not the "
                      "spec-8 event table")

    statements: list[str] = []
    if REVERT_EVENT not in column_type:
        values = ", ".join(literal(value) for value in _EVENT_VALUES)
        statements.append(
            f"ALTER TABLE `aliquot_event` MODIFY COLUMN `event` "
            f"ENUM({values}) NOT NULL"
        )
    if not _column_exists(ledger, "aliquot_event", "reverts_event_id"):
        statements.append(
            "ALTER TABLE `aliquot_event` ADD COLUMN `reverts_event_id` BIGINT "
            "AFTER `event`"
        )
    if not _index_exists(ledger, "aliquot_event", REVERT_UNIQUE_CONSTRAINT):
        statements.append(
            f"ALTER TABLE `aliquot_event` ADD CONSTRAINT "
            f"`{REVERT_UNIQUE_CONSTRAINT}` UNIQUE (`reverts_event_id`)"
        )
    if not _check_exists(ledger, "aliquot_event", REVERT_CHECK_CONSTRAINT):
        statements.append(
            f"ALTER TABLE `aliquot_event` ADD CONSTRAINT "
            f"`{REVERT_CHECK_CONSTRAINT}` CHECK ((`event` = '{REVERT_EVENT}') "
            f"= (`reverts_event_id` IS NOT NULL))"
        )
    if not _foreign_key_exists(ledger, "aliquot_event", REVERT_FK_CONSTRAINT):
        statements.append(_revert_foreign_key_sql())
    return statements


def target_schema(ledger: Ledger) -> EffectiveSchema:
    """The spec-9 schema the ledger opens with.

    This step changes no preset, so the ledger's own schema is the target; the
    module's ``SPEC_VERSION`` (9) is part of its fingerprint, and the two
    reversal rules only exist in the spec-9 generator, so the runner installs
    them in the same commit as the column they read.
    """
    _config, schema = load_ledger_schema(ledger.path)
    return schema


def reopened_schema(ledger: Ledger) -> EffectiveSchema:
    """The schema the ledger opens with after the step, from its own files."""
    _config, schema = load_ledger_schema(ledger.path)
    return schema


step = MigrationStep(
    kind="spec",
    from_version=8,
    plan=plan,
    schema=target_schema,
    reopened_schema=reopened_schema,
    description=(
        "add aliquot_event.reverts_event_id (nullable, AFTER event), extend "
        "the event ENUM with revert_use, add the reversal UNIQUE/CHECK/self "
        "foreign key (ON DELETE CASCADE), and regenerate the derived "
        "dolt_tests including the two reversal rules"
    ),
)


if __name__ == "__main__":
    main(step)
