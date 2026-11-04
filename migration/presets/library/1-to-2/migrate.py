"""library 1 -> 2: the ``copy`` stock kind names the ``tube`` status set.

Library preset version 2 (spec 4, ticket 25) names the status set its stock
kind uses; the sets themselves live in ``base`` (which this preset includes):

* ``base`` v2 defines the top-level ``status_sets`` ``tube``/``cryo``/
  ``animal`` with ``in_stock`` and the allowed ``to:`` transitions;
* the stock kind ``copy`` names ``statuses: tube``.

No table changes: the preset content only feeds the generated views and
``dolt_tests``, and every stock kind's allowed statuses moved from the ledger
config into the preset.  The step therefore

* places the repository preset copies the ledger lists (files are not Dolt
  content — the spec-5 loader refuses a stock kind without ``statuses:``, so
  the copy has to be current before the ledger can be opened), then puts the
  **frozen base v3** shipped next to this file over its own copy: the
  repository's ``presets/base.yaml`` is v4 since spec 5's ``grid96`` ->
  ``grid8x12`` rename, and this wave-10 step must keep installing the base
  content its era reads.  Ticket rq9 bumps ``proteins`` and ``nucleic_acids``
  to v2, so the step also keeps their **frozen v1 copies**.
  ``migration/README.md`` requires the retrofit,
* re-creates the generated views from the ledger's own (now v2) schema,
* bumps ``preset_versions.library`` to 2 and the fingerprint; the shared runner
  regenerates the derived ``dolt_tests`` from the same schema in that commit.

Run it by hand under the wave-11 tool **after** ``migration/spec/4-to-5`` (the
ledger formats at spec 5 first) and **before**
``migration/presets/base/3-to-4`` (that step renames the base v3 copy this step
keeps)::

    uv run migration/presets/library/1-to-2/migrate.py --ledger PATH [--branch B] [--dry-run]

A ledger that lists other presets whose copies are still pre-wave-10 (e.g.
molbio) cannot be opened by the spec-5 loader at all; copy every changed preset
copy first (the step's own placement helper does that for every bundled preset
the ledger lists, and the matching preset steps bump their counters).
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
from tetramark.schema.loader import load_ledger_schema
from tetramark.schema.model import EffectiveSchema

#: The spec format this historical preset step upgrades to.  Pinned: a
#: later ``SPEC_VERSION`` bump must not retarget a frozen preset step.  A ledger
#: on a newer format still runs it only while the preset counters/kinds are
#: still those of this historical step; the manual spec/5-to-6 upgrade runs
#: after the pending preset steps.
REQUIRED_SPEC = 5

#: The repository root: this file is ``migration/presets/library/1-to-2/``.
REPO_ROOT = Path(__file__).resolve().parents[4]
REPO_PRESETS = REPO_ROOT / "presets"

#: The frozen proteins v1 and nucleic_acids v1 this step keeps over the
#: repository copies: ticket rq9 bumps both parts to v2, and an earlier step
#: that places the whole current closure must not install that content behind
#: the parts' own counters (``migration/presets/proteins/1-to-2`` and
#: ``migration/presets/nucleic_acids/1-to-2`` own the bumps).
FROZEN_PROTEINS = Path(__file__).with_name("proteins-1.yaml")
FROZEN_NUCLEIC_ACIDS = Path(__file__).with_name("nucleic_acids-1.yaml")

PRESET = "library"
TO_VERSION = 2

#: The frozen base v3 this step installs over the repository copy: spec 5
#: (ticket 30) bumps the repository's ``presets/base.yaml`` to v4 (``grid96``
#: renamed to ``grid8x12``), and this wave-10 step must keep installing the base
#: content its era reads.  ``migration/README.md`` requires the retrofit.
FROZEN_BASE = Path(__file__).with_name("base-3.yaml")


def _require_ready(ledger: Ledger) -> None:
    """The ledger must be spec 5 and carry the v2 base copy this step bumps to."""
    raw = ledger.meta.get(META_SPEC_VERSION)
    try:
        current = int(str(raw).strip())
    except (TypeError, ValueError):
        current = 0
    if current < REQUIRED_SPEC:
        raise LedgerError(
            f"refusing to migrate library 1 -> 2: ledger {ledger.name!r} is at "
            f"spec_version {current}, but status sets are spec {REQUIRED_SPEC}. "
            "Run migration/spec/4-to-5/migrate.py first; nothing was written."
        )
    version = ledger.schema.preset_versions.get(PRESET)
    if version != TO_VERSION:
        raise LedgerError(
            f"refusing to migrate library 1 -> 2: the ledger's own library copy is "
            f"version {version}, expected {TO_VERSION}. This step places the "
            "repository library copy (and the frozen base-3.yaml) over "
            "<ledger>/presets/ first (files are not Dolt content), and it must "
            "run before migration/presets/base/3-to-4. Nothing was written."
        )


def plan(ledger: Ledger) -> list[str]:
    """Refuse an unprepared ledger, else regenerate the derived views."""
    _require_ready(ledger)
    # No table changes: the preset only changes the status sets that feed the
    # generated views/rules.  Recreating every view makes the step's commit the
    # one that installs the current (spec 5) view shape for this preset; the
    # runner adds the counter/fingerprint bump and the regenerated dolt_tests.
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
        "place the wave-10 library preset copy (copy names the base tube "
        "status set), recreate the generated views and bump "
        "preset_versions.library"
    ),
)


def main(argv: list[str] | None = None) -> None:
    """Place the preset copies, then run the shared one-commit step.

    The library copy comes from the repository (it is still v2); base comes
    from the frozen v3 file, because the repository copy is v4 since spec 5 and
    this wave-10 step must not install the newer content.
    """
    arguments = preset_args(argv)
    saved = vendor_repo_presets(arguments.ledger, REPO_PRESETS)
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
