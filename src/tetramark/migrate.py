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
hand-written ``dolt`` command, so a step runs against the ledger directory via
the embedded driver (``dolt sql`` subprocess).  A user-run ``dolt sql-server``
on the directory is auto-routed by ``dolt sql`` itself; Tetramark does not
manage one.

A preset step may ship ``renames.yaml`` next to its ``migrate.py``; load it with
:func:`load_renames` and pass it to
:func:`tetramark.schema.diff.build_plan` as ``renames=`` so the existing rename
machinery (kind table, ``aliquot_<kind>`` table, columns, core ``kind`` values)
does the work instead of hand-written SQL.

A step whose target schema differs from the ledger's current one (a preset
step always does) declares it with ``MigrationStep(schema=...)``: a callable
``(ledger) -> EffectiveSchema`` that loads the **new** schema from the
repository (the ledger itself still opens with its old preset copy).  The
runner then, in the same commit, writes that schema's fingerprint and
regenerates the derived ``dolt_tests`` from it — otherwise the commit would
record a stale fingerprint and tests that still name the old tables.  The
step's ``plan`` may call the same function itself to build the diff.

Files are not Dolt content, so a step that also replaces ``<ledger>/presets/``
(``tm init`` copies presets into the ledger; a preset step must vendor the new
copy) passes ``after_commit=...``: a callable ``(ledger, result) -> note`` that
the runner runs **only on a real apply**, after the commit.  Its failure is
reported as "the commit exists, the files were not updated" and exits 1.

Two wave-10 details: a **bundle** preset has no kinds of its own, so its
migration step introduces the counters of the parts it now announces
(``preset_versions_extra={part: 1, ...}``, written in the same commit); and a
step that installs a *historical* preset version ships that content next to its
``migrate.py`` and places it with :func:`vendor_preset_files`, because the
repository's ``presets/`` only ever holds the current version.

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
from typing import Any, Callable, Mapping, Sequence

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
from tetramark.errors import LedgerError, UsageError
from tetramark.schema.diff import RenameMap
from tetramark.schema.model import EffectiveSchema
from tetramark.schema.tests_gen import generate_tests, install_tests

__all__ = [
    "AfterCommitCallable",
    "MigrationError",
    "MigrationResult",
    "MigrationStep",
    "PlanCallable",
    "ReopenedSchemaCallable",
    "SchemaCallable",
    "counter",
    "load_renames",
    "main",
    "open_target",
    "preset_args",
    "restore_files",
    "run",
    "vendor_preset_files",
    "vendor_repo_presets",
]

#: A step's SQL builder: ``(ledger) -> ordered SQL statements``.
PlanCallable = Callable[[Ledger], Sequence[str]]

#: A step's target-schema loader: ``(ledger) -> the schema after this step``.
SchemaCallable = Callable[[Ledger], EffectiveSchema]

#: A post-commit hook: ``(ledger, result) -> report note or None``.
AfterCommitCallable = Callable[[Ledger, "MigrationResult"], str | None]

