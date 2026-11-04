"""nucleic_acids 1 -> 2: the IUPAC nucleotide pattern on the oligo sequence.

Nucleic-acids preset version 2 (spec 5, ticket rq9) validates the part's own
sequence field:

* ``oligo.sequence`` gets ``pattern: "[ACGTURYKMSWBDHVN]+"`` (IUPAC nucleotide
  codes).  The ``sequence_fields`` mixin of ``proteins`` already carries the
  same pattern for ``protein_construct`` and ``plasmid``; that part's own step,
  ``migration/presets/proteins/1-to-2``, bumps it.

Patterns are validation-only: the app checks them after sequence normalisation
(``re.fullmatch``) and the generated ``dolt_tests`` check the same pattern, but
no table or column changes.  The step therefore

* places the whole current preset closure the ledger lists **before** the
  runner opens the ledger (files are not Dolt content), then puts the **frozen
  nucleic_acids v2** shipped next to this file over its copy (the target
  content this counter bumps to; a later repository bump must not change it).
  ``proteins`` must already be at version 2 and is placed from the repository
  at that version -- the proteins step must run first,
* re-creates the generated views from the ledger's own (now v2) schema, so the
  commit has statements of its own -- it changes no DDL,
* bumps ``preset_versions.nucleic_acids`` to 2 and the fingerprint; the shared
  runner regenerates the derived ``dolt_tests`` from the same schema in that
  commit, so ``tm check`` now reports oligo rows that do not match the pattern.

Run it by hand under the tool of this release, **after**
``migration/spec/4-to-5``, ``migration/presets/base/3-to-4`` and
``migration/presets/proteins/1-to-2``::

    uv run migration/presets/nucleic_acids/1-to-2/migrate.py --ledger PATH [--branch B] [--dry-run]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from tetramark.core.ledger import META_PRESET_VERSIONS, META_SPEC_VERSION, Ledger
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
from tetramark.schema.loader import load_ledger_schema
from tetramark.schema.model import EffectiveSchema

#: The spec format this historical preset step upgrades to.  Pinned: a
#: later ``SPEC_VERSION`` bump must not retarget a frozen preset step.  A ledger
#: on a newer format still runs it only while the preset counters/kinds are
#: still those of this historical step; the manual spec/5-to-6 upgrade runs
#: after the pending preset steps.
REQUIRED_SPEC = 5

#: The repository root: this file is ``migration/presets/nucleic_acids/1-to-2/``.
REPO_ROOT = Path(__file__).resolve().parents[4]
REPO_PRESETS = REPO_ROOT / "presets"

PRESET = "nucleic_acids"
TO_VERSION = 2

#: The base counter this step needs: it installs the repository's base v4 copy,
#: so ``migration/presets/base/3-to-4`` must have run first.
BASE_VERSION = 4

#: The proteins counter this step needs: its own step installs the pattern on
#: the shared ``sequence_fields`` mixin and must run first.
PROTEINS_VERSION = 2

#: The IUPAC nucleotide pattern, exactly as the v2 preset states it.
NUCLEOTIDE_PATTERN = "[ACGTURYKMSWBDHVN]+"

#: The frozen nucleic_acids v2 content this step installs (the repository's
#: ``presets/`` only holds the current version, so a later bump must not change
#: what this historical step places).
FROZEN_NUCLEIC_ACIDS = Path(__file__).with_name("nucleic_acids-2.yaml")

#: The frozen bacteria v1 this step installs over the repository copy.
#: Tetramark-epn.4 (spec 6) bumps ``presets/bacteria.yaml`` to v2 by dropping
#: its ``statuses:`` keys; a historical step that places the whole closure and
#: records the part counters must keep installing the v1 content whose counter
#: its era reads (``migration/README.md`` requires the retrofit).
FROZEN_BACTERIA = Path(__file__).with_name("bacteria-1.yaml")

#: The frozen base v4 and cell_culture v2 these rq9-era steps read.  Spec 6
#: (Tetramark-epn.4) makes the repository base the fixed model and bumps
#: cell_culture to v3, so the steps that place the whole closure keep the
#: content their era (spec 5, base 4) reads.
FROZEN_BASE = Path(__file__).with_name("base-4.yaml")
FROZEN_CELL_CULTURE = Path(__file__).with_name("cell_culture-2.yaml")

#: The frozen proteins v2 this step requires: it runs after
#: ``migration/presets/proteins/1-to-2`` (the repository copy is v3 since
#: Tetramark-epn.4), so it keeps installing the v2 content it reads.
FROZEN_PROTEINS = Path(__file__).with_name("proteins-2.yaml")


def _recorded_versions(ledger: Ledger) -> dict[str, int]:
    """The counters recorded in ``ledger_meta`` (not the loaded copies)."""
    raw = ledger.meta.get(META_PRESET_VERSIONS)
    try:
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    return {str(name): value for name, value in payload.items()}


def _refuse(message: str) -> LedgerError:
    return LedgerError(
        "refusing to migrate nucleic_acids 1 -> 2: "
        + message
        + " nothing was written."
    )


def _require_ready(ledger: Ledger) -> None:
    """The ledger must be at least spec 5, base 4, proteins 2, and load the v2 target."""
    raw = ledger.meta.get(META_SPEC_VERSION)
    try:
        current = int(str(raw).strip())
    except (TypeError, ValueError):
        current = 0
    if current < REQUIRED_SPEC:
        raise LedgerError(
            f"refusing to migrate nucleic_acids 1 -> 2: ledger {ledger.name!r} "
            f"is at spec_version {current}, but the v2 preset needs spec "
            f"{REQUIRED_SPEC}. Run migration/spec/4-to-5/migrate.py first; "
            "nothing was written."
        )
    recorded = _recorded_versions(ledger)
    if recorded.get("base") != BASE_VERSION:
        raise _refuse(
            f"the ledger records base at version {recorded.get('base')!r}, "
            f"expected {BASE_VERSION}. Run migration/presets/base/3-to-4/"
            "migrate.py first: this step installs the repository's base v4 copy."
        )
    if recorded.get("proteins") != PROTEINS_VERSION:
        raise _refuse(
            "the ledger records proteins at version "
            f"{recorded.get('proteins')!r}, expected {PROTEINS_VERSION}. Run "
            "migration/presets/proteins/1-to-2/migrate.py first: this step "
            "expects the shared sequence_fields pattern already installed."
        )
    loaded = ledger.schema.preset_versions
    if loaded.get("base") != BASE_VERSION:
        raise _refuse(
            f"the loaded base copy is version {loaded.get('base')!r}, expected "
            f"{BASE_VERSION}; the repository copy was not placed over "
            "<ledger>/presets/base.yaml."
        )
    if loaded.get("proteins") != PROTEINS_VERSION:
        raise _refuse(
            f"the loaded proteins copy is version {loaded.get('proteins')!r}, "
            f"expected {PROTEINS_VERSION}; run the proteins step first and do "
            "not place a different proteins copy over the ledger's own."
        )
    if loaded.get(PRESET) != TO_VERSION:
        raise _refuse(
            f"the loaded nucleic_acids copy is version {loaded.get(PRESET)!r}, "
            f"expected {TO_VERSION}; the frozen nucleic_acids-2.yaml next to "
            "migrate.py was not placed over <ledger>/presets/nucleic_acids.yaml."
        )

    oligo = ledger.schema.kinds.get("oligo")
    if oligo is None:
        raise _refuse(
            "the loaded schema has no oligo kind, so it is not the "
            "nucleic_acids preset this step upgrades."
        )
    field = oligo.fields.get("sequence")
    if field is None or field.pattern != NUCLEOTIDE_PATTERN:
        raise _refuse(
            f"oligo.sequence does not carry the v2 pattern "
            f"{NUCLEOTIDE_PATTERN!r}, so the loaded copy is not the target "
            "content."
        )


def plan(ledger: Ledger) -> list[str]:
    """Refuse an unprepared ledger, else recreate the generated views."""
    _require_ready(ledger)
    # No table changes: the pattern is validation metadata that feeds the
    # generated dolt_tests and the app-side check.  Recreating every view makes
    # the step's commit the one that installs the current (spec 5) view shape
    # for this preset; the runner adds the counter/fingerprint bump and the
    # regenerated dolt_tests.
    views = view_statements(ledger.schema)
    return [f"DROP VIEW IF EXISTS `{statement.name}`" for statement in views] + [
        statement.sql for statement in views
    ]


def target_schema(ledger: Ledger) -> EffectiveSchema:
    """The schema after the step: the ledger's own copy, now at version 2."""
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
        "place the nucleic_acids v2 preset copy (IUPAC pattern on "
        "oligo.sequence), recreate the generated views and bump "
        "preset_versions.nucleic_acids"
    ),
)


def main(argv: list[str] | None = None) -> None:
    """Place the preset copies, then run the shared one-commit step.

    Every preset the ledger lists is placed from the repository (proteins must
    already be v2 there); nucleic_acids comes from the frozen v2 file, because
    that is the version this counter bumps to.
    """
    arguments = preset_args(argv)
    saved = vendor_repo_presets(arguments.ledger, REPO_PRESETS)
    saved += vendor_preset_files(
        arguments.ledger,
        {
            PRESET: FROZEN_NUCLEIC_ACIDS,
            "bacteria": FROZEN_BACTERIA,
            "base": FROZEN_BASE,
            "cell_culture": FROZEN_CELL_CULTURE,
            "proteins": FROZEN_PROTEINS,
        },
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
