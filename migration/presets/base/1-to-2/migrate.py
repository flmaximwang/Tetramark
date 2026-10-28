"""base 1 -> 2: status sets ``tube``/``cryo``/``animal``, ``lot`` uses ``tube``.

Base preset version 2 (spec 4, ticket 25) carries the status vocabulary that
used to live in ``ledger.yaml``:

* the top-level ``status_sets`` key defines ``tube``, ``cryo`` and ``animal``
  (design §4), with ``in_stock`` and the allowed ``to:`` transitions;
* the stock kind ``lot`` names ``statuses: tube``.

No table changes: the preset content only feeds the generated views and
``dolt_tests``, and every stock kind's allowed statuses moved from the ledger
config into the preset.  The step therefore

* places the repository preset copies the ledger lists (files are not Dolt
  content — the spec-4 loader refuses a stock kind without ``statuses:``, so
  the copy has to be current before the ledger can be opened), and puts the
  **frozen base v2** shipped next to this file over its own copy (the
  repository's ``presets/`` only holds the current base version),
* re-creates the generated views from the ledger's own (now v2) schema,
* bumps ``preset_versions.base`` to 2 and the fingerprint; the shared runner
  regenerates the derived ``dolt_tests`` from the same schema in that commit.

Run it by hand, **after** ``migration/spec/3-to-4``::

    uv run migration/presets/base/1-to-2/migrate.py --ledger PATH [--branch B] [--dry-run]

A ledger that lists other presets whose copies are still pre-wave-10 (e.g.
molbio) cannot be opened by the spec-4 loader at all; copy every changed preset
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
from tetramark.schema.model import SPEC_VERSION, EffectiveSchema

#: The repository root: this file is ``migration/presets/base/1-to-2/``.
REPO_ROOT = Path(__file__).resolve().parents[4]
REPO_PRESETS = REPO_ROOT / "presets"

PRESET = "base"
TO_VERSION = 2

#: The frozen base v2 this step installs over the repository copy: after a
#: later release ``presets/base.yaml`` is a newer version, so the historical
#: step must carry the content whose counter it bumps.
FROZEN_BASE = Path(__file__).with_name("base-2.yaml")


def _require_ready(ledger: Ledger) -> None:
    """The ledger must be spec 4 and carry the v2 base copy this step bumps to."""
    raw = ledger.meta.get(META_SPEC_VERSION)
    try:
        current = int(str(raw).strip())
    except (TypeError, ValueError):
        current = 0
    if current != SPEC_VERSION:
        raise LedgerError(
            f"refusing to migrate base 1 -> 2: ledger {ledger.name!r} is at "
            f"spec_version {current}, but status sets are spec {SPEC_VERSION}. "
            "Run migration/spec/3-to-4/migrate.py first; nothing was written."
        )
    version = ledger.schema.preset_versions.get(PRESET)
    if version != TO_VERSION:
        raise LedgerError(
            f"refusing to migrate base 1 -> 2: the ledger's own base copy is "
            f"version {version}, expected {TO_VERSION}. Copy this checkout's "
            "presets/base.yaml into <ledger>/presets/base.yaml first (files are "
            "not Dolt content; migration/spec/3-to-4/README.md explains the "
            "upgrade order). Nothing was written."
        )


def plan(ledger: Ledger) -> list[str]:
    """Refuse an unprepared ledger, else regenerate the derived views."""
    _require_ready(ledger)
    # No table changes: the preset only changes the status sets that feed the
    # generated views/rules.  Recreating every view makes the step's commit the
    # one that installs the spec-4 view shape for this preset; the runner adds
    # the counter/fingerprint bump and the regenerated dolt_tests.
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
        "place the wave-10 base preset copy (status sets tube/cryo/animal, lot "
        "uses tube), recreate the generated views and bump preset_versions.base"
    ),
)


def main(argv: list[str] | None = None) -> None:
    """Place the preset copies, then run the shared one-commit step.

    Every other preset the ledger lists is placed from the repository (the
    spec-4 loader needs current copies); base itself comes from the frozen v2
    file, because that is the version this counter bumps to.
    """
    arguments = preset_args(argv)
    saved = vendor_repo_presets(arguments.ledger, REPO_PRESETS)
    saved += vendor_preset_files(arguments.ledger, {PRESET: FROZEN_BASE})
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