#: Loads the schema the ledger now opens with, from its own files.
ReopenedSchemaCallable = Callable[[Ledger], EffectiveSchema]

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

    ``schema`` — optional ``(ledger) -> EffectiveSchema`` loader of the schema
    **after** the step.  When set, the runner records that schema's
    fingerprint (never the ledger's current one) and regenerates the derived
    ``dolt_tests`` from it, both inside the migration commit.  The step's
    ``plan`` normally calls the same loader to diff against the new schema.

    ``after_commit`` — optional ``(ledger, result) -> note`` hook for changes
    that are **not** Dolt content (a preset step replaces
    ``<ledger>/presets/`` with the repository copy).  It runs only on a real
    apply, after the commit; ``after_commit_note`` describes it in a dry run.

    ``reopened_schema`` — optional ``(ledger) -> EffectiveSchema`` loader of
    the schema the ledger **now** opens with (its own preset copy, re-read
    from disk).  It runs after ``after_commit`` and before the success
    report; its fingerprint must equal the one the commit recorded, so a
    stale or wrong preset copy can never pass as migrated.

    ``preset_versions_extra`` — additional ``ledger_meta.preset_versions``
    entries this step introduces, e.g. the parts a new **bundle** preset
    announces (``{"nucleic_acids": 1, ...}``).  They are written in the same
    commit as the stepped counter, and the runner refuses when the target
    schema does not load one of them at the declared version.
    """

    kind: str
    from_version: int
    plan: PlanCallable
    preset: str | None = None
    description: str = ""
    schema: SchemaCallable | None = None
    after_commit: AfterCommitCallable | None = None
    after_commit_note: str = ""
    reopened_schema: ReopenedSchemaCallable | None = None
    preset_versions_extra: Mapping[str, int] = field(default_factory=dict)

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
        if self.preset_versions_extra:
            if self.kind != "preset":
                raise UsageError(
                    "preset_versions_extra is only valid on a preset step"
                )
            for name, version in self.preset_versions_extra.items():
                if not isinstance(version, int) or version < 1:
                    raise UsageError(
                        "preset_versions_extra values must be positive integers "
                        f"(got {name}={version!r})"
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
    #: The schema after the step, when the step declared a ``schema`` loader.
    #: Its fingerprint is written in the commit and its generated tests installed.
    target_schema: EffectiveSchema | None = field(default=None, repr=False)
    #: Generated ``dolt_tests`` rows the commit installs (0: not regenerated).
    tests_regenerated: int = 0
    #: True when the step's ``after_commit`` file hook ran successfully.
    after_commit_done: bool = False
    #: Human note about the post-commit file change (dry-run text before a run).
    after_commit_note: str = ""

    @property
    def applied(self) -> bool:
        return self.commit_hash is not None


# ---------------------------------------------------------------------------
# Opening the target
# ---------------------------------------------------------------------------


def open_target(
    path: str | Path,
    *,
    branch: str | None = None,
    dry_run: bool = False,
    allow_legacy_statuses: bool = True,
) -> Ledger:
    """Open the standalone ledger at ``path``.

    ``path`` is the ledger directory (the Dolt database plus its
    ``ledger.yaml``), e.g. ``/Users/…/Dolt/zsqlab_10_gammaPFD-Fiber_2026.06.25``.
    A ledger is self-contained in 0.5.0, so the path is opened directly: no
    parent directory is searched and ``workspace.yaml`` is never read.  The
    ledger is opened through the embedded driver (``dolt sql`` in the ledger
    directory), so a user-run ``dolt sql-server`` is auto-routed by ``dolt
    sql``, not managed here.

    ``allow_legacy_statuses`` defaults to true: a migration step must read the
    old shape it is about to change (a spec-3 ``ledger.yaml`` still carries the
    removed ``statuses:`` key). Normal reads keep the spec-4 refusal.
    """
    target = Path(path).expanduser()
    if not target.is_dir():
        raise UsageError(f"ledger path {target} is not a directory")
    target = target.resolve()
    if not (target / ".dolt").is_dir() or not (target / LEDGER_FILE).is_file():
        raise UsageError(
            f"{target} is not a Tetramark ledger (it needs .dolt and "
            f"{LEDGER_FILE}): pass the ledger directory itself"
        )
    return open_ledger(
        target,
        branch=branch,
        dry_run=dry_run,
        allow_legacy_statuses=allow_legacy_statuses,
    )


#: First spec whose core tables carry ``aliquot_event.forced``.
_SPEC4 = 4

#: Generated tests that read the spec-4 core shape and therefore cannot run on a
#: ledger that has not reached spec 4 yet.  A preset step may run earlier (e.g.
#: molbio 1 -> 2 on a spec-2 ledger); installing this rule there would query the
#: ``aliquot_event.forced`` column that migration/spec/3-to-4 adds.
SPEC4_ONLY_TESTS: frozenset[str] = frozenset({"core_status_event_allowed"})


def _ledger_spec_version(ledger: Ledger) -> int:
    """The ledger's stored ``spec_version`` (0 when it cannot be read)."""
    try:
        return int(str(ledger.meta.get("spec_version") or 0))
    except (TypeError, ValueError):  # pragma: no cover - corrupt meta
        return 0


def _step_target_spec(step: "MigrationStep", ledger: Ledger) -> int:
    """The ledger's spec version after this step runs.

    A ``spec`` step bumps the format counter to ``to_version``; a preset step
    leaves the format alone, so the ledger keeps its current spec.
    """
    if step.kind == "spec":
        return step.to_version
    return _ledger_spec_version(ledger)


def _generated_tests_for(
    ledger: Ledger, target_schema: Any, *, spec_version: int
) -> list[Any]:
    """The generated tests a step installs on this ledger.

    A step that declares ``schema=`` regenerates ``dolt_tests`` from the target
    schema in its own commit.  When the ledger stays older than spec 4, the
    spec-4-only tests are omitted: they would reference the
    ``aliquot_event.forced`` column that migration/spec/3-to-4 adds.
    """
    tests = generate_tests(target_schema)
    if spec_version < _SPEC4:
        tests = [test for test in tests if test.name not in SPEC4_ONLY_TESTS]
    return tests


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


def _meta_statements(
    step: MigrationStep,
    ledger: Ledger,
    target_schema: EffectiveSchema | None = None,
) -> list[str]:
    """The counter, ``tetramark_version`` and fingerprint updates.

    They are ordinary statements of the same write session, so they land in the
    one migration commit.  The fingerprint is the **target** schema's when the
    step declares one: the ledger's current (old) preset fingerprint would make
    every later ``open`` warn about a mismatch.
    """
    from tetramark import __version__

    schema = target_schema if target_schema is not None else ledger.schema
    values: dict[str, str] = {
        META_TETRAMARK_VERSION: __version__,
        META_FINGERPRINT: schema.fingerprint(),
    }
    if step.kind == "spec":
        values[META_SPEC_VERSION] = str(step.to_version)
    else:
        payload = _preset_versions(ledger)
        payload.update(step.preset_versions_extra)
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
    # The target schema is loaded before the plan: a step that declares one
    # gets the new fingerprint and the regenerated dolt_tests in its commit.
    target_schema = step.schema(ledger) if step.schema is not None else None
    if target_schema is not None and step.kind == "preset":
        assert step.preset is not None
        version = target_schema.preset_versions.get(step.preset)
        if version != step.to_version:
            raise LedgerError(
                f"refusing to run {step.title}: the target schema loads "
                f"preset_versions[{step.preset}] = {version}, expected "
                f"{step.to_version}; the repository preset is not the "
                "version this step upgrades to"
            )
        for name, expected in step.preset_versions_extra.items():
            loaded = target_schema.preset_versions.get(name)
            if loaded != expected:
                raise LedgerError(
                    f"refusing to run {step.title}: the target schema loads "
                    f"preset_versions[{name}] = {loaded}, but the step "
                    f"declares {expected}; the preset list the ledger loads "
                    "does not match the counters this step adds"
                )
    statements = [
        *_plan_statements(step, ledger),
        *_meta_statements(step, ledger, target_schema),
    ]
    state = MigrationResult(
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
        target_schema=target_schema,
        after_commit_note=step.after_commit_note,
    )
    if target_schema is not None:
        state.tests_regenerated = len(
            _generated_tests_for(
                ledger, target_schema, spec_version=_step_target_spec(step, ledger)
            )
        )
    return state


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
    if state.target_schema is not None:
        stdout.write(
            f"dolt tests: replace with {state.tests_regenerated} generated "
            "test(s) from the target schema (same commit)\n"
        )
    if step.after_commit is not None:
        note = step.after_commit_note or "run the step's post-commit file hook"
        stdout.write(f"after commit: {note}\n")


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
            # Derived state (spec §4): the generated dolt_tests are regenerated
            # from the target schema in the same transaction, never left naming
            # the pre-migration tables.  ``WriteSession`` is driver-shaped.
            if state.target_schema is not None:
                state.tests_regenerated = len(
                    install_tests(  # type: ignore[arg-type]
                        session,
                        state.target_schema,
                        tests=_generated_tests_for(
                            ledger,
                            state.target_schema,
                            spec_version=_step_target_spec(step, ledger),
                        ),
                    )
                )
    except BaseException as error:
        _report_failure(step, ledger, state.head_before, error, stderr=stderr)
        raise MigrationError(f"{step.title} failed: {error}") from error

    state.commit_hash = session.commit_hash
    expected = f"migrate(ledger): {step.commit_summary}"
    stdout.write(f"committed {state.commit_hash}: {expected}\n")
    # Files outside Dolt first, then read the ledger back from its own
    # preset copy and verify that reality, not just the statements we ran.
    if step.after_commit is not None:
        _run_after_commit(step, ledger, state, stdout=stdout, stderr=stderr)
    try:
        # ``_verify_after`` refreshes ``ledger.meta`` and checks the commit
        # message; it reads both in one batch (see _read_commit_and_meta).
        _verify_after(step, ledger, state)
    except MigrationError as error:
        # This failure happens after the commit, where no other report is
        # printed: say why the run is exiting 1.
        stderr.write(f"error: {error}\n")
        raise
    stdout.write(
        f"ledger {ledger.name!r} is now at {step.counter_label} {step.to_version} "
        f"({META_TETRAMARK_VERSION} {ledger.meta.get(META_TETRAMARK_VERSION)})\n"
    )
    return state


def _run_after_commit(
    step: MigrationStep,
    ledger: Ledger,
    state: MigrationResult,
    *,
    stdout: Any,
    stderr: Any,
) -> None:
    """Run the step's post-commit file hook; its failure never hides the commit.

    The migration commit exists by now, so a failure here is not a failed
    migration: the report says exactly that, names the commit, and exits 1 so
    the operator fixes the files (the step README documents them) before the
    ledger is reopened.
    """
    assert step.after_commit is not None
    try:
        note = step.after_commit(ledger, state)
    except BaseException as error:
        stderr.write(
            f"{step.title}: commit {state.commit_hash} was made but the "
            f"post-commit preset copy update failed: {error}\n"
            "The Dolt database is migrated; only files outside Dolt (the "
            "preset copy under <ledger>/presets/) were not updated. Fix them "
            "by hand as the step README says, then reopen the ledger.\n"
        )
        raise MigrationError(
            f"{step.title}: commit {state.commit_hash} was made but the "
            f"post-commit preset copy update failed: {error}"
        ) from error
    state.after_commit_done = True
    state.after_commit_note = note or state.after_commit_note
    if note:
        stdout.write(f"post-commit: {note}\n")


def _read_commit_and_meta(ledger: Ledger, commit_hash: str) -> str:
    """Refresh ``ledger.meta`` and return a commit's message, in one batch.

    The post-commit verification needs the new ``ledger_meta`` rows and the
    commit message; ``query_many`` costs one ``dolt sql`` run for the rows and
    one for the CSV column lists, where two separate ``query`` calls cost four.
    A read error propagates as the real :class:`LedgerError`; swallowing it
    would empty ``meta`` and report a misleading version/schema mismatch.
    """
    meta_rows, log_rows = ledger.driver.query_many(
        [
            ("SELECT `key`, `value` FROM `ledger_meta` ORDER BY `key`", None),
            (
                "SELECT `message` FROM `dolt_log` WHERE `commit_hash` = %s",
                (commit_hash,),
            ),
        ]
    )
    ledger.meta = {str(row["key"]): str(row["value"]) for row in meta_rows}
    return str(log_rows[0]["message"]) if log_rows else ""


def _verify_after(step: MigrationStep, ledger: Ledger, state: MigrationResult) -> None:
    """Refuse to report success when the commit did not do what it promised."""
    if state.commit_hash is None:  # pragma: no cover - defensive
        raise MigrationError(f"{step.title}: no Dolt commit was created")
    message = _read_commit_and_meta(ledger, state.commit_hash)
    current = counter(step, ledger)
    if current != step.to_version:
        raise MigrationError(
            f"{step.title}: the commit was made, but {step.counter_label} is "
            f"{current}, expected {step.to_version}"
        )
    if state.target_schema is not None:
        live = ledger.meta.get(META_FINGERPRINT)
        expected = state.target_schema.fingerprint()
        if live != expected:
            raise MigrationError(
                f"{step.title}: the commit was made, but the recorded "
                f"{META_FINGERPRINT} is {live}, expected the target schema's "
                f"{expected}"
            )
    if step.preset_versions_extra:
        recorded_versions = _preset_versions(ledger)
        for name, version in step.preset_versions_extra.items():
            if recorded_versions.get(name) != version:
                raise MigrationError(
                    f"{step.title}: the commit was made, but "
                    f"{META_PRESET_VERSIONS}[{name}] is "
                    f"{recorded_versions.get(name)!r}, expected {version}"
                )
    if step.reopened_schema is not None:
        recorded = ledger.meta.get(META_FINGERPRINT)
        actual = step.reopened_schema(ledger)
        if actual.fingerprint() != recorded:
            raise MigrationError(
                f"{step.title}: the commit was made, but the ledger now "
                f"opens with fingerprint {actual.fingerprint()} (its own "
                f"preset copy) while the commit recorded {recorded}"
            )
    expected = f"migrate(ledger): {step.commit_summary}"
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
        help="ledger directory (the directory holding .dolt/ and ledger.yaml)",
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


# ---------------------------------------------------------------------------
# Preset-file placement (wave-10 preset steps)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CommonArgs:
    """The ``--ledger/--branch/--dry-run`` arguments every step accepts."""

    ledger: str
    branch: str | None
    dry_run: bool


def preset_args(argv: Sequence[str] | None = None) -> CommonArgs:
    """Parse the common step arguments before the ledger is opened.

    A wave-10 preset step must place the repository preset copies **before**
    opening the ledger (the spec-4 loader refuses a stock kind without
    ``statuses:``), so the step script needs these arguments outside the shared
    runner as well.
    """
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--ledger", required=True, metavar="PATH")
    parser.add_argument("--branch", metavar="B", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parsed, _unknown = parser.parse_known_args(sys.argv[1:] if argv is None else argv)
    return CommonArgs(
        ledger=parsed.ledger,
        branch=parsed.branch,
        dry_run=bool(parsed.dry_run),
    )


def _preset_include_closure(
    names: Sequence[str], *, repo_presets_dir: Path
) -> list[str]:
    """``names`` plus the repository ``include:`` closure of every bundled preset."""
    ordered: list[str] = []
    seen: set[str] = set()

    def visit(name: str) -> None:
        if name in seen:
            return
        seen.add(name)
        source = repo_presets_dir / f"{name}.yaml"
        if not source.is_file():
            ordered.append(name)
            return
        try:
            payload = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):  # pragma: no cover - defensive
            ordered.append(name)
            return
        include = payload.get("include") if isinstance(payload, dict) else None
        for included in include or []:
            visit(str(included))
        ordered.append(name)

    for name in names:
        visit(name)
    return ordered


def vendor_repo_presets(
    ledger_dir: str | Path, repo_presets_dir: str | Path
) -> list[tuple[Path, bytes | None]]:
    """Place the repository copy of every bundled preset the ledger lists.

    A preset step of the wave-10 preset-content change (spec 4 status sets)
    cannot open an old ledger first: the spec-4 loader refuses a stock kind
    without ``statuses:``, and that key lives in the preset copy.  The copies
    are files, not Dolt content, so the step places them before the runner
    opens the ledger; the per-preset counters are bumped by the preset steps
    themselves.

    Returns ``[(path, previous_bytes_or_None), ...]`` so a dry run or a refused
    step can call :func:`restore_files` and leave the ledger byte-identical.
    """
    root = Path(ledger_dir).expanduser().resolve()
    repo = Path(repo_presets_dir).expanduser().resolve()
    config = root / LEDGER_FILE
    if not config.is_file():
        raise UsageError(f"{root} is not a Tetramark ledger (no {LEDGER_FILE})")
    try:
        payload = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as error:
        raise UsageError(f"invalid {config}: {error}") from error
    listed = payload.get("presets") if isinstance(payload, dict) else None
    if not isinstance(listed, list):
        raise UsageError(f"{config} has no 'presets' list")
    names = [
        Path(str(raw)).stem if Path(str(raw)).suffix else Path(str(raw)).name
        for raw in listed
    ]
    saved: list[tuple[Path, bytes | None]] = []
    copies = root / "presets"
    for name in _preset_include_closure(names, repo_presets_dir=repo):
        source = repo / f"{name}.yaml"
        if not source.is_file():
            continue
        target = copies / f"{name}.yaml"
        data = source.read_bytes()
        if target.is_file() and target.read_bytes() == data:
            continue
        previous = target.read_bytes() if target.is_file() else None
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        saved.append((target, previous))
    return saved


def vendor_preset_files(
    ledger_dir: str | Path, sources: Mapping[str, str | Path]
) -> list[tuple[Path, bytes | None]]:
    """Place explicit preset files under ``<ledger>/presets/``.

    A migration step installs the preset version it upgrades **to**, and the
    repository's ``presets/`` only ever holds the *current* version.  A
    historical step therefore ships the target content next to its
    ``migrate.py`` (e.g. ``migration/presets/base/1-to-2/base-2.yaml``) and
    places it here; ``sources`` maps the preset name to that file.  The
    destination is ``<ledger>/presets/<name>.yaml``, exactly where ``tm init``
    writes the copy the ledger resolves.

    Returns the same ``[(path, previous_bytes_or_None), ...]`` shape as
    :func:`vendor_repo_presets`, so :func:`restore_files` undoes it and a dry
    run or refused step leaves the ledger byte-identical.
    """
    root = Path(ledger_dir).expanduser().resolve()
    copies = root / "presets"
    saved: list[tuple[Path, bytes | None]] = []
    for name, source_path in sources.items():
        if not _PRESET_NAME_RE.match(str(name)):
            raise UsageError(f"invalid preset name {name!r} for a frozen step copy")
        source = Path(source_path).expanduser()
        if not source.is_file():
            raise UsageError(f"frozen preset copy {source} does not exist")
        target = copies / f"{name}.yaml"
        data = source.read_bytes()
        if target.is_file() and target.read_bytes() == data:
            continue
        previous = target.read_bytes() if target.is_file() else None
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        saved.append((target, previous))
    return saved


def restore_files(saved: Sequence[tuple[Path, bytes | None]]) -> None:
    """Undo :func:`vendor_repo_presets`/:func:`vendor_preset_files` (back to front)."""
    for target, previous in reversed(list(saved)):
        try:
            if previous is None:
                target.unlink(missing_ok=True)
            else:
                target.write_bytes(previous)
        except OSError:  # pragma: no cover - the report already says what broke
            pass
