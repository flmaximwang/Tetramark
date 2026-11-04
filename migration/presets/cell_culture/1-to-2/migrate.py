"""cell_culture 1 -> 2: BioManager's cell-line/sample extras (ticket 28).

Cell-culture preset version 2 adds what BioManager records about a cultured
line and the material taken from it (design §2.1):

* ``cell_vial`` gains ``selection``, ``mycoplasma`` (enum
  ``not_tested``/``negative``/``positive``, default ``not_tested``) and
  ``mycoplasma_tested_on``; its ``statuses: cryo`` is unchanged;
* the stock kind ``cell_biosample`` (``statuses: tube``, ``entity_kinds:
  [cell_line]``) materialises ``stock_cell_biosample`` and its view - the
  cell-side half of the biosample model, which lives here because a stock's
  ``entity_kinds`` may only name entity kinds the ledger loads (ticket 28's
  option (a); the animal half is ``animal_biosample`` in ``biosamples``).

The step therefore

* places the whole current preset closure the ledger lists - the parts and the
  bundles - **before** the runner opens the ledger (files are not Dolt content,
  and the spec-5 loader needs a current copy for every stock kind's
  ``statuses:``), then puts the **frozen copies** shipped next to this file over
  their repository versions: **cell_culture v2** (the repository's
  ``presets/`` only holds the current version) and **base v3** (the repository
  copy is v4 since spec 5's ``grid96`` -> ``grid8x12`` rename; this wave-10
  step must keep installing the base content of its era, per the retrofit rule
  in ``migration/README.md``).  Ticket rq9 bumps ``proteins`` and
  ``nucleic_acids`` to v2, so the step also keeps their **frozen v1 copies**,
* applies the **additive** diff plan (three ``stock_cell_vial`` columns, the
  ``stock_cell_biosample`` table and the regenerated views); a destructive plan
  is refused before anything is written,
* bumps ``preset_versions.cell_culture`` to 2 and records the target
  fingerprint; the shared runner regenerates the derived ``dolt_tests`` from
  the same schema in that commit,
* and, when the plan is already empty (the schema was applied by hand or
  already carries the same content), recreates the generated views so the
  commit has its own statements.

Run it by hand under the wave-11 tool **after** ``migration/spec/4-to-5``
(the ledger formats at spec 5 first) and, on a molbio ledger, after
``migration/presets/molbio/2-to-3`` (that step installs the frozen cell_culture
v1 this one upgrades).  Run it **before** ``migration/presets/base/3-to-4``,
which renames the base v3 copy this step keeps::

    uv run migration/presets/cell_culture/1-to-2/migrate.py --ledger PATH [--branch B] [--dry-run]

A molbio-v2 ledger that has not run ``molbio/2-to-3`` does not list cell_culture
at all (the cell kinds were inside molbio v2), so this step refuses it; run the
molbio step first.
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

#: The repository root: this file is ``migration/presets/cell_culture/1-to-2/``.
REPO_ROOT = Path(__file__).resolve().parents[4]
REPO_PRESETS = REPO_ROOT / "presets"

#: The frozen proteins v1 and nucleic_acids v1 this step keeps over the
#: repository copies: ticket rq9 bumps both parts to v2, and an earlier step
#: that places the whole current closure must not install that content behind
#: the parts' own counters (``migration/presets/proteins/1-to-2`` and
#: ``migration/presets/nucleic_acids/1-to-2`` own the bumps).
FROZEN_PROTEINS = Path(__file__).with_name("proteins-1.yaml")
FROZEN_NUCLEIC_ACIDS = Path(__file__).with_name("nucleic_acids-1.yaml")

PRESET = "cell_culture"
TO_VERSION = 2

#: The frozen cell_culture v2 this step installs over the repository copy:
#: after a later release ``presets/cell_culture.yaml`` is a newer version, so
#: the historical step must carry the content whose counter it bumps.
FROZEN_CELL_CULTURE = Path(__file__).with_name("cell_culture-2.yaml")

#: The frozen base v3 this step installs over the repository copy: spec 5
#: (ticket 30) bumps the repository's ``presets/base.yaml`` to v4 (``grid96``
#: renamed to ``grid8x12``), and this wave-10 step must keep installing the base
#: content its era reads.  ``migration/README.md`` requires the retrofit.
FROZEN_BASE = Path(__file__).with_name("base-3.yaml")

#: What the target content must contain, checked before anything is written.
REQUIRED_COLUMNS = ("selection", "mycoplasma", "mycoplasma_tested_on")
REQUIRED_KINDS = ("cell_biosample",)


def _require_ready(ledger: Ledger) -> None:
    """The ledger must be at least spec 5 and carry the v2 copy this step bumps to."""
    raw = ledger.meta.get(META_SPEC_VERSION)
    try:
        current = int(str(raw).strip())
    except (TypeError, ValueError):
        current = 0
    if current < REQUIRED_SPEC:
        raise LedgerError(
            f"refusing to migrate cell_culture 1 -> 2: ledger {ledger.name!r} is "
            f"at spec_version {current}, but the v2 preset needs spec "
            f"{REQUIRED_SPEC}. Run migration/spec/4-to-5/migrate.py first; "
            "nothing was written."
        )
    versions = ledger.schema.preset_versions
    version = versions.get(PRESET)
    if version != TO_VERSION:
        raise LedgerError(
            f"refusing to migrate cell_culture 1 -> 2: the ledger's own "
            f"cell_culture copy is version {version}, expected {TO_VERSION}. "
            "This step places the frozen cell_culture-2.yaml and base-3.yaml "
            "next to migrate.py over <ledger>/presets/ first (files are not "
            "Dolt content); or run migration/presets/molbio/2-to-3/migrate.py "
            "when the ledger still lists molbio v2. Nothing was written."
        )
    vial = ledger.schema.kinds.get("cell_vial")
    missing = [
        name
        for name in REQUIRED_COLUMNS
        if vial is None or name not in vial.fields
    ]
    if missing:
        raise LedgerError(
            "refusing to migrate cell_culture 1 -> 2: the loaded cell_vial kind "
            "has no " + ", ".join(missing) + " field(s), so it is not the v2 "
            "preset this step upgrades to. Nothing was written."
        )
    absent = [kind for kind in REQUIRED_KINDS if kind not in ledger.schema.kinds]
    if absent:
        raise LedgerError(
            "refusing to migrate cell_culture 1 -> 2: the loaded preset has no "
            + ", ".join(absent)
            + " kind, so it is not the v2 content this step upgrades to. "
            "Nothing was written."
        )


def plan(ledger: Ledger) -> list[str]:
    """Refuse an unprepared ledger, else apply the additive v2 DDL."""
    _require_ready(ledger)
    schema_plan = build_plan(ledger.driver, ledger.schema, ledger_name=ledger.name)
    if schema_plan.destructive:
        lines = [
            f"  {index}. [{step.tag}] {step.action} {step.name}: {step.sql}"
            for index, step in enumerate(schema_plan.steps)
            if step.destructive
        ]
        raise LedgerError(
            "refusing to migrate cell_culture 1 -> 2: the diff plan has "
            "destructive step(s) that would drop or change existing schema or "
            "data:\n"
            + "\n".join(lines)
            + "\nNothing was written. This step only adds the cell_vial fields, "
            "the cell_biosample kind and their views; fix the ledger's local "
            "schema first."
        )
    statements = [step.sql for step in schema_plan.steps]
    if statements:
        return statements
    # The content may already be applied (another wave-10 step places the same
    # preset closure, or the schema was applied by hand).  The counter bump then
    # needs a statement of its own: recreate the generated views, exactly like
    # the content-only base 1 -> 2 step.
    views = view_statements(ledger.schema)
    return [f"DROP VIEW IF EXISTS `{statement.name}`" for statement in views] + [
        statement.sql for statement in views
    ]


def target_schema(ledger: Ledger) -> EffectiveSchema:
    """The schema after the step: the ledger's own (just placed) v2 copy."""
    return ledger.schema


def reopened_schema(ledger: Ledger) -> EffectiveSchema:
    """The schema the ledger opens with from its own (placed) copy."""
    _config, schema = load_ledger_schema(ledger.path)
    return schema


step = MigrationStep(
    kind="preset",
    preset=PRESET,
    from_version=1,
    plan=plan,
    schema=target_schema,
    reopened_schema=reopened_schema,
    description=(
        "place the cell_culture v2 preset copy (cell_vial selection/mycoplasma "
        "fields, the cell_biosample kind), apply the additive diff and bump "
        "preset_versions.cell_culture"
    ),
)


def main(argv: list[str] | None = None) -> None:
    """Place the preset copies, then run the shared one-commit step.

    Every other preset the ledger lists is placed from the repository (the
    spec-5 loader needs current copies); cell_culture comes from the frozen v2
    file, because that is the version this counter bumps to, and base from the
    frozen v3 file (the repository copy is v4 since spec 5).
    """
    arguments = preset_args(argv)
    saved = vendor_repo_presets(arguments.ledger, REPO_PRESETS)
    saved += vendor_preset_files(
        arguments.ledger,
        {PRESET: FROZEN_CELL_CULTURE, "base": FROZEN_BASE},
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
