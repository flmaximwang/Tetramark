"""base 2 -> 3: the slotless box kind ``incubator``.

Base preset version 3 (ticket 27, design §3) adds the box kind ``incubator``:
fly, worm and cell-culture material all sit in one, so a single slotless kind in
``base`` serves every organism part instead of each part defining its own.

The change has one table: the new box kind materialises ``box_incubator`` and
the generated views in their current (spec 5) shape (a box kind has no
``v_box_<kind>`` view any more; each existing box gets a ``v_box_<box name>``
view), and every kind gains the generated DDL the planner derives from the
ledger's own (now v3) schema.  The step therefore

* places the repository preset copies the ledger lists (files are not Dolt
  content, and the spec-5 loader needs a current copy for every stock kind's
  ``statuses:``), then puts the two **frozen copies** shipped next to this file
  over their repository versions: **base v3** (the repository copy is v4 since
  spec 5's ``grid96`` -> ``grid8x12`` rename, and this wave-10 step must keep
  installing the content whose counter it bumps) and **cell_culture v1**
  (ticket 28 bumps the repository copy to v2, and this step must not install a
  later preset's content).  Ticket rq9 bumps ``proteins`` and
  ``nucleic_acids`` to v2, so the step also keeps their **frozen v1 copies**
  the same way.  Retrofitting the old target copy into every earlier step that
  places the whole closure is the rule in ``migration/README.md``,
* applies the **additive** diff plan for the new kind (a destructive step is
  refused before anything is written),
* bumps ``preset_versions.base`` to 3 and the fingerprint; the shared runner
  regenerates the derived ``dolt_tests`` from the same schema in that commit,
* and, when the plan is already empty (another wave-10 preset step ran first
  and placed the same v3 schema), recreates the generated views so the commit
  still has its own statements.

Run it by hand, **after** ``migration/spec/4-to-5``: the wave-11 tool formats
the ledger at spec 5, and this step keeps the base-3 content that
``migration/presets/base/3-to-4`` later renames to base 4::

    uv run migration/presets/base/2-to-3/migrate.py --ledger PATH [--branch B] [--dry-run]

On a wave-11 upgrade the order is ``migration/spec/4-to-5`` first, then the
preset steps (``base/2-to-3``, ``molbio/2-to-3``), and
``migration/presets/base/3-to-4`` last.

A ledger that lists other presets also has their current copies placed before
it opens; that is how the wave-10 helper keeps the spec-5 loader happy.  Their
own counters are bumped by their own steps (``molbio/2-to-3`` here), and each
step is idempotent: whichever runs first may already create the other's new
tables, and the second one then finds nothing to do but bumps its counter.
"""

from __future__ import annotations

import sys
from pathlib import Path

from tetramark.core.ledger import META_SPEC_VERSION, Ledger
from tetramark.errors import LedgerError
from tetramark.migrate import (
    MigrationError,
    MigrationStep,
    preset_args,
    restore_files,
    run,
    vendor_preset_files,
    vendor_repo_presets,
)
from tetramark.schema.ddl import view_statements
from tetramark.schema.diff import build_plan
from tetramark.schema.loader import load_ledger_schema
from tetramark.schema.model import SPEC_VERSION, EffectiveSchema

#: The repository root: this file is ``migration/presets/base/2-to-3/``.
REPO_ROOT = Path(__file__).resolve().parents[4]
REPO_PRESETS = REPO_ROOT / "presets"

#: The frozen proteins v1 and nucleic_acids v1 this step keeps over the
#: repository copies: ticket rq9 bumps both parts to v2, and an earlier step
#: that places the whole current closure must not install that content behind
#: the parts' own counters (``migration/presets/proteins/1-to-2`` and
#: ``migration/presets/nucleic_acids/1-to-2`` own the bumps).
FROZEN_PROTEINS = Path(__file__).with_name("proteins-1.yaml")
FROZEN_NUCLEIC_ACIDS = Path(__file__).with_name("nucleic_acids-1.yaml")

PRESET = "base"
TO_VERSION = 3

#: The frozen base v3 this step installs over the repository copy: since spec 5
#: (ticket 30) the repository's ``presets/base.yaml`` is v4 (``grid96`` renamed
#: to ``grid8x12``), and this step must keep placing the v3 content whose
#: counter it bumps.  ``migration/README.md`` requires the retrofit.
FROZEN_BASE = Path(__file__).with_name("base-3.yaml")

#: The frozen cell_culture v1 this step keeps over the repository copy: ticket
#: 28 bumped ``presets/cell_culture.yaml`` to v2, whose counter is bumped by
#: ``migration/presets/cell_culture/1-to-2``.  Placing the v2 content here would
#: install a later preset version behind its own step's back.
FROZEN_CELL_CULTURE = Path(__file__).with_name("cell_culture-1.yaml")


