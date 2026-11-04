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

* places the whole current preset closure the ledger lists — the four parts and
  ``base`` — **before** the runner opens the ledger (files are not Dolt content;
  the spec-5 loader needs the parts and every stock kind's ``statuses:``), then
  puts the **frozen copies** shipped next to this file over their repository
  versions: **cell_culture v1** (ticket 28 bumps the repository copy to v2, which
  this historical step must not install: its counter stops at 1 and
  ``migration/presets/cell_culture/1-to-2`` owns the bump) and **base v3** (the
  repository copy is v4 since spec 5's ``grid96`` -> ``grid8x12`` rename, and
  this step must keep installing the base whose counter
  ``migration/presets/base/2-to-3`` bumps).  Ticket rq9 bumps ``proteins``
  and ``nucleic_acids`` to v2, so the step also keeps their **frozen v1
  copies** (their own steps own the bumps).  Freezing the old target copy into
  an earlier step that places the whole closure is the rule in
  ``migration/README.md``,
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
Run this step by hand under the wave-11 tool **after**
``migration/spec/4-to-5`` (the ledger formats at spec 5 first)::

    uv run migration/presets/molbio/2-to-3/migrate.py --ledger PATH [--branch B] [--dry-run]

On a wave-11 upgrade the order is ``migration/spec/4-to-5``, then
``base/2-to-3`` and this step (either order), then
``migration/presets/base/3-to-4`` last: the base v3 copy placed here is what that
final step renames to v4.

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
from tetramark.schema.model import EffectiveSchema

#: The spec format this historical preset step upgrades to.  Pinned: a
#: later ``SPEC_VERSION`` bump must not retarget a frozen preset step.  A ledger
#: on a newer format still runs it only while the preset counters/kinds are
#: still those of this historical step; the manual spec/5-to-6 upgrade runs
#: after the pending preset steps.
REQUIRED_SPEC = 5

#: The repository root: this file is ``migration/presets/molbio/2-to-3/``.
REPO_ROOT = Path(__file__).resolve().parents[4]
REPO_PRESETS = REPO_ROOT / "presets"

#: The frozen proteins v1 and nucleic_acids v1 this step keeps over the
#: repository copies: ticket rq9 bumps both parts to v2, and an earlier step
#: that places the whole current closure must not install that content behind
#: the parts' own counters (``migration/presets/proteins/1-to-2`` and
#: ``migration/presets/nucleic_acids/1-to-2`` own the bumps).
FROZEN_PROTEINS = Path(__file__).with_name("proteins-1.yaml")
FROZEN_NUCLEIC_ACIDS = Path(__file__).with_name("nucleic_acids-1.yaml")

PRESET = "molbio"
TO_VERSION = 3

#: The parts molbio v3 includes; every one starts at version 1.
PARTS = ("nucleic_acids", "bacteria", "proteins", "cell_culture")

#: ``ledger_meta.preset_versions`` entries this step adds next to molbio's bump.
PRESET_VERSIONS_EXTRA = {part: 1 for part in PARTS}

#: The frozen part copies this step installs over the repository copies.
#: Ticket 28 bumps ``presets/cell_culture.yaml`` to v2; molbio v3 was released
#: with cell_culture 1 and its counter is bumped by
#: ``migration/presets/cell_culture/1-to-2``, not here.  Tetramark-epn.4 (spec
#: 6) bumps ``presets/bacteria.yaml`` to v2 the same way, and this step records
#: ``bacteria`` at version 1, so it keeps the frozen v1 content.  The other
#: parts are still the current repository version, which is why they are placed
#: from ``presets/`` above.
FROZEN_PART_FILES = {
    "cell_culture": Path(__file__).with_name("cell_culture-1.yaml"),
    "bacteria": Path(__file__).with_name("bacteria-1.yaml"),
}

#: The frozen base v3 this step installs over the repository copy: spec 5
#: (ticket 30) bumps the repository's ``presets/base.yaml`` to v4 (``grid96``
#: renamed to ``grid8x12``), and molbio v3 ships on the base v3 content whose
#: counter ``migration/presets/base/2-to-3`` bumps.  ``migration/README.md``
#: requires the retrofit.
FROZEN_BASE = Path(__file__).with_name("base-3.yaml")

#: The tooling facts the v3 bundle needs in the ledger's own copy.
REQUIRED_KINDS = ("oligo", "oligo_stock")


def _require_ready(ledger: Ledger) -> None:
    """The ledger must be at least spec 5 and carry the v3 bundle this step bumps to."""
    raw = ledger.meta.get(META_SPEC_VERSION)
    try:
        current = int(str(raw).strip())
    except (TypeError, ValueError):
        current = 0
    if current < REQUIRED_SPEC:
        raise LedgerError(
            f"refusing to migrate molbio 2 -> 3: ledger {ledger.name!r} is at "
            f"spec_version {current}, but the v3 bundle needs spec {REQUIRED_SPEC}. "
            "Run migration/spec/4-to-5/migrate.py first; nothing was written."
        )
    versions = ledger.schema.preset_versions
    version = versions.get(PRESET)
    if version != TO_VERSION:
        raise LedgerError(
            f"refusing to migrate molbio 2 -> 3: the ledger's own molbio copy is "
            f"version {version}, expected {TO_VERSION}. This step places the "
            "current molbio bundle/parts plus the frozen base-3.yaml and "
            "cell_culture-1.yaml over <ledger>/presets/ (files are not Dolt "
            "content; migration/spec/4-to-5/README.md explains the upgrade "
            "order). Nothing was written."
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
        "place the molbio v3 bundle and its parts (with the frozen base v3 and "
        "cell_culture v1 copies), add the oligo/oligo_stock tables (and the base "
        "incubator kind), and bump preset_versions.molbio plus a key per part"
    ),
)


def main(argv: list[str] | None = None) -> None:
    """Place the preset copies, then run the shared one-commit step.

    Every preset the ledger lists is placed from the repository (the spec-5
    loader needs current copies), except ``base`` and ``cell_culture``: each is
    frozen at the version whose counter this step's own era records.
    """
    arguments = preset_args(argv)
    saved = vendor_repo_presets(arguments.ledger, REPO_PRESETS)
    saved += vendor_preset_files(arguments.ledger, FROZEN_PART_FILES)
    saved += vendor_preset_files(arguments.ledger, {"base": FROZEN_BASE})
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
