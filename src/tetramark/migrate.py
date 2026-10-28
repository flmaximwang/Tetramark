"""Run one hand-written migration step from ``migration/`` (spec §5).

``migration/`` is keyed by **the version counter that changed**, never by the
Tetramark release: ``migration/spec/<N>-to-<N+1>/migrate.py`` upgrades
``ledger_meta.spec_version`` and ``migration/presets/<preset>/<N>-to-<N+1>/``
upgrades one ``ledger_meta.preset_versions`` entry.  ``migration/README.md``
holds the maintenance rule: whoever bumps ``SPEC_VERSION`` or a preset's
``version:`` adds exactly one folder in the same change.  The steps are run by
hand::

    uv run migration/spec/1-to-2/migrate.py --ledger PATH [--branch B] [--dry-run]

There is deliberately no ``tm`` command for this: the ``tm upgrade`` proposal
in ``docs/issues/001-tm-upgrade.md`` stays a separate design.

This module implements the script contract once, so a step script only
declares the counter, the tag name and its SQL:

1. open the ledger, read the counter and refuse unless it equals ``N``;
2. ``--dry-run`` prints every SQL statement and writes nothing;
3. apply → **exactly one** Dolt commit, ``migrate(ledger): spec N -> N+1`` (or
   ``migrate(ledger): <preset> N -> N+1``); the counter, ``tetramark_version``
   and the fingerprint are bumped in that same commit;
4. the tag ``pre-migrate-<spec|preset>-<N>`` is created before writing;
5. Dolt DDL is not transactional: on failure the last good commit and the
   ``dolt reset --hard`` command are printed, and no commit is made.

Everything goes through the :class:`~tetramark.driver.base.Driver` interface
(:class:`~tetramark.core.ledger.Ledger` + its write session), never through a
hand-written ``dolt`` command, so a step runs identically against an embedded
ledger (``dolt sql`` subprocess) and one served by ``dolt sql-server`` — the
latter is how ticket 24 migrates the real zsqlab10 ledger.

A preset step may ship ``renames.yaml`` next to its ``migrate.py``; load it with
:func:`load_renames` and pass it to
:func:`tetramark.schema.diff.build_plan` as ``renames=`` so the existing rename
machinery (kind table, ``aliquot_<kind>`` table, columns, core ``kind`` values)
does the work instead of hand-written SQL.

A step script looks like::

    from tetramark.migrate import MigrationStep, main
    from tetramark.schema.ddl import view_statements

    def plan(ledger):
        statements = view_statements(ledger.schema)
        return [f"DROP VIEW IF EXISTS `{s.name}`" for s in statements] + [
            s.sql for s in statements
        ]

    step = MigrationStep(kind="spec", from_version=1, plan=plan)

    if __name__ == "__main__":
        main(step)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

import yaml

from tetramark.core.ledger import (
    LEDGER_FILE,
    META_FINGERPRINT,
    META_PRESET_VERSIONS,
    META_SPEC_VERSION,
    META_TETRAMARK_VERSION,
    Ledger,
    open as open_ledger,
)
from tetramark.driver.base import ensure_ascii, literal
from tetramark.driver.workspace import WORKSPACE_FILE, Workspace
from tetramark.errors import LedgerError, UsageError
from tetramark.schema.diff import RenameMap

__all__ = [
    "MigrationError",
    "MigrationResult",
    "MigrationStep",
    "PlanCallable",
    "counter",
    "load_renames",
    "main",
    "open_target",
    "run",
]

#: A step's SQL builder: ``(ledger) -> ordered SQL statements``.
PlanCallable = Callable[[Ledger], Sequence[str]]

#: Kinds of counter a step can bump.
_STEP_KINDS = ("spec", "preset")

#: A preset name ends up in a tag name and a commit message.
_PRESET_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._-]*$")


class MigrationError(LedgerError):
    """A step failed after it started writing.

    The failure report (last good commit + ``dolt reset --hard``) has already
    been printed to stderr, so :func:`main` only maps this to exit code 1
    without repeating the report.
    """


@dataclass(frozen=True)
class MigrationStep:
    """One ``migration/<…>/migrate.py`` step: counter ``N`` → ``N + 1``.

    ``plan`` is called with the opened ledger and returns the ordered SQL
    statements of the step.  Reads are fine inside it; the helper collects the
    result and writes it in one write session.

    ``kind`` is ``"spec"`` (bump ``ledger_meta.spec_version``) or ``"preset"``
    (bump one entry of ``ledger_meta.preset_versions``); a preset step must
    name its ``preset``.
    """

    kind: str
    from_version: int
    plan: PlanCallable
    preset: str | None = None
    description: str = ""

    def __post_init__(self) -> None:
        if self.kind not in _STEP_KINDS:
            raise UsageError(
                f"invalid migration kind {self.kind!r} (use 'spec' or 'preset')"
            )
        if self.kind == "preset":
            if not self.preset:
                raise UsageError("a preset migration step needs preset=<name>")
            if not _PRESET_NAME_RE.match(self.preset):
                raise UsageError(f"invalid preset name {self.preset!r}")
        elif self.preset is not None:
            raise UsageError("a spec migration step must not name a preset")
        if not isinstance(self.from_version, int) or self.from_version < 1:
            raise UsageError(
                f"from_version must be a positive integer, not {self.from_version!r}"
            )
        ensure_ascii(self.commit_summary)

    # -- identity ----------------------------------------------------------
    @property
    def label(self) -> str:
        """``spec`` or the preset name; appears in the tag and commit message."""
        return "spec" if self.kind == "spec" else str(self.preset)

    @property
    def to_version(self) -> int:
        return self.from_version + 1

    @property
    def tag(self) -> str:
        """The restore point created before the step writes anything."""
        return f"pre-migrate-{self.label}-{self.from_version}"

    @property
    def commit_summary(self) -> str:
        """``spec 1 -> 2`` / ``molbio 1 -> 2``.

        The ledger write session prefixes ``migrate(ledger): `` so the Dolt
        commit message is exactly ``migrate(ledger): spec 1 -> 2``.
        """
        return f"{self.label} {self.from_version} -> {self.to_version}"

    @property
    def counter_label(self) -> str:
        """Human name of the counter this step upgrades."""
        if self.kind == "spec":
            return META_SPEC_VERSION
        return f"{META_PRESET_VERSIONS}[{self.preset}]"

    @property
    def title(self) -> str:
        return f"migration {self.label} {self.from_version} -> {self.to_version}"


@dataclass
class MigrationResult:
    """What :func:`run` did (or, with ``dry_run``, would have done)."""

    step: MigrationStep
    ledger: str
    path: Path
    driver: str
    counter_before: int
    tag: str
    head_before: str
    #: Statements of the one commit: the step's plan followed by the meta bump.
    statements: list[str] = field(default_factory=list)
    #: ``CALL DOLT_TAG(...)``; ``None`` when the tag already pointed at HEAD.
    tag_statement: str | None = None
    tag_note: str = ""
    tag_created: bool = False
    commit_hash: str | None = None
    dry_run: bool = False

    @property
    def applied(self) -> bool:
        return self.commit_hash is not None


# ---------------------------------------------------------------------------
# Opening the target
# ---------------------------------------------------------------------------


def open_target(
    path: str | Path, *, branch: str | None = None, dry_run: bool = False
) -> Ledger:
    """Open the ledger at ``path`` through its workspace, if it has one.

    ``path`` is the ledger directory (the Dolt database), e.g.
    ``/Users/…/Dolt/zsqlab_10_gammaPFD-Fiber_2026.06.25``.  When the parent
    directory has a ``workspace.yaml`` the workspace decides the driver — a
    running ``dolt sql-server`` is used, otherwise the embedded driver — so the
    same step works for a lab workspace and a bare checkout.  A bare Dolt
    directory (no ``workspace.yaml``) is always opened embedded.
    """
    target = Path(path).expanduser()
    if not target.is_dir():
        raise UsageError(f"ledger path {target} is not a directory")
    target = target.resolve()
    if not (target / ".dolt").is_dir() or not (target / LEDGER_FILE).is_file():
        raise UsageError(
            f"{target} is not a Tetramark ledger (it needs .dolt and "
            f"{LEDGER_FILE}): pass the ledger directory itself, not the "
            "workspace data directory"
        )
    if (target.parent / WORKSPACE_FILE).is_file():
        workspace = Workspace.load(target.parent)
    else:
        workspace = Workspace(target.parent)
    if target.name not in workspace.list_ledgers():
        raise UsageError(f"{target} is not a ledger of {workspace.data_dir}")
    return open_ledger(workspace, target.name, branch=branch, dry_run=dry_run)


def _driver_kind(ledger: Ledger) -> str:
    name = type(ledger.driver).__name__
    return name[: -len("Driver")].lower() if name.endswith("Driver") else name.lower()


def _branch(ledger: Ledger) -> str | None:
    return getattr(ledger.driver, "branch", None)


# ---------------------------------------------------------------------------
# Preconditions
# ---------------------------------------------------------------------------


def counter(step: MigrationStep, ledger: Ledger) -> int | None:
    """Read the counter this step upgrades (``None`` when the ledger has none).

    ``spec_version`` comes straight from ``ledger_meta``; a preset version is
    one entry of the JSON ``preset_versions`` value.
    """
    if step.kind == "spec":
        raw = ledger.meta.get(META_SPEC_VERSION)
        if raw is None:
            return None
        try:
            return int(str(raw).strip())
        except ValueError:
            raise LedgerError(
                f"ledger {ledger.name!r}: {META_SPEC_VERSION} is not an integer "
                f"({raw!r})"
            ) from None

    raw = ledger.meta.get(META_PRESET_VERSIONS)
    try:
        payload: Any = json.loads(raw) if raw else {}
    except json.JSONDecodeError as error:
        raise LedgerError(
            f"ledger {ledger.name!r}: {META_PRESET_VERSIONS} is not valid JSON "
            f"({error})"
        ) from error
    if not isinstance(payload, dict):
        raise LedgerError(
            f"ledger {ledger.name!r}: {META_PRESET_VERSIONS} must be a JSON object"
        )
    value = payload.get(step.preset) if step.preset is not None else None
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise LedgerError(
            f"ledger {ledger.name!r}: {META_PRESET_VERSIONS}[{step.preset!r}] "
            f"is not an integer ({value!r})"
        ) from None


def _require_counter(step: MigrationStep, ledger: Ledger) -> int:
    current = counter(step, ledger)
    if current is None:
        raise LedgerError(
            f"refusing to run {step.title}: ledger {ledger.name!r} records no "
            f"{step.counter_label} (not a formatted Tetramark ledger for this "
            "counter)"
        )
    if current != step.from_version:
        raise LedgerError(
            f"refusing to run {step.title}: ledger {ledger.name!r} is at "
            f"{step.counter_label} {current}, expected {step.from_version}"
        )
    return current


def _require_clean(ledger: Ledger) -> None:
    rows = ledger.driver.query(
        "SELECT `table_name` AS `name` FROM `dolt_status` ORDER BY `table_name`"
    )
    dirty = [str(row["name"]) for row in rows]
    if not dirty:
        return
    shown = ", ".join(dirty[:5])
    if len(dirty) > 5:
        shown += f", ... {len(dirty) - 5} more"
    raise LedgerError(
        f"refusing to migrate ledger {ledger.name!r}: its working set has "
        f"uncommitted changes ({shown}). Commit or discard them first: a "
        "migration must leave exactly one commit of its own"
    )


def _head(ledger: Ledger) -> str:
    rows = ledger.driver.query("SELECT DOLT_HASHOF('HEAD') AS `hash`")
    value = rows[0].get("hash") if rows else None
    if value is None:
        raise LedgerError(
            f"cannot read HEAD of ledger {ledger.name!r} (DOLT_HASHOF is empty)"
        )
    return str(value)


# ---------------------------------------------------------------------------
# The statements of one run
# ---------------------------------------------------------------------------


def _plan_statements(step: MigrationStep, ledger: Ledger) -> list[str]:
    statements: list[str] = []
    for index, statement in enumerate(step.plan(ledger), start=1):
        if not isinstance(statement, str) or not statement.strip():
            raise LedgerError(
                f"{step.title}: the plan returned an empty/non-SQL entry at "
                f"position {index} ({statement!r})"
            )
        statements.append(statement.strip().rstrip(";"))
    if not statements:
        raise LedgerError(f"{step.title}: the plan returned no SQL statements")
    return statements


def _meta_statements(step: MigrationStep, ledger: Ledger) -> list[str]:
    """The counter, ``tetramark_version`` and fingerprint updates.

    They are ordinary statements of the same write session, so they land in the
    one migration commit.
    """
    from tetramark import __version__

    values: dict[str, str] = {
        META_TETRAMARK_VERSION: __version__,
        META_FINGERPRINT: ledger.schema.fingerprint(),
    }
    if step.kind == "spec":
        values[META_SPEC_VERSION] = str(step.to_version)
    else:
        payload = _preset_versions(ledger)
        assert step.preset is not None
        payload[step.preset] = step.to_version
        values[META_PRESET_VERSIONS] = json.dumps(
            payload, sort_keys=True, ensure_ascii=False
        )
    return [
        "INSERT INTO `ledger_meta` (`key`, `value`) VALUES "
        f"({literal(key)}, {literal(value)}) "
        "ON DUPLICATE KEY UPDATE `value` = VALUES(`value`)"
        for key, value in values.items()
    ]


def _preset_versions(ledger: Ledger) -> dict[str, Any]:
    raw = ledger.meta.get(META_PRESET_VERSIONS)
    try:
        payload: Any = json.loads(raw) if raw else {}
    except json.JSONDecodeError as error:
        raise LedgerError(
            f"ledger {ledger.name!r}: {META_PRESET_VERSIONS} is not valid JSON "
            f"({error})"
        ) from error
    if not isinstance(payload, dict):
        raise LedgerError(
            f"ledger {ledger.name!r}: {META_PRESET_VERSIONS} must be a JSON object"
        )
    return dict(payload)


def _tag_plan(
    step: MigrationStep, ledger: Ledger, head: str
) -> tuple[str | None, str]:
    """The tag SQL to run and a human note (``None`` when the tag exists).

    An existing tag is reused only when it points at ``head`` (a previous
    failed attempt before any commit); a tag pointing anywhere else means the
    ledger moved since, which the operator must resolve, so the run refuses.
    """
    rows = ledger.driver.query(
        "SELECT `tag_hash` AS `hash` FROM `dolt_tags` WHERE `tag_name` = %s",
        (step.tag,),
    )
    if rows:
        existing = str(rows[0]["hash"])
        if existing != head:
            raise LedgerError(
                f"refusing to run {step.title}: tag {step.tag!r} already exists "
                f"at {existing}, but HEAD is {head}. Delete the tag or restore "
                "the ledger before retrying"
            )
        return None, f"{step.tag} already exists at HEAD (reusing it)"
    return (
        f"CALL DOLT_TAG({literal(step.tag)}, {literal(head)})",
        f"{step.tag} -> {head} (created before writing)",
    )


def _create_tag(ledger: Ledger, statement: str) -> None:
    """Create the restore-point tag outside the migration commit."""
    with ledger.driver.transaction():
        ledger.driver.execute(statement)


# ---------------------------------------------------------------------------
# Run / report
# ---------------------------------------------------------------------------


def run(
    step: MigrationStep,
    *,
    ledger_path: str | Path,
    branch: str | None = None,
    dry_run: bool = False,
    out: Any = None,
    err: Any = None,
) -> MigrationResult:
    """Run one step and return what was done.

    Raises :class:`~tetramark.errors.LedgerError` for a refused precondition and
    :class:`MigrationError` when the SQL failed after the tag was made (the
    report with the restore command has already been printed).
    """
    stdout = out or sys.stdout
    stderr = err or sys.stderr

    ledger = open_target(ledger_path, branch=branch, dry_run=dry_run)
    try:
        state = _prepare(step, ledger, dry_run=dry_run)
        _print_plan(step, ledger, state, stdout)
        if dry_run:
            stdout.write(
                f"dry run: {len(state.statements)} statement(s) in one commit, "
                "nothing was written\n"
            )
            return state
        return _apply(step, ledger, state, stdout=stdout, stderr=stderr)
    finally:
        ledger.close()


def _prepare(
    step: MigrationStep, ledger: Ledger, *, dry_run: bool
) -> MigrationResult:
    current = _require_counter(step, ledger)
    _require_clean(ledger)
    head = _head(ledger)
    tag_statement, tag_note = _tag_plan(step, ledger, head)
    statements = [
        *_plan_statements(step, ledger),
        *_meta_statements(step, ledger),
    ]
    return MigrationResult(
        step=step,
        ledger=ledger.name,
        path=ledger.path,
        driver=_driver_kind(ledger),
        counter_before=current,
        tag=step.tag,
        head_before=head,
        statements=statements,
        tag_statement=tag_statement,
        tag_note=tag_note,
        dry_run=dry_run,
    )


def _print_plan(
    step: MigrationStep,
    ledger: Ledger,
    state: MigrationResult,
    stdout: Any,
) -> None:
    branch = _branch(ledger)
    where = f"{state.driver} driver"
    where += f", branch {branch!r}" if branch else ""
    stdout.write(f"migration: {step.label} {step.from_version} -> {step.to_version}\n")
    if step.description:
        stdout.write(f"what:       {step.description}\n")
    stdout.write(f"ledger:     {state.ledger}\n")
    stdout.write(f"path:       {state.path} ({where})\n")
    stdout.write(f"counter:    {step.counter_label} = {state.counter_before}\n")
    stdout.write(f"tag:        {state.tag_note}\n")
    if state.tag_statement is not None:
        stdout.write(f"  {state.tag_statement};\n")
    stdout.write(f"statements: {len(state.statements)}\n")
    for statement in state.statements:
        stdout.write(f"  {statement};\n")


def _apply(
    step: MigrationStep,
    ledger: Ledger,
    state: MigrationResult,
    *,
    stdout: Any,
    stderr: Any,
) -> MigrationResult:
    if state.tag_statement is not None:
        _create_tag(ledger, state.tag_statement)
        state.tag_created = True

    try:
        with ledger.write(
            step.commit_summary, verb="migrate", object="ledger"
        ) as session:
            for statement in state.statements:
                session.execute(statement)
    except BaseException as error:
        _report_failure(step, ledger, state.head_before, error, stderr=stderr)
        raise MigrationError(f"{step.title} failed: {error}") from error

    state.commit_hash = session.commit_hash
    ledger.read_meta()
    _verify_after(step, ledger, state)
    expected = f"migrate(ledger): {step.commit_summary}"
    stdout.write(
        f"committed {state.commit_hash}: {expected}\n"
        f"ledger {ledger.name!r} is now at {step.counter_label} {step.to_version} "
        f"({META_TETRAMARK_VERSION} {ledger.meta.get(META_TETRAMARK_VERSION)})\n"
    )
    return state


def _verify_after(step: MigrationStep, ledger: Ledger, state: MigrationResult) -> None:
    """Refuse to report success when the commit did not do what it promised."""
    if state.commit_hash is None:  # pragma: no cover - defensive
        raise MigrationError(f"{step.title}: no Dolt commit was created")
    current = counter(step, ledger)
    if current != step.to_version:
        raise MigrationError(
            f"{step.title}: the commit was made, but {step.counter_label} is "
            f"{current}, expected {step.to_version}"
        )
    expected = f"migrate(ledger): {step.commit_summary}"
    rows = ledger.driver.query(
        "SELECT `message` FROM `dolt_log` WHERE `commit_hash` = %s",
        (state.commit_hash,),
    )
    message = str(rows[0]["message"]) if rows else ""
    if message.strip() != expected:
        raise MigrationError(
            f"{step.title}: commit {state.commit_hash} has message "
            f"{message!r}, expected {expected!r}"
        )


def _report_failure(
    step: MigrationStep,
    ledger: Ledger,
    head_before: str,
    error: BaseException,
    *,
    stderr: Any,
) -> None:
    """Print the last good commit and the ``dolt reset --hard`` command."""
    last_good = head_before
    try:
        last_good = _head(ledger)
    except BaseException:  # noqa: BLE001 - the connection itself may have failed
        pass
    stderr.write(f"{step.title} failed: {error}\n")
    stderr.write(f"last good commit: {last_good} (no migration commit was made)\n")
    stderr.write(
        "Dolt DDL is not transactional, so the statements above may have run "
        "partially. Discard the partial DDL with:\n"
        f"  dolt reset --hard {last_good}\n"
        "  # or, while the workspace server is running:\n"
        f"  CALL DOLT_RESET('--hard', {literal(last_good)})\n"
        f"restore-point tag (unchanged): {step.tag}\n"
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(step: MigrationStep, argv: Sequence[str] | None = None) -> None:
    """The ``--ledger/--branch/--dry-run`` entry point every step script uses."""
    parser = argparse.ArgumentParser(
        prog=_script_name(),
        description=f"{step.title}: {step.description or 'migration step'}",
    )
    parser.add_argument(
        "--ledger",
        required=True,
        metavar="PATH",
        help="ledger directory (the Dolt database, not the workspace directory)",
    )
    parser.add_argument(
        "--branch",
        metavar="B",
        help=(
            "session branch every read, write and commit happens on "
            "(default: 'branch:' in ledger.yaml, else the checked-out branch); "
            "a missing branch is an error, never created"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print every SQL statement and write nothing",
    )
    arguments = parser.parse_args(argv)
    try:
        run(
            step,
            ledger_path=arguments.ledger,
            branch=arguments.branch,
            dry_run=arguments.dry_run,
        )
    except MigrationError:
        # The failure report (last good commit + reset command) was printed.
        raise SystemExit(1)
    except LedgerError as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(error.exit_code)


def _script_name() -> str:
    argv0 = Path(sys.argv[0]).name if sys.argv else "migrate.py"
    return argv0 or "migrate.py"


# ---------------------------------------------------------------------------
# renames.yaml
# ---------------------------------------------------------------------------


def load_renames(path: str | Path) -> RenameMap:
    """Read a preset step's optional ``renames.yaml`` as a :class:`RenameMap`.

    The file is the explicit alternative to ``renamed_from`` in the preset:
    ``entity``/``stock``/``box`` map old kind → new kind and ``fields`` maps
    ``<object>.<new kind>`` → ``{old column: new column}``.  Pass the result to
    :func:`tetramark.schema.diff.build_plan` as ``renames=``.
    """
    target = Path(path).expanduser()
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as error:
        raise UsageError(f"cannot read renames file {target}: {error}") from error
    try:
        payload = yaml.safe_load(text) or {}
    except yaml.YAMLError as error:
        raise UsageError(f"invalid renames file {target}: {error}") from error
    if not isinstance(payload, dict):
        raise UsageError(
            f"invalid renames file {target}: expected a mapping with "
            "'entity', 'stock', 'box' and/or 'fields' keys"
        )
    return RenameMap.from_dict(payload)
