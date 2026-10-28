"""molbio 1 -> 2: kind renames, cell kinds, and ``glycerol_pct`` on the stock.

Molbio preset version 2 (release 0.2.0, ticket 22) applies one model rule — *all
aliquots of one stock are the same* — to the bundled biology preset:

* four kind renames, done by the diff planner through ``renames.yaml``:
  ``construct`` → ``protein_construct``, ``strain`` → ``bacterial_strain``,
  ``bacterial`` → ``bacterial_glycerol``, ``pellet`` → ``bacterial_pellet``;
* the bacterial tube property ``glycerol_pct`` becomes a **stock** field on
  ``bacterial_glycerol`` (``aliquot_bacterial`` disappears);
* new kinds: the ``cell_line`` entity (with a ``parental`` self-reference) and
  the ``cell_vial`` / ``cell_pellet`` stocks — additive tables and views;
* the ledger's own ``<ledger>/presets/`` copy is replaced with the repository's
  v2 preset and the counter ``preset_versions.molbio`` becomes 2.

Run it by hand from the repository root::

    uv run migration/presets/molbio/1-to-2/migrate.py --ledger PATH [--branch B] [--dry-run]

The shared runner (``src/tetramark/migrate.py``) provides the tag, the one
commit, the counter/fingerprint bump, and — because this step declares a target
``schema`` — the fingerprint of molbio 2 plus regenerated ``dolt_tests`` in the
same commit.  ``after_commit`` replaces the ledger's preset files (they are not
Dolt content) and ``reopened_schema`` makes the runner re-read the ledger from
that copy and verify its fingerprint before the success report.

Ordering note: the planner drops extra tables *before* it adds columns, and it
cannot see ``aliquot_bacterial`` once ``stock_bacterial`` was renamed (its name
is derived from the renamed kind).  The step therefore splices the
``glycerol_pct`` copy and the explicit extension-table drop directly after the
``ADD COLUMN stock_bacterial_glycerol.glycerol_pct`` step, so the data moves
after the target column exists and the source table is dropped last.
"""

from __future__ import annotations

from pathlib import Path

from tetramark.core.ledger import Ledger
from tetramark.errors import LedgerError
from tetramark.migrate import MigrationStep, load_renames, main
from tetramark.schema.ddl import table_name
from tetramark.schema.diff import build_plan
from tetramark.schema.loader import load_ledger_schema, load_schema
from tetramark.schema.model import EffectiveSchema

#: The repository root: this file is ``migration/presets/molbio/1-to-2/``.
REPO_ROOT = Path(__file__).resolve().parents[4]
REPO_PRESETS = REPO_ROOT / "presets"

#: ``presets/molbio.yaml`` version this step upgrades a ledger to.
TO_VERSION = 2

#: Old → new names live in ``renames.yaml`` (the planner does the renames).
RENAMES = load_renames(Path(__file__).with_name("renames.yaml"))

#: After the rename map, the old ``stock_bacterial`` is this table.
STOCK_TABLE = "stock_bacterial_glycerol"
#: The v1 bacterial tube extension: source of the moved values, then dropped.
EXTENSION_TABLE = "aliquot_bacterial"


#: Copy each stock's tube-level glycerol percentage onto the stock.  The
#: extension rows are keyed by *aliquot* id, so the stock id is
#: ``aliquot.stock_id`` — never ``stock_bacterial.id``.
COPY_GLYCEROL_SQL = (
    "UPDATE `stock_bacterial_glycerol` AS `s` "
    "JOIN `aliquot` AS `a` ON `a`.`stock_id` = `s`.`id` "
    "JOIN `aliquot_bacterial` AS `e` ON `e`.`id` = `a`.`id` "
    "SET `s`.`glycerol_pct` = `e`.`glycerol_pct` "
    "WHERE `e`.`glycerol_pct` IS NOT NULL"
)
DROP_EXTENSION_SQL = "DROP TABLE IF EXISTS `aliquot_bacterial`"

#: Ledger preset files this step vendors from the repository (v2 molbio; base
#: is unchanged at version 1 but is re-copied if the ledger's copy differs).
PRESET_FILES = ("molbio.yaml", "base.yaml")


def target_schema(ledger: Ledger) -> EffectiveSchema:
    """The molbio-2 schema of this repository, plus the ledger's local kinds/rules.

    ``open_target`` still opens the ledger with its old preset copy, so the
    migration must load the repository preset itself.  ``base_dir`` is the
    ledger directory, so a ``kinds_dir``/``rules_dir`` in ``ledger.yaml`` is
    included exactly as a later ``open`` will include it.  The loaded copy
    must really be version 2: the runner refuses to commit otherwise.
    """
    config = ledger.config.model_copy(
        update={
            "presets": [
                str(REPO_PRESETS / "base.yaml"),
                str(REPO_PRESETS / "molbio.yaml"),
            ]
        }
    )
    schema = load_schema(config, base_dir=ledger.path)
    version = schema.preset_versions.get("molbio")
    if version != TO_VERSION:
        raise LedgerError(
            f"refusing to migrate molbio 1 -> 2: the repository preset is "
            f"version {version}, expected {TO_VERSION}"
        )
    return schema


def reopened_schema(ledger: Ledger) -> EffectiveSchema:
    """The schema the ledger opens with from its **own** (just vendored) copy."""
    _config, schema = load_ledger_schema(ledger.path)
    return schema


def _table_exists(ledger: Ledger, table: str) -> bool:
    rows = ledger.driver.query(
        "SELECT `table_name` AS `name` FROM `information_schema`.`tables` "
        "WHERE `table_schema` = DATABASE() AND `table_name` = %s",
        (table,),
    )
    return bool(rows)


