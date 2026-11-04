"""proteins 3 -> 4: a protein construct's ``sequence`` is amino acids, not DNA.

Proteins preset version 3 (ticket rq9) validated ``sequence_fields.sequence``
as a nucleotide alphabet.  That mixin is shared: ``plasmid`` (nucleic_acids)
really does hold a DNA sequence, but ``protein_construct`` is imported from the
legacy ``entity_constructs.sequence``, which is the construct's **full-length
amino-acid** sequence.  The nucleotide pattern was therefore a false positive
for every construct, and ``length_bp`` ("base pairs") computed a residue count.

Version 4 splits the two: ``plasmid`` keeps the nucleotide ``sequence_fields``
mixin, and ``protein_construct`` gets its own ``sequence`` with the amino-acid
pattern ``[ACDEFGHIKLMNPQRSTVWYXBZUO*]+`` plus ``length_aa``.  Nothing else
changes -- no kind, no other field, no rule.

The step therefore

* places the **frozen proteins v4** copy shipped next to this file over
  ``<ledger>/presets/proteins.yaml`` **before** the runner opens the ledger
  (files are not Dolt content; ``presets/`` only ever holds the current version,
  so a later bump must not change what this historical step installs).  Every
  other copy the ledger lists is already the spec-6 one this step targets, so
  only this part is placed,
* applies the diff plan with the explicit rename map from ``renames.yaml``:
  ``entity_protein_construct.length_bp`` -> ``length_aa``.  The column is a
  generated ``STORED`` column, so Dolt renames it in place -- no drop, no
  retype, no data movement; the pattern is validation metadata, so it changes
  no DDL.  A plan with any destructive step is refused before anything is
  written,
* bumps ``preset_versions.proteins`` to 4 and the fingerprint; the shared
  runner regenerates the derived ``dolt_tests`` from the same schema in that
  commit, so ``tm check`` now validates the construct's amino-acid alphabet and
  no longer reports the legacy constructs.

Run it by hand under the tool of this release, **after**
``migration/spec/5-to-6`` (the v4 preset belongs to the spec-6 closure)::

    uv run migration/presets/proteins/3-to-4/migrate.py --ledger PATH [--branch B] [--dry-run]

A spec-5 ledger at proteins 2 runs the older ``proteins/1-to-2`` step, the
manual ``proteins/2-to-3`` copy (frozen ``proteins-3.yaml``) and the spec-5 ->
6 upgrade first; this step then renames the column.  The frozen
``proteins-3.yaml`` next to ``../2-to-3/README.md`` is the content this step
moves a ledger from (the rollback copy).
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
)
from tetramark.schema.ddl import view_statements
from tetramark.schema.diff import build_plan
from tetramark.schema.loader import load_ledger_schema
from tetramark.schema.model import EffectiveSchema

#: The spec format this step runs under.  Proteins 3 only exists on spec 6
#: (the spec 5 -> 6 upgrade writes the counters of the spec-6 closure), so a
#: ledger still on spec 5 has to finish that upgrade first.
REQUIRED_SPEC = 6

PRESET = "proteins"
TO_VERSION = 4

#: The base counter of the spec-6 closure this step's target schema belongs to.
#: Pinned so the step cannot run against a mixed closure (its plan would then
#: carry changes that belong to another step).
BASE_VERSION = 5

#: The amino-acid alphabet a protein construct's ``sequence`` now carries.
AMINO_ACID_PATTERN = "[ACDEFGHIKLMNPQRSTVWYXBZUO*]+"

#: The explicit rename map: the construct's length column counts residues.
RENAMES = Path(__file__).with_name("renames.yaml")

#: The frozen proteins v4 content this step installs.
FROZEN_PROTEINS = Path(__file__).with_name("proteins-4.yaml")


def _refuse(message: str) -> LedgerError:
    return LedgerError(
        "refusing to migrate proteins 3 -> 4: " + message + " nothing was written."
    )


def _target_schema(ledger: Ledger) -> EffectiveSchema:
    """The ledger's own schema under the **fixed** status model.

    The shared migration runner opens the ledger with
    ``allow_legacy_statuses=True`` -- a step must be able to read the old shape
    it changes -- and that loader injects the legacy per-kind status sets.  This
    step runs on spec 6, where the fixed status set is part of the schema: under
    the injected model ``aliquot_status_check`` returns ``None`` and the plan
    would drop the ledger's ``ck_aliquot_status`` CHECK.  Reload the ledger's
    own copies without that allowance, so the plan and the fingerprint recorded
    in the commit describe the schema the ledger really opens with.
    """
    _config, schema = load_ledger_schema(ledger.path)
    return schema


def _require_ready(ledger: Ledger, schema: EffectiveSchema) -> None:
    """The ledger must be spec 6, carry the v4 copy and load the split mixin."""
    raw = ledger.meta.get(META_SPEC_VERSION)
    try:
        current = int(str(raw).strip())
    except (TypeError, ValueError):
        current = 0
    if current < REQUIRED_SPEC:
        raise LedgerError(
            f"refusing to migrate proteins 3 -> 4: ledger {ledger.name!r} is at "
            f"spec_version {current}, but the v4 preset belongs to spec "
            f"{REQUIRED_SPEC}. Run migration/spec/5-to-6/README.md first; "
            "nothing was written."
        )
    loaded = schema.preset_versions
    if loaded.get("base") != BASE_VERSION:
        raise _refuse(
            f"the loaded base copy is version {loaded.get('base')!r}, expected "
            f"{BASE_VERSION}; the v4 proteins preset belongs to the spec-6 "
            "closure. Replace the ledger's copies with this checkout's "
            "status-free ones first (migration/spec/5-to-6/README.md)."
        )
    version = loaded.get(PRESET)
    if version != TO_VERSION:
        raise _refuse(
            f"the loaded proteins copy is version {version!r}, expected "
            f"{TO_VERSION}; the frozen proteins-4.yaml next to migrate.py was "
            "not placed over <ledger>/presets/proteins.yaml."
        )

    construct = schema.kinds.get("protein_construct")
    if construct is None:
        raise _refuse(
            "the loaded schema has no protein_construct kind, so it is not the "
            "proteins preset this step upgrades."
        )
    sequence = construct.fields.get("sequence")
    if sequence is None or sequence.pattern != AMINO_ACID_PATTERN:
        got = None if sequence is None else sequence.pattern
        raise _refuse(
            f"protein_construct.sequence does not carry the v4 amino-acid "
            f"pattern {AMINO_ACID_PATTERN!r} (got {got!r}), so the loaded copy "
            "is not the target content."
        )
    if "length_aa" not in construct.fields:
        raise _refuse(
            "protein_construct has no length_aa field, so the loaded copy is "
            "not the v4 target content."
        )
    if "length_bp" in construct.fields:
        raise _refuse(
            "protein_construct still defines length_bp, so the loaded copy is "
            "not the v4 target content."
        )


def plan(ledger: Ledger) -> list[str]:
    """Refuse an unprepared ledger, else apply the rename plan."""
    schema = _target_schema(ledger)
    _require_ready(ledger, schema)
    renames = load_renames(RENAMES)
    schema_plan = build_plan(
        ledger.driver,
        schema,
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
            "refusing to migrate proteins 3 -> 4: the diff plan has destructive "
            "step(s) beyond the length_bp -> length_aa rename that would drop or "
            "change existing schema or data:\n"
            + "\n".join(lines)
            + "\nNothing was written. This step only renames the computed "
            "column; fix the ledger's local schema first."
        )
    statements = [statement.sql for statement in schema_plan.steps]
    if statements:
        return statements
    # Another step may already have renamed the column (the step is idempotent
    # like every preset step).  The counter bump then needs statements of its
    # own: recreate the generated views, like the content-only steps do.
    views = view_statements(schema)
    return [f"DROP VIEW IF EXISTS `{statement.name}`" for statement in views] + [
        statement.sql for statement in views
    ]


def reopened_schema(ledger: Ledger) -> EffectiveSchema:
    """The schema the ledger opens with from its own (placed) copy."""
    _config, schema = load_ledger_schema(ledger.path)
    return schema


step = MigrationStep(
    kind="preset",
    preset=PRESET,
    from_version=3,
    plan=plan,
    schema=_target_schema,
    reopened_schema=reopened_schema,
    description=(
        "place the proteins v4 preset copy (protein_construct.sequence is the "
        "amino-acid sequence and length_bp becomes length_aa), apply the rename "
        "plan from renames.yaml, and bump preset_versions.proteins"
    ),
)


def main(argv: list[str] | None = None) -> None:
    """Place the frozen proteins copy, then run the shared one-commit step."""
    arguments = preset_args(argv)
    saved = vendor_preset_files(arguments.ledger, {PRESET: FROZEN_PROTEINS})
    try:
        run(
            step,
            ledger_path=arguments.ledger,
            branch=arguments.branch,
            dry_run=arguments.dry_run,
        )
    except MigrationError:
        # A commit may exist and the DDL may be partial; the placed copy is the
        # target content a retry needs, so keep it.
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
