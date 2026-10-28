"""molbio 2 -> 3: the preset becomes a bundle of reusable parts.

Molbio preset version 3 (ticket 27, design §2) has no kinds of its own: it
includes ``base``, ``nucleic_acids``, ``bacteria``, ``proteins`` and
``cell_culture``, so another bundle (``cellbio``, ``angenet``) can share the
parts without duplicate kind names.  Every kind and table name of an existing
molbio ledger stays exactly as it was; the only new tables are the
``nucleic_acids`` additions:

* ``entity_oligo`` — a synthetic oligo design (sequence, direction, target,
  modification, purification, ``tm_c``, and the ``length_nt``/``gc_pct``
  computed from the sequence);
* ``stock_oligo_stock`` — a tube of a resuspended oligo (concentration_µM,
  solvent), with ``statuses: tube`` like every other molbio stock kind.

The step therefore

* places the whole current preset closure the ledger lists — ``base`` v3
  (slotless ``incubator`` box kind) and the four parts — **before** the runner
  opens the ledger (files are not Dolt content; the spec-4 loader needs the
  parts and every stock kind's ``statuses:``), then puts the **frozen
  cell_culture v1** shipped next to this file over its own copy: ticket 28
  bumps the repository's ``presets/cell_culture.yaml`` to v2, which this
  historical step must not install (its counter stops at 1 and
  ``migration/presets/cell_culture/1-to-2`` owns the bump),
* applies the **additive** diff plan (oligo tables, the base box kind and their
  views); a destructive step is refused before anything is written,
* bumps ``preset_versions.molbio`` to 3 and records every part at version 1 in
  the same commit (``preset_versions_extra``), then the shared runner writes the
  target fingerprint and regenerates ``dolt_tests`` from the same schema,
* and, when the plan is already empty (the base ``2 -> 3`` step ran first and
  placed the same schema), recreates the generated views so the commit has its
  own statements.

``base``'s own counter stays at 2 here: ``migration/presets/base/2-to-3`` bumps
it, exactly as the molbio 1 -> 2 step leaves base's counter to the base step.
Run this step by hand, **after** ``migration/spec/3-to-4`` where the ledger was
still pre-spec-4::

    uv run migration/presets/molbio/2-to-3/migrate.py --ledger PATH [--branch B] [--dry-run]

No table or kind is renamed, so this step ships no ``renames.yaml``.
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

#: The repository root: this file is ``migration/presets/molbio/2-to-3/``.
REPO_ROOT = Path(__file__).resolve().parents[4]
REPO_PRESETS = REPO_ROOT / "presets"

PRESET = "molbio"
TO_VERSION = 3

#: The parts molbio v3 includes; every one starts at version 1.
PARTS = ("nucleic_acids", "bacteria", "proteins", "cell_culture")

#: ``ledger_meta.preset_versions`` entries this step adds next to molbio's bump.
PRESET_VERSIONS_EXTRA = {part: 1 for part in PARTS}

#: The frozen cell_culture v1 this step installs over the repository copy.
#: Ticket 28 bumps ``presets/cell_culture.yaml`` to v2; molbio v3 was released
#: with cell_culture 1 and its counter is bumped by
#: ``migration/presets/cell_culture/1-to-2``, not here.  The other parts are
#: still the current repository version, which is why they are placed from
#: ``presets/`` above.
FROZEN_PART_FILES = {
    "cell_culture": Path(__file__).with_name("cell_culture-1.yaml"),
}

#: The tooling facts the v3 bundle needs in the ledger's own copy.
REQUIRED_KINDS = ("oligo", "oligo_stock")


def _require_ready(ledger: Ledger) -> None:
    """The ledger must be spec 4 and carry the v3 bundle this step bumps to."""
    raw = ledger.meta.get(META_SPEC_VERSION)
    try:
        current = int(str(raw).strip())
    except (TypeError, ValueError):
        current = 0
    if current != SPEC_VERSION:
        raise LedgerError(
            f"refusing to migrate molbio 2 -> 3: ledger {ledger.name!r} is at "
            f"spec_version {current}, but the v3 bundle needs spec {SPEC_VERSION}. "
            "Run migration/spec/3-to-4/migrate.py first; nothing was written."
        )
    versions = ledger.schema.preset_versions
    version = versions.get(PRESET)
    if version != TO_VERSION:
        raise LedgerError(
            f"refusing to migrate molbio 2 -> 3: the ledger's own molbio copy is "
            f"version {version}, expected {TO_VERSION}. Copy this checkout's "
            "presets/molbio.yaml and every file it includes into "
            "<ledger>/presets/ first (files are not Dolt content; "
            "migration/spec/3-to-4/README.md explains the upgrade order). "
            "Nothing was written."
        )
    for part in PARTS:
        if versions.get(part) != PRESET_VERSIONS_EXTRA[part]:
            raise LedgerError(
                f"refusing to migrate molbio 2 -> 3: the molbio copy includes "
                f"part {part!r} at version {versions.get(part)!r}, expected "
                f"{PRESET_VERSIONS_EXTRA[part]}; the ledger's preset copy is not "
                "the v3 bundle this step upgrades to. Nothing was written."
            )
    missing = [kind for kind in REQUIRED_KINDS if kind not in ledger.schema.kinds]
    if missing:
        raise LedgerError(
            "refusing to migrate molbio 2 -> 3: the loaded bundle has no "
            + ", ".join(missing)
            + " kind(s), so it is not the v3 preset this step upgrades to. "
            "Nothing was written."
        )


def plan(ledger: Ledger) -> list[str]:
    """Refuse an unprepared ledger, else apply the additive oligo/base DDL."""
    _require_ready(ledger)
    schema_plan = build_plan(ledger.driver, ledger.schema, ledger_name=ledger.name)
    if schema_plan.destructive:
        lines = [
            f"  {index}. [{step.tag}] {step.action} {step.name}: {step.sql}"
            for index, step in enumerate(schema_plan.steps)
            if step.destructive
        ]
        raise LedgerError(
            "refusing to migrate molbio 2 -> 3: the diff plan has destructive "
            "step(s) that would drop or change existing schema or data:\n"
            + "\n".join(lines)
            + "\nNothing was written. This step only adds the oligo tables, the "
            "base incubator kind and their views; it never drops a molbio table "
            "or column."
        )
    statements = [step.sql for step in schema_plan.steps]
    if statements:
        return statements
    # The base 2 -> 3 step may already have placed the same target schema and
    # created these tables.  The counter bump then needs a statement of its
    # own: recreate the generated views.
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
    preset_versions_extra=PRESET_VERSIONS_EXTRA,
    description=(
        "place the molbio v3 bundle and its parts, add the oligo/oligo_stock "
        "tables (and the base incubator kind), and bump preset_versions.molbio "
        "plus a key per part"
    ),
)


def main(argv: list[str] | None = None) -> None:
    """Place the preset copies, then run the shared one-commit step.

    Every preset the ledger lists is placed from the repository (the spec-4
    loader needs current copies), except the parts this step froze at the
    version whose counter it records.
    """
    arguments = preset_args(argv)
    saved = vendor_repo_presets(arguments.ledger, REPO_PRESETS)
    saved += vendor_preset_files(arguments.ledger, FROZEN_PART_FILES)
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