def _require_ready(ledger: Ledger) -> None:
    """The ledger must be spec 5 and carry the v3 base copy this step bumps to."""
    raw = ledger.meta.get(META_SPEC_VERSION)
    try:
        current = int(str(raw).strip())
    except (TypeError, ValueError):
        current = 0
    if current != SPEC_VERSION:
        raise LedgerError(
            f"refusing to migrate base 2 -> 3: ledger {ledger.name!r} is at "
            f"spec_version {current}, but the v3 preset needs spec {SPEC_VERSION}. "
            "Run migration/spec/4-to-5/migrate.py first; nothing was written."
        )
    version = ledger.schema.preset_versions.get(PRESET)
    if version != TO_VERSION:
        raise LedgerError(
            f"refusing to migrate base 2 -> 3: the ledger's own base copy is "
            f"version {version}, expected {TO_VERSION}. This step places the "
            "frozen base-3.yaml next to migrate.py over <ledger>/presets/base.yaml "
            "(the repository copy is v4 since spec 5; files are not Dolt content; "
            "migration/spec/4-to-5/README.md explains the upgrade order). "
            "Nothing was written."
        )
    if "incubator" not in ledger.schema.kinds:
        raise LedgerError(
            "refusing to migrate base 2 -> 3: the loaded base copy has no "
            "incubator box kind, so it is not the v3 preset this step "
            "upgrades to. Nothing was written."
        )


def plan(ledger: Ledger) -> list[str]:
    """Refuse an unprepared ledger, else apply the additive incubator DDL."""
    _require_ready(ledger)
    schema_plan = build_plan(ledger.driver, ledger.schema, ledger_name=ledger.name)
    if schema_plan.destructive:
        lines = [
            f"  {index}. [{step.tag}] {step.action} {step.name}: {step.sql}"
            for index, step in enumerate(schema_plan.steps)
            if step.destructive
        ]
        raise LedgerError(
            "refusing to migrate base 2 -> 3: the diff plan has destructive "
            "step(s) that would drop or change existing schema or data:\n"
            + "\n".join(lines)
            + "\nNothing was written. This step only adds the incubator kind "
            "(and recreates views); fix the ledger's local schema first."
        )
    statements = [step.sql for step in schema_plan.steps]
    if statements:
        return statements
    # Another preset step may already have created the same target schema
    # (each step places the whole current preset closure before it opens the
    # ledger).  The counter bump then needs a statement of its own: recreate
    # the generated views, exactly like the content-only base 1 -> 2 step.
    views = view_statements(ledger.schema)
    return [f"DROP VIEW IF EXISTS `{statement.name}`" for statement in views] + [
        statement.sql for statement in views
    ]


def target_schema(ledger: Ledger) -> EffectiveSchema:
    """The schema after the step: the ledger's own (just placed) v3 copy."""
    return ledger.schema


def reopened_schema(ledger: Ledger) -> EffectiveSchema:
    """The schema the ledger opens with from its own (placed) copy."""
    _config, schema = load_ledger_schema(ledger.path)
    return schema


step = MigrationStep(
    kind="preset",
    preset=PRESET,
    from_version=2,
    plan=plan,
    schema=target_schema,
    reopened_schema=reopened_schema,
    description=(
        "place the base v3 preset copy (slotless incubator box kind), apply the "
        "additive diff that creates box_incubator and its view, and bump "
        "preset_versions.base"
    ),
)


def main(argv: list[str] | None = None) -> None:
    """Place the preset copies, then run the shared one-commit step.

    Presets the ledger lists are placed from the repository; base is held at
    the frozen v3 (the repository copy is v4 since spec 5) and cell_culture at
    the frozen v1 (the repository copy is v2 since ticket 28), because each
    older step installs the content whose counter it bumps.
    """
    arguments = preset_args(argv)
    saved = vendor_repo_presets(arguments.ledger, REPO_PRESETS)
    saved += vendor_preset_files(
        arguments.ledger,
        {"base": FROZEN_BASE, "cell_culture": FROZEN_CELL_CULTURE},
    )
    saved += vendor_preset_files(
        arguments.ledger,
        {"proteins": FROZEN_PROTEINS, "nucleic_acids": FROZEN_NUCLEIC_ACIDS},
    )
    try:
        run(
            step,
            ledger_path=arguments.ledger,
            branch=arguments.branch,
            dry_run=arguments.dry_run,
        )
    except MigrationError:
        # A commit may exist and the DDL may be partial; the placed copies are
        # the target content a retry needs, so keep them.
        raise SystemExit(1)
    except LedgerError as error:
        restore_files(saved)
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(error.exit_code)
    else:
        if arguments.dry_run:
            restore_files(saved)


if __name__ == "__main__":
    main()
