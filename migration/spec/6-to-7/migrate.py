"""spec 6 -> 7: ``aliquot_event.note`` becomes ``message``, ``purpose`` is retired.

Spec 7 (ticket ``Tetramark-fga.1``) settles the naming mismatch left by spec 6:
the CLI's ``-m/--message`` value was stored in ``aliquot_event.note``, while
``purpose`` was never written by ``use``/``move``.  One column now says what it
holds:

* ``aliquot_event.note`` is **renamed** to ``message`` (``RENAME COLUMN``, not
  copy-and-drop, so the column keeps its position: ``..., forced, actor,
  message, at`` — exactly the spec-7 DDL and ``tests/data/golden/ddl_full.sql``);
* the unused ``purpose`` column is **dropped** after its non-empty values are
  folded into ``message``, so no hand-written purpose text is lost.

Fold rule (one ``UPDATE`` after the rename; "empty" means ``NULL`` or
``TRIM(x) = ''``; values are copied verbatim, never trimmed):

===============  ===================================================
purpose          resulting message
===============  ===================================================
empty            unchanged
message empty    ``message = purpose``
both non-empty   ``message = CONCAT(purpose, '; ', message)``
===============  ===================================================

The step prints ``purpose folded into message: N row(s)`` in a dry run and in
an apply alike (``N`` counts rows whose ``purpose`` is non-empty), so the
operator sees the fold before it is written.

The column matrix is checked through ``information_schema`` like the spec
3 -> 4 step's ``_column_exists`` — this step also runs against ledgers that were
patched by hand:

====================  =====================================================
shape                 step
====================  =====================================================
N, no M, P            rename; fold; drop purpose
N, no M, no P         rename
M, no N, P            fold; drop purpose
M, no N, no P         ``MODIFY COLUMN message MEDIUMTEXT AFTER actor``
N and M               **refuse** (ambiguous), nothing written, no tag
neither N nor M       **refuse** (damaged), nothing written, no tag
====================  =====================================================

The step is the only migration with a step-local ``--apply`` flag; it is
**modelled on the spec 3 -> 4 wrapper**: without ``--apply`` it is a dry run
(``--dry-run`` is accepted and identical), with ``--apply`` it writes exactly
one commit, ``migrate(ledger): spec 6 -> 7``, after the restore tag
``pre-migrate-spec-6``.  The shared runner in :mod:`tetramark.migrate` is
unchanged.

Run it by hand::

    uv run migration/spec/6-to-7/migrate.py --ledger PATH [--branch B] [--apply]

``--apply`` is the only way to write; the plain command is a dry run.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from tetramark.core.ledger import Ledger
from tetramark.errors import LedgerError
from tetramark.migrate import MigrationError, MigrationStep, run
from tetramark.schema.loader import load_ledger_schema
from tetramark.schema.model import EffectiveSchema

#: The one line both a dry run and an apply print before the plan.
FOLD_LINE = "purpose folded into message: {count} row(s)"


def _column_exists(ledger: Ledger, table: str, column: str) -> bool:
    """Whether ``table`` has ``column`` in the live database (spec 3 -> 4 helper)."""
    rows = ledger.driver.query(
        "SELECT `column_name` AS `name` FROM `information_schema`.`columns` "
        "WHERE `table_schema` = DATABASE() AND `table_name` = %s "
        "AND `column_name` = %s",
        (table, column),
    )
    return bool(rows)


def _fold_count(ledger: Ledger) -> int:
    """Rows whose ``purpose`` is non-empty (the rows the fold rewrites)."""
    rows = ledger.driver.query(
        "SELECT COUNT(*) AS `n` FROM `aliquot_event` "
        "WHERE `purpose` IS NOT NULL AND TRIM(`purpose`) <> ''"
    )
    return int(rows[0]["n"]) if rows else 0


def _refuse(reason: str) -> LedgerError:
    return LedgerError(
        f"refusing to migrate spec 6 -> 7: {reason}. Nothing was written."
    )


def plan(ledger: Ledger) -> list[str]:
    """Refuse an ambiguous/damaged shape, else rename, fold, drop, normalise."""
    has_note = _column_exists(ledger, "aliquot_event", "note")
    has_message = _column_exists(ledger, "aliquot_event", "message")
    has_purpose = _column_exists(ledger, "aliquot_event", "purpose")

    if has_note and has_message:
        raise _refuse(
            "aliquot_event has both `note` and `message`, so it is ambiguous "
            "which one holds the event text"
        )
    if not has_note and not has_message:
        raise _refuse(
            "aliquot_event has neither `note` nor `message`, so the event text "
            "column is missing"
        )

    folded = _fold_count(ledger) if has_purpose else 0
    print(FOLD_LINE.format(count=folded))

    statements: list[str] = []
    if has_note:
        # Rename, never copy-and-drop: the column keeps its position, so the
        # resulting order is a fresh spec-7 ledger's and ddl_full.sql's.
        statements.append(
            "ALTER TABLE `aliquot_event` RENAME COLUMN `note` TO `message`"
        )
    if has_purpose:
        statements.append(
            "UPDATE `aliquot_event` SET `message` = CASE "
            "WHEN `message` IS NULL OR TRIM(`message`) = '' THEN `purpose` "
            "WHEN `purpose` IS NULL OR TRIM(`purpose`) = '' THEN `message` "
            "ELSE CONCAT(`purpose`, '; ', `message`) END "
            "WHERE `purpose` IS NOT NULL AND TRIM(`purpose`) <> ''"
        )
        statements.append("ALTER TABLE `aliquot_event` DROP COLUMN `purpose`")
    if not has_note and not has_purpose:
        # The column is already there, but at whatever position a hand patch
        # left it; normalise it and give the runner its >= 1 statement.
        statements.append(
            "ALTER TABLE `aliquot_event` MODIFY COLUMN `message` MEDIUMTEXT "
            "AFTER `actor`"
        )
    return statements


def target_schema(ledger: Ledger) -> EffectiveSchema:
    """The spec-7 schema the ledger opens with.

    This step changes no preset, so the ledger's own schema is the target; the
    module's ``SPEC_VERSION`` (7) is part of its fingerprint, so the runner
    records a fresh fingerprint and regenerates the derived ``dolt_tests`` in
    the same commit.

    It is loaded **without** the migration-only ``allow_legacy_statuses`` mode
    the runner uses to open the old shape (``open_target``): that mode injects
    the spec-3 default status set into a status-free preset, which would record
    a legacy fingerprint for a perfectly normal spec-6 ledger.  A spec-6 ledger
    loads its own presets normally, so the reopened schema is the target.
    """
    _config, schema = load_ledger_schema(ledger.path)
    return schema


def reopened_schema(ledger: Ledger) -> EffectiveSchema:
    """The schema the ledger opens with after the step, from its own files."""
    _config, schema = load_ledger_schema(ledger.path)
    return schema


step = MigrationStep(
    kind="spec",
    from_version=6,
    plan=plan,
    schema=target_schema,
    reopened_schema=reopened_schema,
    description=(
        "rename aliquot_event.note to message, fold the non-empty purpose "
        "values into message, drop purpose, and regenerate the derived "
        "dolt_tests"
    ),
)


# ---------------------------------------------------------------------------
# The step-local wrapper: --apply writes, everything else is a dry run
# ---------------------------------------------------------------------------


class _Arguments(argparse.Namespace):
    ledger: str
    branch: str | None
    apply: bool
    dry_run: bool


def _parse_args(argv: Sequence[str]) -> _Arguments:
    parser = argparse.ArgumentParser(
        add_help=False,
        description="spec 6 -> 7: aliquot_event.message replaces note/purpose",
    )
    parser.add_argument("--ledger", required=True, metavar="PATH")
    parser.add_argument("--branch", metavar="B", default=None)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--apply",
        action="store_true",
        help="write the step: one commit and the restore tag (default: dry run)",
    )
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="print the plan and write nothing (the default; accepted for "
        "symmetry with the other steps)",
    )
    args, _unknown = parser.parse_known_args(argv)
    return args  # type: ignore[return-value]


def main(argv: Sequence[str] | None = None) -> None:
    """``--ledger/--branch/--apply/--dry-run`` entry point (dry run by default)."""
    arguments = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        run(
            step,
            ledger_path=Path(arguments.ledger).expanduser(),
            branch=arguments.branch,
            dry_run=not arguments.apply,
        )
    except MigrationError:
        # The failure report (last good commit + reset command) was printed.
        raise SystemExit(1)
    except LedgerError as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(error.exit_code)


if __name__ == "__main__":
    main()
