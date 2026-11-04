"""proteins 1 -> 2: IUPAC patterns on the shared sequence fields.

Proteins preset version 2 (spec 5, ticket rq9) turns the sequence fields it
owns into validated IUPAC sequences:

* the ``sequence_fields`` mixin's ``sequence`` -- loaded by
  ``protein_construct`` here and by ``plasmid`` in ``nucleic_acids`` -- gets
  ``pattern: "[ACGTURYKMSWBDHVN]+"`` (IUPAC nucleotide codes);
* ``protein_construct.mature_sequence`` gets
  ``pattern: "[ACDEFGHIKLMNPQRSTVWYXBZUO*]+"`` (IUPAC amino-acid codes plus the
  ambiguity and stop symbols).

Patterns are validation-only: the app checks them after sequence normalisation
(``re.fullmatch``) and the generated ``dolt_tests`` check the same pattern, but
no table or column changes.  The step therefore

* places the whole current preset closure the ledger lists **before** the
  runner opens the ledger (files are not Dolt content), then puts the **frozen
  copies** shipped next to this file over their copies: **proteins v2** (the
  target content this counter bumps to; a later repository bump must not change
  it) and **nucleic_acids v1** (the repository copy is v2 now, but a ledger
  stays at ``nucleic_acids`` 1 until
  ``migration/presets/nucleic_acids/1-to-2`` bumps it; placing v2 here would
  install a newer preset behind its counter),
* re-creates the generated views from the ledger's own (now v2) schema, so the
  commit has statements of its own -- it changes no DDL,
* bumps ``preset_versions.proteins`` to 2 and the fingerprint; the shared
  runner regenerates the derived ``dolt_tests`` from the same schema in that
  commit, so ``tm check`` now reports rows that do not match the patterns.

Run it by hand under the tool of this release, **after**
``migration/spec/4-to-5`` and ``migration/presets/base/3-to-4`` (this step
places the repository's base v4 copy and refuses a ledger whose base counter
is not 4), and **before** ``migration/presets/nucleic_acids/1-to-2`` (which
expects proteins already at 2)::

    uv run migration/presets/proteins/1-to-2/migrate.py --ledger PATH [--branch B] [--dry-run]

``nucleic_acids`` keeps its counter at 1 here: its own step bumps it.
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

#: The repository root: this file is ``migration/presets/proteins/1-to-2/``.
REPO_ROOT = Path(__file__).resolve().parents[4]
REPO_PRESETS = REPO_ROOT / "presets"

PRESET = "proteins"
TO_VERSION = 2

#: The base counter this step needs: it installs the repository's base v4 copy,
#: so ``migration/presets/base/3-to-4`` must have run first.
BASE_VERSION = 4

#: The nucleotide sequences of ``sequence_fields`` (used by ``protein_construct``
#: here and ``plasmid`` in ``nucleic_acids``) and the amino-acid
#: ``mature_sequence``, exactly as the v2 preset states them.
NUCLEOTIDE_PATTERN = "[ACGTURYKMSWBDHVN]+"
AMINO_ACID_PATTERN = "[ACDEFGHIKLMNPQRSTVWYXBZUO*]+"

#: The frozen proteins v2 content this step installs (the repository's
#: ``presets/`` only holds the current version, so a later bump must not change
#: what this historical step places).
FROZEN_PROTEINS = Path(__file__).with_name("proteins-2.yaml")

#: The frozen nucleic_acids v1 this step keeps over the repository copy: that
#: part's counter is still 1 here, and its own step owns the bump to 2.
FROZEN_NUCLEIC_ACIDS = Path(__file__).with_name("nucleic_acids-1.yaml")


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
        "refusing to migrate proteins 1 -> 2: " + message + " nothing was written."
    )


def _require_ready(ledger: Ledger) -> None:
    """The ledger must be at least spec 5, base 4, and load the v2 target content."""
    raw = ledger.meta.get(META_SPEC_VERSION)
    try:
        current = int(str(raw).strip())
    except (TypeError, ValueError):
        current = 0
    if current < REQUIRED_SPEC:
        raise LedgerError(
            f"refusing to migrate proteins 1 -> 2: ledger {ledger.name!r} is at "
            f"spec_version {current}, but the v2 preset needs spec "
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
    nucleic = recorded.get("nucleic_acids")
    if nucleic is not None and nucleic != 1:
        raise _refuse(
            f"the ledger already records nucleic_acids at version {nucleic!r}; "
            "the documented order bumps proteins before nucleic_acids "
            "(migration/presets/proteins/1-to-2, then "
            "migration/presets/nucleic_acids/1-to-2)."
        )
    loaded = ledger.schema.preset_versions
    if loaded.get("base") != BASE_VERSION:
        raise _refuse(
            f"the loaded base copy is version {loaded.get('base')!r}, expected "
            f"{BASE_VERSION}; the repository copy was not placed over "
            "<ledger>/presets/base.yaml."
        )
    if loaded.get(PRESET) != TO_VERSION:
        raise _refuse(
            f"the loaded proteins copy is version {loaded.get(PRESET)!r}, "
            f"expected {TO_VERSION}; the frozen proteins-2.yaml next to "
            "migrate.py was not placed over <ledger>/presets/proteins.yaml."
        )
    if "nucleic_acids" in loaded and loaded["nucleic_acids"] != 1:
        raise _refuse(
            "the loaded nucleic_acids copy is version "
            f"{loaded['nucleic_acids']!r}, expected 1; the frozen "
            "nucleic_acids-1.yaml was not placed over its copy."
        )

    construct = ledger.schema.kinds.get("protein_construct")
    if construct is None:
        raise _refuse(
            "the loaded schema has no protein_construct kind, so it is not the "
            "proteins preset this step upgrades."
        )
    for field_name, expected in (
        ("sequence", NUCLEOTIDE_PATTERN),
        ("mature_sequence", AMINO_ACID_PATTERN),
    ):
        field = construct.fields.get(field_name)
        if field is None or field.pattern != expected:
            raise _refuse(
                f"protein_construct.{field_name} does not carry the v2 pattern "
                f"{expected!r}, so the loaded copy is not the target content."
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
        "place the proteins v2 preset copy (IUPAC patterns on sequence_fields "
        "and mature_sequence, nucleic_acids kept at v1), recreate the generated "
        "views and bump preset_versions.proteins"
    ),
)


def main(argv: list[str] | None = None) -> None:
    """Place the preset copies, then run the shared one-commit step.

    Every preset the ledger lists is placed from the repository; proteins comes
    from the frozen v2 file (the version this counter bumps to) and
    nucleic_acids from the frozen v1 file (its own step owns the bump to 2).
    """
    arguments = preset_args(argv)
    saved = vendor_repo_presets(arguments.ledger, REPO_PRESETS)
    saved += vendor_preset_files(
        arguments.ledger,
        {
            PRESET: FROZEN_PROTEINS,
            "nucleic_acids": FROZEN_NUCLEIC_ACIDS,
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
