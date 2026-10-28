"""spec 1 -> 2: recreate the generated views with the alias columns.

Spec 2 (commit ``861faae``, release 0.1.2) made the ``alias`` table visible to
view readers: every generated view gained a final ``alias_list`` column and
``v_box_slots`` gained ``occupant_alias_list`` (spec §3).  The change is
additive in SQL terms — ``DROP VIEW`` + ``CREATE VIEW`` per view, no table or
row touched — but it changes the stored shape, hence ``spec_version`` 1 -> 2.

Run it by hand:

    uv run migration/spec/1-to-2/migrate.py --ledger PATH [--branch B] [--dry-run]

The views are generated from the ledger's own loadable schema
(``view_statements(ledger.schema)``), so this step works for any kind set.  The
one-commit contract, the ``pre-migrate-spec-1`` tag and the ``ledger_meta`` bump
(counter, tetramark_version, fingerprint) come from ``src/tetramark/migrate.py``.
"""

from __future__ import annotations

from tetramark.migrate import MigrationStep, main
from tetramark.schema.ddl import view_statements


def plan(ledger):
    """Every generated view: drop the old definition, create the spec-2 one.

    Views do not reference each other, but all drops run before all creates so
    a future kind view that did depend on another would still be rebuilt in a
    valid order.
    """
    statements = view_statements(ledger.schema)
    return [f"DROP VIEW IF EXISTS `{statement.name}`" for statement in statements] + [
        statement.sql for statement in statements
    ]


step = MigrationStep(
    kind="spec",
    from_version=1,
    plan=plan,
    description=(
        "recreate the generated views in the spec-2 shape "
        "(alias_list/occupant_alias_list)"
    ),
)


if __name__ == "__main__":
    main(step)