def old_kind_tables() -> tuple[str, ...]:
    """Every v1 kind table the rename map names.

    A ledger missing one is not a molbio 1 ledger (or the rename map has a
    typo), so the step refuses before the planner silently skips the rename.
    """
    return tuple(
        table_name(object_name, old_kind)
        for object_name, mapping in RENAMES.kind_maps()
        for old_kind in mapping
    )


def conflicting_stocks(ledger: Ledger) -> list[str]:
    """Stocks whose bacterial aliquots disagree on ``glycerol_pct``.

    One value per stock is the whole point of moving the field, so a conflict
    must stop the step before it writes anything.  ``NULL`` means "not
    recorded" and is ignored; a stock with no recorded value keeps the new
    column's default (15%).
    """
    rows = ledger.driver.query(
        "SELECT `a`.`stock_id` AS `stock_id`, "
        "GROUP_CONCAT(DISTINCT `e`.`glycerol_pct` ORDER BY `e`.`glycerol_pct`) "
        "AS `pcts` "
        "FROM `aliquot_bacterial` AS `e` "
        "JOIN `aliquot` AS `a` ON `a`.`id` = `e`.`id` "
        "WHERE `e`.`glycerol_pct` IS NOT NULL "
        "GROUP BY `a`.`stock_id` "
        "HAVING COUNT(DISTINCT `e`.`glycerol_pct`) > 1 "
        "ORDER BY `a`.`stock_id`"
    )
    return [f"{row['stock_id']} ({row['pcts']})" for row in rows]


def plan(ledger: Ledger):
    """Rename/add/update SQL, with the glycerol copy spliced at its safe point."""
    has_extension = _table_exists(ledger, EXTENSION_TABLE)
    if has_extension:
        conflicts = conflicting_stocks(ledger)
        if conflicts:
            raise LedgerError(
                "refusing to migrate molbio 1 -> 2: aliquots of the same stock "
                "disagree on glycerol_pct, which v2 stores on the stock: "
                + ", ".join(conflicts)
                + ". Set one value per stock in aliquot_bacterial (or NULL "
                "everywhere) before retrying."
            )

    missing = [name for name in old_kind_tables() if not _table_exists(ledger, name)]
    if missing:
        raise LedgerError(
            "refusing to migrate molbio 1 -> 2: the ledger has no "
            + ", ".join(missing)
            + "; the v1 molbio tables named by the rename map are not all "
            "present, so this is not a complete molbio 1 ledger (or the "
            "rename map has a typo)"
        )

    schema = target_schema(ledger)
    schema_plan = build_plan(
        ledger.driver, schema, ledger_name=ledger.name, renames=RENAMES
    )
    steps = schema_plan.steps

    add_index = next(
        (
            index
            for index, step in enumerate(steps)
            if step.action == "add_column"
            and step.name == f"{STOCK_TABLE}.glycerol_pct"
        ),
        None,
    )
    if add_index is None:
        if has_extension:  # a v1 ledger that still has tube glycerol needs the add
            raise LedgerError(
                "the plan has no ADD COLUMN for "
                f"{STOCK_TABLE}.glycerol_pct; the ledger does not look like a "
                "complete molbio 1 ledger (was it half-migrated by hand?)"
            )
        # No extension table and no column to add: nothing to splice.
        return [step.sql for step in steps]

    statements: list[str] = []
    for index, step in enumerate(steps):
        if step.action == "drop_table" and step.name == EXTENSION_TABLE:
            # If the planner ever learns to drop the old-named extension, it
            # would schedule the drop before the column add, i.e. before the
            # copy. Keep the one spliced drop below and skip the early one.
            continue
        statements.append(step.sql)
        if index != add_index:
            continue
        # The target column now exists and the source table still does.
        if has_extension:
            statements.append(COPY_GLYCEROL_SQL)
        # The extension is not in the v2 schema; the planner cannot see it under
        # its pre-rename name, so the step drops it after the copy.
        statements.append(DROP_EXTENSION_SQL)
    return statements


def vendor_presets(ledger: Ledger, result) -> str:
    """Replace the ledger's preset copies with the repository copies.

    ``after_commit`` hook: the files are not Dolt content, so the runner can
    only do this after the migration commit exists.  A ledger resolves presets
    **only** from ``<ledger>/presets/`` (ticket 20), so without this the next
    ``open`` would load molbio v1 while ``preset_versions.molbio`` says 2.
    The runner's ``reopened_schema`` hook then re-reads the ledger from these
    files and verifies the fingerprint, so a partial copy cannot pass.
    """
    copies = ledger.path / "presets"
    updated: list[str] = []
    for name in PRESET_FILES:
        source = REPO_PRESETS / name
        target = copies / name
        data = source.read_bytes()
        if target.is_file() and target.read_bytes() == data:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        updated.append(name)

    if not updated:
        return f"presets in {copies} already match the repository copies"
    return (
        f"replaced {', '.join(updated)} in {copies} with the repository copies; "
        "the runner verifies the reopened fingerprint"
    )


step = MigrationStep(
    kind="preset",
    preset="molbio",
    from_version=1,
    plan=plan,
    schema=target_schema,
    after_commit=vendor_presets,
    reopened_schema=reopened_schema,
    after_commit_note=(
        "replace <ledger>/presets/molbio.yaml with presets/molbio.yaml (v2), "
        "then reopen the ledger and compare its fingerprint"
    ),
    description=(
        "rename construct/strain/bacterial/pellet kinds, add the cell_line, "
        "cell_vial and cell_pellet kinds, and move glycerol_pct from the "
        "bacterial aliquot extension to the stock"
    ),
)


if __name__ == "__main__":
    main(step)
