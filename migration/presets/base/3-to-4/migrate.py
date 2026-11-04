"""base 3 -> 4: rename the box kind ``grid96`` to ``grid8x12``.

Base preset version 4 (spec 5, ticket 30) keeps the geometry of the old
``grid96`` kind -- rows A-H, columns 1-12 -- but renames it: spec 5 generates
one view per **existing box** (``v_box_<box name>``) instead of one per box
kind, so a kind is no longer named after a view and ``grid8x12`` says what the
geometry is.  Nothing else in the preset changes.

The step therefore

* places the repository preset copies the ledger lists (files are not Dolt
  content; the spec-5 loader needs a current copy for every stock kind's
  ``statuses:``), then puts the **frozen base v4** shipped next to this file
  over its own copy: ``presets/`` only ever holds the current version, so a
  later bump must not change what this historical step installs.  Ticket rq9
  bumps ``proteins`` and ``nucleic_acids`` to v2, so this step also keeps their
  **frozen v1 copies**; a ledger must reach those parts' counters through their
  own steps, not behind them,
* applies the diff plan with the explicit rename map from ``renames.yaml`` --
  the kind table ``box_grid96`` -> ``box_grid8x12``, its constraints and the
  core ``kind`` discriminator values; the per-box views that ``spec/4-to-5``
  built are already the spec-5 shape and read ``box``/``slot``/``aliquot``, not
  the kind table, so they stay as they are (when the plan is empty the step
  recreates the generated views so its commit has statements of its own).  A
  plan with any *other* destructive step is refused before anything is
  written,
* bumps ``preset_versions.base`` to 4 and the fingerprint; the shared runner
  regenerates the derived ``dolt_tests`` from the same schema in that commit.

Run it by hand, **after** ``migration/spec/4-to-5``: that step keeps the
ledger's own base-v3 copy (and so its ``grid96`` kind) while it creates the
per-box views, then this step replaces the copy and renames the kind::

    uv run migration/presets/base/3-to-4/migrate.py --ledger PATH [--branch B] [--dry-run]

The frozen ``base-3.yaml`` next to this file is the content this step moves a
ledger from: it is the rollback copy (put it back after
``dolt reset --hard pre-migrate-base-3``) and the shape the test fixture uses.
"""

from __future__ import annotations

import sys
from pathlib import Path

from tetramark.core.ledger import META_SPEC_VERSION, Ledger
from tetramark.errors import LedgerError
from tetramark.migrate import (
    MigrationError,
    MigrationStep,
    load_renames,
    preset_args,
    restore_files,
    run,
    vendor_preset_files,
    vendor_repo_presets,
)
from tetramark.schema.ddl import view_statements
from tetramark.schema.diff import build_plan
from tetramark.schema.loader import load_ledger_schema
from tetramark.schema.model import EffectiveSchema

#: The spec format this historical preset step upgrades to.  Pinned: a
#: later ``SPEC_VERSION`` bump must not retarget a frozen preset step.  A ledger
#: on a newer format still runs it only while the preset counters/kinds are
#: still those of this historical step; the manual spec/5-to-6 upgrade runs
#: after the pending preset steps.
REQUIRED_SPEC = 5

#: The repository root: this file is ``migration/presets/base/3-to-4/``.
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
TO_VERSION = 4

#: The frozen base v4 this step installs over the repository copy: a later
#: release bumps ``presets/base.yaml`` again, and this step must keep installing
#: the content whose counter it bumps.
FROZEN_BASE = Path(__file__).with_name("base-4.yaml")

#: The frozen bacteria v1 this step installs over the repository copy.
#: Tetramark-epn.4 (spec 6) bumps ``presets/bacteria.yaml`` to v2 by dropping
#: its ``statuses:`` keys; a historical step that places the whole closure and
#: records the part counters must keep installing the v1 content whose counter
#: its era reads (``migration/README.md`` requires the retrofit).
FROZEN_BACTERIA = Path(__file__).with_name("bacteria-1.yaml")

#: The frozen cell_culture v2 this step installs over the repository copy.
#: Tetramark-epn.4 (spec 6) bumps ``presets/cell_culture.yaml`` to v3; this
#: step's era reads v2 (the content ``cell_culture/1-to-2`` installs), so it
#: keeps that copy.
FROZEN_CELL_CULTURE = Path(__file__).with_name("cell_culture-2.yaml")

#: The explicit rename map: the geometry is unchanged, only the kind name is.
RENAMES = Path(__file__).with_name("renames.yaml")

