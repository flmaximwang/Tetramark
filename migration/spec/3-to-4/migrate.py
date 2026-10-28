"""spec 3 -> 4: per-stock-kind status sets, forced events, in-stock views.

Spec 4 (ticket 25) settles git-bug ``0a9acec``: the aliquot status vocabulary
stops belonging to the ledger and belongs to each **stock kind** instead.  Four
generated-shape changes follow:

* ``ledger.yaml`` loses ``statuses:`` (the key also disappears from the
  ``ledger_meta.ledger_yaml`` copy).  The step only removes it when it still
  holds the spec-3 default ``[pending, stored, missing, consumed]``: any other
  list is a real vocabulary that has to become a ``status_set`` in the ledger's
  ``kinds/`` first, so the step refuses and the README explains the conversion.
* the global ``aliquot.status`` CHECK is dropped (a status now belongs to the
  stock kind's set, which a CHECK cannot express);
* ``aliquot_event`` gains ``forced BOOLEAN NOT NULL DEFAULT FALSE``: an
  ``--force`` status change records ``forced = TRUE`` and is accepted by
  ``tm check``;
* every generated view is recreated in the spec-4 shape: the stock views report
  ``aliquots_in_stock``/``aliquots_total``/``quantity_in_stock`` + ``unit`` and
  a three-value ``status`` (``active``/``depleted``/``empty``) derived from the
  schema's in-stock statuses, and ``v_box_slots`` shows the occupant stock's
  summary instead of the raw five-value aliquot status.

It also **refuses** — before writing anything — when an existing aliquot's
status is not in its stock kind's set (:func:`out_of_set_aliquots` lists them):
those rows would fail the generated ``core_aliquot_status_known`` test, and
guessing a status would lose data.

The step declares its target schema (``schema=``): the shared runner records
that schema's fingerprint and regenerates the derived ``dolt_tests`` in the same
commit, so the new status rules are installed with the shape they check.

A spec-3 ledger still has ``statuses:`` in ``ledger.yaml``, and spec 4's loader
refuses to open such a config (that refusal is the point: it names this step).
The step therefore strips the default list from the file for the duration of
the run — validating it first, and restoring the bytes if the run refuses or is
a dry run.  On a real apply the stripped file stays, and the plan records the
same YAML text in ``ledger_meta.ledger_yaml``.

A spec-3 ledger also has pre-spec-4 preset copies; this step cannot load those
(a stock kind needs ``statuses:`` to load).  Replace ``<ledger>/presets/`` with
this checkout's wave-10 copies **before** running it (files are not Dolt
content; the step README lists the copy command).  The per-preset counters are
bumped by the matching ``migration/presets/<name>/N-to-N+1`` steps.

Run it by hand::

    uv run migration/spec/3-to-4/migrate.py --ledger PATH [--branch B] [--dry-run]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

import yaml

from tetramark.core.ledger import LEDGER_FILE, META_LEDGER_YAML, Ledger
from tetramark.driver.base import literal
from tetramark.errors import LedgerError
from tetramark.migrate import (
    MigrationError,
    MigrationStep,
    run,
)
from tetramark.schema.ddl import view_statements
from tetramark.schema.loader import load_ledger_schema
from tetramark.schema.model import EffectiveSchema

#: The status list every spec-3 ledger was initialised with.  The step removes
#: ``statuses:`` only when the ledger still holds exactly this list; anything
#: else is a real vocabulary and must be converted by hand (see the README).
DEFAULT_STATUSES: tuple[str, ...] = ("pending", "stored", "missing", "consumed")


def _statuses_of(payload: object) -> list[str] | None:
    """The ``statuses`` list of a ``ledger.yaml`` payload, when present."""
    if not isinstance(payload, dict) or "statuses" not in payload:
        return None
    value = payload["statuses"]
    if not isinstance(value, list):
        return [str(value)]
    return [str(item) for item in value]


def _custom_list_error(found: list[str]) -> LedgerError:
    return LedgerError(
        "refusing to migrate spec 3 -> 4: this ledger's statuses list is not "
        "the spec-3 default, so it is a real vocabulary and cannot be "
        "discarded:\n"
        f"  statuses: [{', '.join(found)}]\n"
        "Turn it into a status_set in the ledger's kinds/ folder first: declare "
        "a top-level 'status_sets:' mapping whose statuses are exactly this "
        "list (each with 'in_stock:' and, optionally, 'to:'), give every stock "
        "kind that used the ledger-wide list 'statuses: <set name>', remove "
        "'statuses:' from ledger.yaml, then retry. "
        "migration/spec/3-to-4/README.md explains the conversion; nothing was "
        "written."
    )


def out_of_set_aliquots(ledger: Ledger) -> list[str]:
    """Aliquots whose status is not in their stock kind's status set.

    Returns ``"<aliquot id> (<stock kind>): <status>"`` lines, sorted.  A stock
    kind that the loaded schema does not know counts as a mismatch too: the
    generated status rule would flag those rows.
    """
    rows = ledger.driver.query(
        "SELECT a.`id` AS aliquot_id, a.`status` AS status, "
        "s.`kind` AS stock_kind "
        "FROM `aliquot` a JOIN `stock` s ON s.`id` = a.`stock_id` "
        "ORDER BY a.`id`"
    )
    out: list[str] = []
    for row in rows:
        kind_name = str(row["stock_kind"])
        if not ledger.schema.has_kind(kind_name):
            out.append(
                f"{row['aliquot_id']} (unknown stock kind {kind_name!r}): "
                f"{row['status']}"
            )
            continue
        if str(row["status"]) not in ledger.schema.statuses_for(kind_name):
            out.append(f"{row['aliquot_id']} ({kind_name}): {row['status']}")
    return out


def _column_exists(ledger: Ledger, table: str, column: str) -> bool:
    rows = ledger.driver.query(
        "SELECT `column_name` AS `name` FROM `information_schema`.`columns` "
        "WHERE `table_schema` = DATABASE() AND `table_name` = %s "
        "AND `column_name` = %s",
        (table, column),
    )
    return bool(rows)


def _check_constraints(ledger: Ledger, table: str) -> list[str]:
    rows = ledger.driver.query(
        "SELECT tc.`constraint_name` AS `name` "
        "FROM `information_schema`.`table_constraints` tc "
        "JOIN `information_schema`.`check_constraints` cc "
        "  ON cc.`constraint_schema` = tc.`constraint_schema` "
        " AND cc.`constraint_name` = tc.`constraint_name` "
        "WHERE tc.`table_schema` = DATABASE() AND tc.`table_name` = %s "
        "AND tc.`constraint_type` = 'CHECK' ORDER BY tc.`constraint_name`",
        (table,),
    )
    return [str(row["name"]) for row in rows]


def _stripped_ledger_yaml(ledger: Ledger) -> str | None:
    """The ``ledger_meta.ledger_yaml`` copy without ``statuses``, or ``None``.

    A custom list here is refused, exactly like the file copy: the meta row must
    describe the same configuration the file does.
    """
    raw = ledger.meta.get(META_LEDGER_YAML)
    if not raw:
        return None
    try:
        payload = yaml.safe_load(raw)
    except yaml.YAMLError as error:  # pragma: no cover - defensive
        raise LedgerError(
            f"ledger_meta.{META_LEDGER_YAML} is not valid YAML: {error}"
        ) from error
    found = _statuses_of(payload)
    if "statuses" not in (payload or {}):
        return None
    assert found is not None
    if tuple(found) != DEFAULT_STATUSES:
        raise _custom_list_error(found)
    stripped = dict(payload)
    stripped.pop("statuses")
    return yaml.safe_dump(stripped, sort_keys=False, allow_unicode=True)


def plan(ledger: Ledger) -> list[str]:
    """Refuse a custom list or an out-of-set status, else change the shape."""
    bad = out_of_set_aliquots(ledger)
    if bad:
        raise LedgerError(
            "refusing to migrate spec 3 -> 4: these aliquots have a status that "
            "is not in their stock kind's status set (spec 4 requires one), so "
            "the generated status rule would fail immediately:\n  "
            + "\n  ".join(bad)
            + "\nSet each one to a status of its stock kind's set (or extend "
            "the set in <ledger>/presets/ or kinds/), then retry. Nothing was "
            "written."
        )

    statements: list[str] = []
    if not _column_exists(ledger, "aliquot_event", "forced"):
        statements.append(
            "ALTER TABLE `aliquot_event` ADD COLUMN `forced` BOOLEAN NOT NULL "
            "DEFAULT FALSE"
        )
    # The spec-4 schema declares no CHECK on ``aliquot``: the vocabulary moved
    # to the stock kind's status set, which the generated tests enforce.
    for constraint in _check_constraints(ledger, "aliquot"):
        statements.append(
            f"ALTER TABLE `aliquot` DROP CHECK `{constraint}`"
        )

    views = view_statements(ledger.schema)
    statements.extend(f"DROP VIEW IF EXISTS `{statement.name}`" for statement in views)
    statements.extend(statement.sql for statement in views)

    stripped = _stripped_ledger_yaml(ledger)
    if stripped is not None:
        statements.append(
            "UPDATE `ledger_meta` SET `value` = " + literal(stripped)
            + " WHERE `key` = " + literal(META_LEDGER_YAML)
        )
    return statements


def target_schema(ledger: Ledger) -> EffectiveSchema:
    """The spec-4 schema the ledger opens with (the YAML itself unchanged).

    The opened ledger is already loaded by the spec-4 tool, so its
    ``EffectiveSchema`` carries the spec-4 fingerprint (the module's
    ``SPEC_VERSION`` is part of it).  The runner records it and installs the
    regenerated ``dolt_tests`` — including the two status rules — in the same
    commit.
    """
    return ledger.schema


def reopened_schema(ledger: Ledger) -> EffectiveSchema:
    """The schema the ledger opens with after the step, from its own files."""
    _config, schema = load_ledger_schema(ledger.path)
    return schema


# ---------------------------------------------------------------------------
# The legacy ledger.yaml (spec-3 shape) wrapper
# ---------------------------------------------------------------------------


class _Arguments(argparse.Namespace):
    ledger: str
    branch: str | None
    dry_run: bool


def _parse_args(argv: Sequence[str]) -> _Arguments:
    parser = argparse.ArgumentParser(
        add_help=False,
        description="spec 3 -> 4: status sets per stock kind, forced events, "
        "in-stock views",
    )
    parser.add_argument("--ledger", required=True, metavar="PATH")
    parser.add_argument("--branch", metavar="B", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args, _unknown = parser.parse_known_args(argv)
    return args  # type: ignore[return-value]


def _prepare_config(path: Path) -> tuple[Path, bytes] | None:
    """Strip a default ``statuses:`` list from ``ledger.yaml`` for the run.

    Returns the file path and its previous bytes so the caller can restore them
    on a refusal or a dry run; ``None`` when the file needs no change.  A custom
    list is refused here, before anything is written.
    """
    if not path.is_dir():
        raise LedgerError(
            f"ledger path {path} is not a directory; pass the ledger directory "
            "(the Dolt database), not ledger.yaml"
        )
    config_path = path / LEDGER_FILE
    if not config_path.is_file():
        raise LedgerError(
            f"{config_path} is not a Tetramark ledger.yaml; pass the ledger "
            "directory (the Dolt database)"
        )
    previous = config_path.read_bytes()
    try:
        payload = yaml.safe_load(previous.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as error:
        raise LedgerError(f"cannot read {config_path}: {error}") from error
    found = _statuses_of(payload)
    if found is None:
        return None
    if tuple(found) != DEFAULT_STATUSES:
        raise _custom_list_error(found)
    stripped = dict(payload)
    stripped.pop("statuses")
    config_path.write_text(
        yaml.safe_dump(stripped, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return config_path, previous


def _restore(path: Path, previous: bytes) -> None:
    path.write_bytes(previous)


step = MigrationStep(
    kind="spec",
    from_version=3,
    plan=plan,
    schema=target_schema,
    reopened_schema=reopened_schema,
    description=(
        "drop the global aliquot.status CHECK, add aliquot_event.forced, "
        "recreate the spec-4 in-stock views, remove statuses: from "
        "ledger.yaml/ledger_meta, and regenerate the derived dolt_tests"
    ),
)


def main(argv: Sequence[str] | None = None) -> None:
    """``--ledger/--branch/--dry-run`` entry point with the legacy-config wrapper."""
    arguments = _parse_args(sys.argv[1:] if argv is None else argv)
    target = Path(arguments.ledger).expanduser()
    restore: tuple[Path, bytes] | None = None
    try:
        restore = _prepare_config(target)
    except LedgerError as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(error.exit_code)
    try:
        run(
            step,
            ledger_path=target,
            branch=arguments.branch,
            dry_run=arguments.dry_run,
        )
    except MigrationError:
        # The commit may exist and DDL may be partial: keep the stripped
        # ledger.yaml, which is the spec-4 shape the retry needs.
        raise SystemExit(1)
    except LedgerError as error:
        if restore is not None:
            _restore(*restore)
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(error.exit_code)
    else:
        if restore is not None and arguments.dry_run:
            _restore(*restore)


if __name__ == "__main__":
    main()