#: The kinds the target copy must / must not define (spec 5's rename).
NEW_KIND = "grid8x12"
OLD_KIND = "grid96"


def _require_ready(ledger: Ledger) -> None:
    """The ledger must be at least spec 5 and carry the v4 base copy this step bumps to."""
    raw = ledger.meta.get(META_SPEC_VERSION)
    try:
        current = int(str(raw).strip())
    except (TypeError, ValueError):
        current = 0
    if current < REQUIRED_SPEC:
        raise LedgerError(
            f"refusing to migrate base 3 -> 4: ledger {ledger.name!r} is at "
            f"spec_version {current}, but the v4 preset needs spec {REQUIRED_SPEC}. "
            "Run migration/spec/4-to-5/migrate.py first; nothing was written."
        )
    version = ledger.schema.preset_versions.get(PRESET)
    if version != TO_VERSION:
        raise LedgerError(
            f"refusing to migrate base 3 -> 4: the ledger's own base copy is "
            f"version {version}, expected {TO_VERSION}. This step places the "
            "frozen base-4.yaml next to migrate.py over <ledger>/presets/base.yaml "
            "(files are not Dolt content; migration/spec/4-to-5/README.md "
            "explains the upgrade order). Nothing was written."
        )
    if NEW_KIND not in ledger.schema.kinds:
        raise LedgerError(
            f"refusing to migrate base 3 -> 4: the loaded base copy has no "
            f"{NEW_KIND!r} box kind, so it is not the v4 preset this step "
            "upgrades to. Nothing was written."
        )
    if OLD_KIND in ledger.schema.kinds:
        raise LedgerError(
            f"refusing to migrate base 3 -> 4: the loaded schema still defines "
            f"the old {OLD_KIND!r} box kind (probably a local kinds/ file), so "
            "the rename target already looks ambiguous. Remove the local kind "
            "first; nothing was written."
        )


def plan(ledger: Ledger) -> list[str]:
    """Refuse an unprepared ledger, else apply the rename plan."""
    _require_ready(ledger)
    renames = load_renames(RENAMES)
    schema_plan = build_plan(
        ledger.driver,
        ledger.schema,
        ledger_name=ledger.name,
        renames=renames,
    )
    if schema_plan.destructive:
        lines = [
            f"  {index}. [{step.tag}] {step.action} {step.name}: {step.sql}"
            for index, step in enumerate(schema_plan.steps)
            if step.destructive
        ]
        raise LedgerError(
            "refusing to migrate base 3 -> 4: the diff plan has destructive "
            "step(s) beyond the grid96 -> grid8x12 rename that would drop or "
            "change existing schema or data:\n"
            + "\n".join(lines)
            + "\nNothing was written. This step only renames the box kind; "
            "fix the ledger's local schema first."
        )
    statements = [step.sql for step in schema_plan.steps]
    if statements:
        return statements
    # Another step may already have created the renamed table (the step is
    # idempotent like every wave-10 preset step).  The counter bump then needs
    # statements of its own: recreate the generated views, exactly like the
    # content-only steps do.
    views = view_statements(ledger.schema)
    return [f"DROP VIEW IF EXISTS `{statement.name}`" for statement in views] + [
        statement.sql for statement in views
    ]


def target_schema(ledger: Ledger) -> EffectiveSchema:
    """The schema after the step: the ledger's own (just placed) v4 copy."""
    return ledger.schema


def reopened_schema(ledger: Ledger) -> EffectiveSchema:
    """The schema the ledger opens with from its own (placed) copy."""
    _config, schema = load_ledger_schema(ledger.path)
    return schema


step = MigrationStep(
    kind="preset",
    preset=PRESET,
    from_version=3,
    plan=plan,
    schema=target_schema,
    reopened_schema=reopened_schema,
    description=(
        "place the base v4 preset copy (the box kind grid96 renamed to "
        "grid8x12), apply the rename plan from renames.yaml, and bump "
        "preset_versions.base"
    ),
)


def main(argv: list[str] | None = None) -> None:
    """Place the preset copies, then run the shared one-commit step.

    Every preset the ledger lists is placed from the repository; base itself
    comes from the frozen v4 file, because that is the version this counter
    bumps to and a later repository bump must not change it.
    """
    arguments = preset_args(argv)
    saved = vendor_repo_presets(arguments.ledger, REPO_PRESETS)
    saved += vendor_preset_files(
        arguments.ledger,
        {
            PRESET: FROZEN_BASE,
            "bacteria": FROZEN_BACTERIA,
            "cell_culture": FROZEN_CELL_CULTURE,
        },
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
