"""Ledger lifecycle: ``init_ledger``, ``adopt_ledger``, ``open`` and the write session.

A ledger is one self-contained directory holding a Dolt database (``.dolt/``)
and its configuration (``ledger.yaml``); it lives next to its docs or
repository and every command edits it in place through the embedded driver
(spec §1.1).  Creating one means:

1. ``mkdir <ledger_dir>`` and ``dolt init`` in it;
2. write ``ledger.yaml`` (the human-editable config, copied into the DB);
3. load the effective schema and execute the generated DDL;
4. write ``ledger_meta`` (fingerprint, preset versions, the ``ledger.yaml`` text,
   ``spec_version``, tool provenance);
5. make exactly one Dolt commit, ``init(ledger): <name>``.

:func:`adopt_ledger` formats an **existing** Dolt database in place instead
(spec A4): same steps 2–5 on an empty, clean branch, but no ``mkdir``/``dolt
init``, one ``adopt(ledger): <name>`` commit on top of the existing history and
``adopted_from_commit`` recording the pre-adopt HEAD.  Adopt never deletes
anything, and a failed adopt rolls its own writes back — it must never remove
the database directory the way a failed :func:`init_ledger` removes its own.

A ledger's identity is ``ledger.yaml``'s ``name:``, not its path; the directory
basename is only the Dolt database name (P2).  Resolution (``-l/--ledger PATH``
or the upward walk) lives in :mod:`tetramark.driver.resolution`.

Opening one loads ``ledger.yaml``, the effective schema, the id strategies and
the commit author (``ledger.yaml`` → Git config), then compares the live
fingerprint with the YAML one.  A mismatch is a warning, not an error:
inspecting the ledger is still useful and ``tm schema plan`` is the fix.

Every successful write goes through :meth:`Ledger.write`, which groups the
statements of one command into one SQL transaction and one Dolt commit (spec
§6).  ``dry_run`` collects the SQL instead of executing it; ``no_commit``
executes but leaves the change uncommitted.

One writer per ledger (P11): a write takes an exclusive ``flock`` on
``<ledger>/.dolt/tetramark.lock`` (see :mod:`tetramark.core.writelock`).
:class:`WriteSession` holds it for the transaction and the commit; a CLI write
command takes it before the ledger is opened and holds it for the whole
read -> validate -> write -> commit window.  Reads and ``--dry-run`` never lock.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import warnings
from collections.abc import Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager, suppress
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from types import TracebackType
from typing import Any

import yaml

from tetramark.core.ids import IdStrategy, strategy_from_config
from tetramark.core.objects import (
    AliasOps,
    AliquotOps,
    BoxOps,
    EntityOps,
    StockOps,
)
from tetramark.core.writelock import (
    LockHolder,
    WriteLock,
    write_lock_holder,
)
from tetramark.driver.base import (
    Driver,
    Params,
    ensure_ascii,
    looks_like_error,
    render_sql,
)
from tetramark.driver.embedded import EmbeddedDriver
from tetramark.driver.resolution import (
    DOLT_DIR,
    LEDGER_FILE,
    is_ledger_dir,
    ledger_name,
    refuse_nested,
    resolve_ledger,
)
from tetramark.errors import LedgerError, UnreachableError, UsageError
from tetramark.schema.ddl import Statement, schema_statements
from tetramark.schema.loader import (
    PresetCopy,
    PresetFile,
    default_presets_dir,
    expand_presets,
    load_ledger_schema,
    load_schema,
    preset_copy_files,
    resolve_preset_spec,
)
from tetramark.schema.model import (
    SPEC_VERSION,
    EffectiveSchema,
    LedgerConfig,
    SchemaError,
)
from tetramark.schema.tests_gen import CheckReport, install_tests, run_check

__all__ = [
    "LEDGER_FILE",
    "META_ADOPTED_FROM_COMMIT",
    "META_CREATED_WITH",
    "META_FINGERPRINT",
    "META_LEDGER_YAML",
    "META_PRESET_VERSIONS",
    "META_SPEC_VERSION",
    "META_TETRAMARK_VERSION",
    "AdoptPlan",
    "InitPlan",
    "Ledger",
    "LockHolder",
    "WriteSession",
    "adopt_ledger",
    "build_id_strategies",
    "init_ledger",
    "open",
    "resolve_author",
    "write_lock_holder",
]

# ``LEDGER_FILE`` comes from :mod:`tetramark.driver.resolution` and stays in
# this module's ``__all__``: callers (and the migration scripts) import it here.

#: Keys stored in ``ledger_meta``.  Other tickets (08/09) read and update them.
META_SPEC_VERSION = "spec_version"
META_TETRAMARK_VERSION = "tetramark_version"
META_CREATED_WITH = "created_with"
#: Only ``tm adopt`` writes this key (ticket TM-04); it is deliberately not part
#: of :data:`META_ORDER`, so a plain init never records it.
META_ADOPTED_FROM_COMMIT = "adopted_from_commit"
META_FINGERPRINT = "fingerprint"
META_PRESET_VERSIONS = "preset_versions"
META_LEDGER_YAML = "ledger_yaml"
META_ORDER: tuple[str, ...] = (
    META_SPEC_VERSION,
    META_TETRAMARK_VERSION,
    META_CREATED_WITH,
    META_FINGERPRINT,
    META_PRESET_VERSIONS,
    META_LEDGER_YAML,
)

_LEDGER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_COMMIT_PART_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
_AUTHOR_RE = re.compile(r"^\s*(?P<name>[^<>]*?)\s*<(?P<email>[^<>]+)>\s*$")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _check_name(name: str) -> str:
    """Validate a ledger name.

    A ledger name is one directory (and Dolt database) name: letters, digits,
    ``_``, ``-`` and ``.``, starting with a letter or digit.  ``..`` anywhere,
    a trailing ``.`` and ``/`` are rejected — they would make the name ambiguous
    as a path component or as a Dolt revision (``db/branch``).
    """
    if (
        not isinstance(name, str)
        or not _LEDGER_NAME_RE.match(name)
        or ".." in name
        or name.endswith(".")
    ):
        raise UsageError(
            f"invalid ledger name {name!r}: use letters, digits, '_', '-' or '.' "
            "(starting with a letter or digit); '/' is not allowed, nor '..' "
            "anywhere or a trailing '.'"
        )
    return name


def _git_config(key: str, cwd: Path, env: Mapping[str, str] | None) -> str | None:
    binary = shutil.which("git")
    if binary is None:
        return None
    try:
        process = subprocess.run(
            [binary, "config", "--get", key],
            cwd=cwd,
            env=dict(env) if env is not None else None,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:  # pragma: no cover - git disappeared mid-run
        return None
    if process.returncode != 0:
        return None
    value = process.stdout.strip()
    return value or None


def _git_identity(cwd: Path, env: Mapping[str, str] | None) -> str | None:
    name = _git_config("user.name", cwd, env)
    email = _git_config("user.email", cwd, env)
    if name and email:
        return f"{name} <{email}>"
    return None


def split_author(author: str | None) -> tuple[str | None, str | None]:
    """Split ``"Name <email>"`` into its parts; a bare string is a name."""
    if not author:
        return None, None
    match = _AUTHOR_RE.match(author)
    if match is not None:
        name = match.group("name").strip()
        return (name or None), match.group("email").strip()
    return author.strip() or None, None


def resolve_author(
    config_author: str | None,
    *,
    cwd: Path,
    env: Mapping[str, str] | None = None,
) -> str | None:
    """Resolve the commit author: ``ledger.yaml`` → Git config (P6).

    The workspace tier is gone in 0.5.0: a ledger carries its own author, and a
    ledger copied to another machine falls back to that machine's Git identity.
    """
    if config_author and str(config_author).strip():
        return str(config_author).strip()
    return _git_identity(cwd, env)


def _dolt_identity(
    author: str | None, *, cwd: Path, env: Mapping[str, str] | None
) -> tuple[str, str]:
    """Name and email for the init/adopt commit, filling gaps from Git config."""
    name, email = split_author(author)
    name = name or _git_config("user.name", cwd, env)
    email = email or _git_config("user.email", cwd, env)
    if not name or not email:
        raise UsageError(
            "cannot determine the commit author: pass --author "
            "\"Name <email>\" or configure git user.name/user.email"
        )
    return name, email


def build_id_strategies(config: LedgerConfig) -> dict[str, IdStrategy]:
    """The entity and stock ID strategies configured by ``ledger.yaml``."""
    strategies: dict[str, IdStrategy] = {}
    for object_type in ("entity", "stock"):
        entry = config.id_config(object_type)
        options: dict[str, Any] = {"strategy": entry.strategy}
        if entry.strategy == "datecode":
            options["prefix"] = entry.prefix
            options["reuse"] = entry.reuse
        elif entry.strategy == "sequence":
            options["prefix"] = entry.prefix
            options["width"] = entry.width
        strategies[object_type] = strategy_from_config(options)
    return strategies


def _preset_sources(
    presets: Sequence[str | Path], presets_dir: Path | None
) -> tuple[list[PresetFile], list[PresetCopy]]:
    """Resolve ``--preset`` values and the files that copy them into the ledger.

    Bare names resolve through ``$TETRAMARK_PRESETS_DIR`` then the bundled
    ``presets/`` directory (P8); a path (one containing ``/`` or ending in
    ``.yaml``/``.yml``) works without any preset directory.  The second element
    is the full include closure, so a ledger's ``presets/`` copy is
    self-contained.
    """
    sources: list[str] = []
    for raw in presets:
        text = str(raw).strip()
        if not text:
            raise UsageError("preset names must not be empty")
        sources.append(text)
    top = [
        resolve_preset_spec(source, presets_dir=presets_dir, base_dir=Path.cwd())
        for source in sources
    ]
    names: list[str] = []
    for preset in top:
        if preset.name not in names:
            names.append(preset.name)
    if len(names) != len(top):
        # e.g. ``-p base -p ./other/base.yaml``: both would be "base".
        raise UsageError(
            "the same preset name was given more than once: " + ", ".join(names)
        )

    closure = expand_presets(
        [preset.path for preset in top],
        base_dir=Path.cwd(),
        require_self_contained=True,
    )
    by_name: dict[str, PresetFile] = {}
    for preset in closure:
        existing = by_name.get(preset.name)
        if existing is not None and existing.path != preset.path:
            raise UsageError(
                f"two different presets would be copied as {preset.name!r} "
                f"({existing.path} and {preset.path}); preset names must be "
                "unique within one ledger"
            )
        if existing is None:
            by_name[preset.name] = preset
    copies: list[PresetCopy] = []
    for preset in closure:
        if by_name.get(preset.name) is preset:
            copies.extend(preset_copy_files(preset))
            by_name.pop(preset.name)
    return top, copies


def _write_presets(
    target: Path,
    copies: Sequence[PresetCopy],
    written: list[tuple[Path, bytes | None]] | None = None,
) -> list[tuple[Path, bytes | None]]:
    """Copy the preset closure into ``<ledger>/presets/``.

    Each destination is appended to ``written`` *before* it is written, so a
    failure midway still leaves :func:`_restore_presets` the exact list of files
    this attempt touched (the ticket-20 adopt-rollback promise).  Each entry is
    ``(path, previous bytes)`` — ``None`` when the file did not exist.
    """
    root = target / "presets"
    root.mkdir(parents=True, exist_ok=True)
    written = written if written is not None else []
    resolved_root = root.resolve()
    for copy in copies:
        destination = root / copy.destination
        if (
            copy.destination.is_absolute()
            or ".." in copy.destination.parts
            or not copy.destination.parts
        ):
            raise UsageError(
                f"refusing to write preset {copy.name!r} outside {root}: "
                f"invalid destination {str(copy.destination)!r}"
            )
        if not destination.resolve().is_relative_to(resolved_root):
            raise UsageError(
                f"refusing to write preset {copy.name!r} outside {root}: "
                f"{destination} resolves outside the ledger's presets directory"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        previous = destination.read_bytes() if destination.is_file() else None
        written.append((destination, previous))
        shutil.copyfile(copy.source, destination)
    return written


def _verify_preset_copy(target: Path, expected: EffectiveSchema) -> None:
    """Prove the ledger reopens from its own ``<ledger>/presets/`` copy.

    ``tm init`` promises a self-contained ledger: after writing the copies and
    ``ledger.yaml`` (and before any database work, hence before the commit), the
    ledger must reload from itself with exactly the planned schema.  Unsupported
    include shapes (a path include, an include name that differs from the
    included preset's own name) are rejected earlier; this is the safety net
    that also catches anything the copy logic missed.
    """
    presets_dir = target / "presets"
    try:
        _config, reloaded = load_ledger_schema(target)
    except SchemaError as error:
        raise LedgerError(
            f"the presets copied into {presets_dir} do not reopen from the ledger "
            f"itself: {error.message}. 'include:' must list bare preset names "
            "matching each included preset's own 'preset:' field so that the "
            "ledger's presets/ copy is self-contained; fix the preset and re-run"
        ) from error
    if (
        reloaded.fingerprint() != expected.fingerprint()
        or reloaded.preset_versions != expected.preset_versions
    ):
        raise LedgerError(
            f"the presets copied into {presets_dir} do not reproduce the planned "
            f"schema (planned fingerprint {expected.fingerprint()}, reloaded "
            f"{reloaded.fingerprint()}); the copy is not self-contained"
        )


def _restore_presets(
    written: Sequence[tuple[Path, bytes | None]],
    *,
    presets_root: Path | None = None,
) -> None:
    """Undo :func:`_write_presets` after a failed adopt (best effort).

    Restores (or removes) every recorded file and prunes the directories the
    copy created; with ``presets_root`` the now-empty root is pruned too (the
    caller passes it only when this attempt created that directory).
    """
    directories: set[Path] = set()
    for destination, previous in reversed(written):
        if previous is None:
            destination.unlink(missing_ok=True)
        else:
            destination.write_bytes(previous)
        directory = destination.parent
        directories.add(directory)
        if presets_root is not None:
            while directory != presets_root and directory.parent != directory:
                directory = directory.parent
                directories.add(directory)
                if directory == presets_root:
                    break
    for directory in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        with suppress(OSError):
            directory.rmdir()


def _ledger_yaml(
    *,
    name: str,
    presets: Sequence[str],
    id_prefix: str | None,
    author: str,
) -> tuple[dict[str, Any], str]:
    ids: dict[str, dict[str, Any]] = {"entity": {"strategy": "uuid"}}
    if id_prefix is None or not str(id_prefix).strip():
        # datecode needs a non-empty prefix (spec §6.1); until one is given the
        # simplest consistent choice is UUID stock ids.
        ids["stock"] = {"strategy": "uuid"}
    else:
        ids["stock"] = {"strategy": "datecode", "prefix": str(id_prefix).strip()}

    payload: dict[str, Any] = {"name": name, "presets": list(presets)}
    payload["author"] = author
    payload["ids"] = ids
    text = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
    return payload, text


def _run_dolt_init(
    path: Path, name: str, email: str, env: Mapping[str, str] | None
) -> None:
    binary = shutil.which("dolt")
    if binary is None:
        raise UnreachableError("the 'dolt' binary was not found on PATH")
    process = subprocess.run(
        [binary, "init", f"--name={name}", f"--email={email}"],
        cwd=path,
        env=dict(env) if env is not None else None,
        capture_output=True,
        text=True,
        check=False,
    )
    output = "\n".join(
        part for part in (process.stdout.strip(), process.stderr.strip()) if part
    )
    # Dolt 2.3.5 exits 0 even when it refuses to initialise (e.g. no author).
    if process.returncode != 0 or looks_like_error(output) or not (path / ".dolt").is_dir():
        raise LedgerError(f"dolt init failed in {path}:\n{output or '(no output)'}")


# ---------------------------------------------------------------------------
# Init
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InitPlan:
    """Everything ``init_ledger`` would do, computed without touching disk."""

    path: Path
    name: str
    config: LedgerConfig
    schema: EffectiveSchema
    ledger_yaml: str
    statements: list[Statement]
    author: str
    preset_copies: tuple[PresetCopy, ...]
    #: Environment override for the ``dolt`` subprocesses (tests/embedding only).
    env: Mapping[str, str] | None

    @property
    def sql(self) -> str:
        """The generated DDL as one SQL script."""
        return ";\n".join(statement.sql for statement in self.statements) + ";"


@dataclass(frozen=True)
class AdoptPlan(InitPlan):
    """Everything ``adopt_ledger`` would do, computed without writing anything.

    ``branch`` is the branch the adoption happens on and
    ``adopted_from_commit`` the branch HEAD before the adoption commit — the
    value the eventual ``ledger_meta`` row records.
    """

    branch: str
    adopted_from_commit: str


def _ledger_plan(
    path: str | Path,
    name: str,
    presets: Sequence[str | Path] | str | Path | None,
    *,
    id_prefix: str | None,
    author: str | None,
    env: Mapping[str, str] | None,
) -> InitPlan:
    """Build the configuration, schema and DDL of a new ledger (no disk writes).

    Every validation that does not need the database happens here, so a bad
    preset or a missing author fails before a directory is created or (for
    adopt) before the existing database is touched.
    """
    target = Path(path).expanduser().resolve()

    if presets is None:
        requested: list[str | Path] = []
    elif isinstance(presets, (str, Path)):
        requested = [presets]
    else:
        requested = list(presets)
    if not requested:
        raise UsageError(
            "a ledger needs at least one preset: pass --preset NAME (or a preset path)"
        )
    preset_files, preset_copies = _preset_sources(
        requested, default_presets_dir(env=env)
    )
    preset_names = [preset.name for preset in preset_files]

    identity_cwd = target.parent if target.parent.is_dir() else Path.cwd()
    resolved_author = resolve_author(author, cwd=identity_cwd, env=env)
    identity_name, identity_email = _dolt_identity(
        resolved_author, cwd=identity_cwd, env=env
    )
    canonical_author = f"{identity_name} <{identity_email}>"

    payload, text = _ledger_yaml(
        name=name,
        presets=preset_names,
        id_prefix=id_prefix,
        author=canonical_author,
    )
    try:
        config = LedgerConfig(**payload)
    except Exception as error:  # pydantic ValidationError, rephrased below
        raise UsageError(f"invalid ledger configuration: {error}") from error

    # Fail before creating directories: a bad preset must not leave junk behind.
    build_id_strategies(config)
    # The schema is loaded from the sources before anything is copied; the
    # ledger records the bare names and reopens from its own presets/ copy.
    schema_config = config.model_copy(
        update={"presets": [str(preset.path) for preset in preset_files]}
    )
    schema = load_schema(schema_config, base_dir=target)

    return InitPlan(
        path=target,
        name=name,
        config=config,
        schema=schema,
        ledger_yaml=text,
        statements=schema_statements(schema),
        author=canonical_author,
        preset_copies=tuple(preset_copies),
        env=env,
    )


def _existing_target_error(target: Path) -> str | None:
    """Why ``target`` cannot become a new ledger, or ``None`` when it can.

    A new ledger needs a path that does not exist (parents are created) or an
    existing **empty** directory.  Everything else gets the hint that fits what
    is already there, so ``tm init`` never masks an already-formatted ledger or
    a bare 0.4 database behind a generic "already exists" (R2/P9).
    """
    if not target.exists():
        return None
    if not target.is_dir():
        return (
            f"cannot create a ledger at {target}: the path already exists and is "
            "not a directory; pass a directory to create"
        )
    if is_ledger_dir(target):
        try:
            existing = ledger_name(target)
        except UsageError:  # a broken ledger.yaml is still a ledger directory
            existing = target.name
        return (
            f"cannot create a ledger at {target}: it is already the ledger "
            f"{existing!r}. Use it directly ('tm --ledger {target} ...' or cd "
            "into it), or choose another directory"
        )
    if (target / DOLT_DIR).is_dir():
        return (
            f"cannot create a ledger at {target}: the directory already holds a "
            f"bare Dolt database ({DOLT_DIR}/) but no {LEDGER_FILE}. Format it in "
            f"place with 'tm adopt {target}' instead"
        )
    if any(target.iterdir()):
        return (
            f"cannot create a ledger at {target}: the path already exists and is "
            "not an empty directory; choose a new path (an existing empty "
            "directory is allowed)"
        )
    return None


def _prepare_init(
    path: str | Path,
    name: str | None,
    presets: Sequence[str | Path] | str | Path | None,
    *,
    id_prefix: str | None,
    author: str | None,
    env: Mapping[str, str] | None,
) -> InitPlan:
    target = Path(path).expanduser().resolve()
    # Validate the requested identity first: a path-like name ("..") must never
    # be turned into a target path before it is rejected.
    ledger = _check_name(name or target.name)
    # P5: a ledger is one self-contained directory; never create one inside one.
    refuse_nested(target)
    problem = _existing_target_error(target)
    if problem is not None:
        raise LedgerError(problem)

    return _ledger_plan(
        target,
        ledger,
        presets,
        id_prefix=id_prefix,
        author=author,
        env=env,
    )


def _prepare_adopt(
    path: str | Path,
    name: str | None,
    presets: Sequence[str | Path] | str | Path | None,
    *,
    id_prefix: str | None,
    author: str | None,
    force_config: bool,
    env: Mapping[str, str] | None,
) -> InitPlan:
    """Validation of the adopt target that needs no database connection.

    The existing database is judged by its ``.dolt`` directory and its
    ``ledger.yaml``; the schema and working-set preconditions need SQL and are
    checked by :func:`_check_adoptable`.
    """
    target = Path(path).expanduser().resolve()
    # Validate the requested identity first: a path-like name ("..") must never
    # be turned into a target path before it is rejected.
    ledger = _check_name(name or target.name)

    if not (target / DOLT_DIR).is_dir():
        if target.exists():
            raise LedgerError(
                f"cannot adopt {target}: it is not a Dolt database (no "
                f"{DOLT_DIR}/ directory); use 'tm init {target}' to create a new "
                "ledger"
            )
        raise LedgerError(
            f"cannot adopt {target}: there is no Dolt database at that path; "
            f"create a new ledger with 'tm init {target}' instead"
        )
    refuse_nested(target)
    config_path = target / LEDGER_FILE
    if config_path.is_file() and not force_config:
        try:
            existing = ledger_name(target)
        except UsageError:  # an unreadable ledger.yaml is still in the way
            existing = None
        state = (
            f"the ledger {existing!r}"
            if existing is not None
            else f"already formatted ({LEDGER_FILE} exists)"
        )
        raise LedgerError(
            f"cannot adopt {ledger!r}: {target} is {state}; there is nothing to "
            "adopt. Pass --force-config to overwrite a ledger.yaml left on an "
            "otherwise empty database"
        )

    return _ledger_plan(
        target,
        ledger,
        presets,
        id_prefix=id_prefix,
        author=author,
        env=env,
    )


def _write_meta(
    driver: Driver,
    schema: EffectiveSchema,
    ledger_yaml: str,
    *,
    adopted_from_commit: str | None = None,
) -> None:
    # Imported here because ``tetramark/__init__`` imports this module; the
    # package is fully initialised by the time a ledger is written.
    from tetramark import __version__

    values = {
        META_SPEC_VERSION: str(SPEC_VERSION),
        # Provenance only: which tool last wrote meta and which first
        # formatted the ledger.
        META_TETRAMARK_VERSION: __version__,
        META_CREATED_WITH: __version__,
        META_FINGERPRINT: schema.fingerprint(),
        META_PRESET_VERSIONS: json.dumps(
            schema.preset_versions, sort_keys=True, ensure_ascii=False
        ),
        META_LEDGER_YAML: ledger_yaml,
    }
    for key in META_ORDER:
        driver.execute(
            "INSERT INTO `ledger_meta` (`key`, `value`) VALUES (%s, %s)",
            (key, values[key]),
        )
    # Only the adopt path records the pre-adoption HEAD (ticket TM-04); a plain
    # init has no history to adopt, so the key stays absent.
    if adopted_from_commit is not None:
        driver.execute(
            "INSERT INTO `ledger_meta` (`key`, `value`) VALUES (%s, %s)",
            (META_ADOPTED_FROM_COMMIT, adopted_from_commit),
        )


def init_ledger(
    path: str | Path,
    name: str | None = None,
    presets: Sequence[str | Path] | str | Path | None = None,
    *,
    id_prefix: str | None = None,
    author: str | None = None,
    dry_run: bool = False,
    env: Mapping[str, str] | None = None,
) -> Ledger | InitPlan:
    """Create one standalone ledger directory (spec §1.1, §3; R2).

    ``path`` is the ledger directory to create (``.dolt/`` and ``ledger.yaml``
    end up in it); missing parents are created and an existing **empty**
    directory is used as-is.  ``name`` is the ledger's identity in
    ``ledger.yaml`` and defaults to the directory basename (P2/R2); the basename
    is also the Dolt database name.  An existing ledger, a bare Dolt database
    (``.dolt/`` without ``ledger.yaml``) and a non-empty directory are refused
    with the hint that fits (a bare database points at ``adopt_ledger``).  Every
    enclosing directory is checked first: ledgers do not nest (P5).

    Returns the opened :class:`Ledger`; with ``dry_run=True`` returns an
    :class:`InitPlan` and writes nothing.  ``env`` overrides the environment of
    the ``dolt`` subprocesses (tests/embedding only).
    """
    plan = _prepare_init(
        path,
        name,
        presets,
        id_prefix=id_prefix,
        author=author,
        env=env,
    )
    if dry_run:
        return plan

    target = plan.path
    # An existing empty directory is a valid target; a fresh path is created
    # together with its parents.
    pre_existed = target.exists()
    target.mkdir(parents=True, exist_ok=pre_existed)
    try:
        identity_name, identity_email = split_author(plan.author)
        assert identity_name and identity_email  # _dolt_identity checked this
        _run_dolt_init(target, identity_name, identity_email, plan.env)

        # The ledger is self-contained: its presets live in <ledger>/presets/.
        _write_presets(target, plan.preset_copies)
        (target / LEDGER_FILE).write_text(plan.ledger_yaml, encoding="utf-8")
        _verify_preset_copy(target, plan.schema)

        driver = EmbeddedDriver(target, env=plan.env)
        with driver.transaction():
            for statement in plan.statements:
                driver.execute(statement.sql)
            _write_meta(driver, plan.schema, plan.ledger_yaml)
            # Generated dolt_tests are derived state (spec §4): install them
            # with the schema so a fresh ledger is immediately checkable.
            install_tests(driver, plan.schema)
        driver.commit(f"init(ledger): {plan.name}", author=plan.author)
    except BaseException:
        # A half-initialised ledger is worse than none: never leave one behind.
        # A pre-existing empty directory stays, minus the artifacts this attempt
        # wrote (anything else in it appeared concurrently and is not ours).
        if pre_existed:
            for name in (DOLT_DIR, "presets", LEDGER_FILE):
                artifact = target / name
                if artifact.is_dir():
                    shutil.rmtree(artifact, ignore_errors=True)
                else:
                    with suppress(OSError):
                        artifact.unlink()
        else:
            shutil.rmtree(target, ignore_errors=True)
        raise

    ledger = Ledger(
        path=plan.path,
        name=plan.name,
        driver=driver,
        config=plan.config,
        schema=plan.schema,
        author=plan.author,
    )
    ledger.refresh_meta()
    return ledger


# ---------------------------------------------------------------------------
# Adopt (spec A4)
# ---------------------------------------------------------------------------


def _head_hash(driver: Driver) -> str:
    """The commit hash ``HEAD`` points to on the driver's branch."""
    rows = driver.query("SELECT DOLT_HASHOF('HEAD') AS `hash`")
    value = rows[0].get("hash") if rows else None
    if value is None:
        raise LedgerError("cannot read HEAD of the database (DOLT_HASHOF is empty)")
    return str(value)


def _active_branch(driver: Driver) -> str:
    """The branch the driver's session reads and writes."""
    rows = driver.query("SELECT active_branch() AS `branch`")
    value = rows[0].get("branch") if rows else None
    if value is None:
        raise LedgerError("cannot determine the database's current branch")
    return str(value)


def _name_list(names: Sequence[str], *, limit: int = 5) -> str:
    """``(a, b, c, ... 3 more)`` — never an unbounded list in an error."""
    shown = list(names[:limit])
    text = ", ".join(shown)
    if len(names) > limit:
        text += f", ... {len(names) - limit} more"
    return f"({text})"


def _check_adoptable(driver: Driver, name: str) -> None:
    """Refuse to adopt a database that is not empty, clean and unformatted.

    Read-only: every refusal happens before the first write (spec A4).  The
    checks are: clean working set, no user tables and no views, no
    ``dolt_tests`` rows.  ``ledger.yaml`` is checked without a connection.
    """
    dirty = driver.query("SELECT `table_name` AS `name` FROM dolt_status")
    if dirty:
        changed = sorted({str(row.get("name")) for row in dirty})
        raise LedgerError(
            f"cannot adopt {name!r}: the working set is not clean "
            f"({len(dirty)} uncommitted change(s) in {_name_list(changed)}); "
            "commit or reset them first"
        )

    rows = driver.query(
        "SELECT `table_name` AS `name`, `table_type` AS `kind` "
        "FROM information_schema.tables WHERE table_schema = DATABASE()"
    )
    tables = sorted(
        str(row["name"])
        for row in rows
        if str(row["kind"]).upper() == "BASE TABLE"
        and not str(row["name"]).startswith("dolt_")
    )
    views = sorted(
        str(row["name"]) for row in rows if str(row["kind"]).upper() == "VIEW"
    )
    if tables or views:
        found: list[str] = []
        if tables:
            found.append(f"{len(tables)} user table(s) {_name_list(tables)}")
        if views:
            found.append(f"{len(views)} view(s) {_name_list(views)}")
        raise LedgerError(
            f"cannot adopt {name!r}: the schema is not empty — "
            f"{'; '.join(found)}; drop the legacy schema as its own commit "
            "first (adopt never deletes anything)"
        )

    rows = driver.query("SELECT COUNT(*) AS `n` FROM `dolt_tests`")
    count = int(rows[0]["n"]) if rows else 0
    if count:
        raise LedgerError(
            f"cannot adopt {name!r}: `dolt_tests` still has {count} row(s); "
            "delete them before adopting"
        )


def _rollback_statements(statements: Sequence[Statement]) -> list[str]:
    """SQL undoing a (possibly partial) application of ``schema_statements``.

    Foreign keys are dropped first because ``stock``/``aliquot`` reference each
    other, so no table order can satisfy both; then views (which may depend on
    any table); then tables in reverse creation order.
    """

    def ident(value: str) -> str:
        return "`" + value.replace("`", "``") + "`"

    sql: list[str] = [
        f"ALTER TABLE {ident(str(statement.table))} "
        f"DROP FOREIGN KEY {ident(statement.name)}"
        for statement in reversed(statements)
        if statement.object == "fk" and statement.table
    ]
    for statement in reversed(statements):
        if statement.object == "view":
            sql.append(f"DROP VIEW IF EXISTS {ident(statement.name)}")
        elif statement.object == "table":
            sql.append(f"DROP TABLE IF EXISTS {ident(statement.name)}")
    return sql


def _missing_object_error(error: BaseException) -> bool:
    """True when DDL failed only because the object does not exist (yet)."""
    text = str(error).lower()
    markers = (
        "table not found",
        "doesn't exist",
        "does not exist",
        "check that column/key exists",
        "check that table",
        "unknown table",
    )
    return any(marker in text for marker in markers)


def _rollback_adopt(
    path: Path,
    name: str,
    *,
    branch: str | None,
    pre_head: str,
    statements: Sequence[Statement],
    config_path: Path,
    previous_config: str | None,
    preset_writes: Sequence[tuple[Path, bytes | None]] = (),
    presets_root: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> None:
    """Best-effort undo of a failed adopt.  Never deletes the database.

    The directory and its history are untouched: only the objects this attempt
    created are dropped (Dolt's ``reset --hard`` leaves untracked tables
    behind), then the branch is reset to ``pre_head``.  A pre-existing
    ``ledger.yaml`` and any overwritten ``presets/`` files are restored.
    Failures here must not mask the original error, hence the warnings instead
    of nested exceptions.
    """
    failed: list[str] = []
    driver: Driver | None = None
    try:
        driver = EmbeddedDriver(path, branch=branch, env=env)
    except Exception as error:  # pragma: no cover - only if the database vanished
        failed.append(f"could not reconnect: {error}")
    if driver is not None:
        try:
            for sql in _rollback_statements(statements):
                try:
                    driver.execute(sql)
                except Exception as error:
                    # A missing object is fine: this attempt never created it
                    # (e.g. the failure happened before the DDL, or only part of
                    # it ran).  Dropping a non-existent FK/table/view must not be
                    # reported as an incomplete rollback.
                    if _missing_object_error(error):
                        continue
                    failed.append(f"{sql.split()[0].lower()} failed: {error}")
            driver.execute("CALL DOLT_RESET('--hard', %s)", (pre_head,))
        except Exception as error:  # pragma: no cover - defensive
            failed.append(f"reset failed: {error}")
        finally:
            with suppress(Exception):
                _close_driver(driver)
    try:
        if previous_config is None:
            config_path.unlink(missing_ok=True)
        else:
            config_path.write_text(previous_config, encoding="utf-8")
    except OSError as error:  # pragma: no cover - defensive
        failed.append(f"could not restore {config_path}: {error}")
    try:
        _restore_presets(preset_writes, presets_root=presets_root)
    except OSError as error:  # pragma: no cover - defensive
        failed.append(f"could not restore presets/: {error}")
    if failed:
        warnings.warn(
            f"adopt rollback of {name!r} is incomplete: {'; '.join(failed)}",
            UserWarning,
            stacklevel=2,
        )


def adopt_ledger(
    path: str | Path,
    name: str | None = None,
    presets: Sequence[str | Path] | str | Path | None = None,
    *,
    id_prefix: str | None = None,
    author: str | None = None,
    force_config: bool = False,
    branch: str | None = None,
    dry_run: bool = False,
    env: Mapping[str, str] | None = None,
) -> Ledger | AdoptPlan:
    """Format an existing, empty Dolt database in place (spec A4; P9).

    ``path`` is the existing database directory; ``name`` is the identity
    written to ``ledger.yaml`` and defaults to the directory basename (the Dolt
    database name).  This is ``init`` steps 2–5 (``ledger.yaml``, core and kind
    tables, ``ledger_meta``, generated ``dolt_tests``) applied to a database
    that already exists, skipping ``mkdir``/``dolt init``.  It commits
    ``adopt(ledger): <name>`` on top of the existing history and records the
    pre-adopt HEAD in ``ledger_meta.adopted_from_commit``.

    Preconditions (a refusal writes nothing): the selected branch's working set
    is clean, it has no user tables, no views and no ``dolt_tests`` rows, and
    the database has no ``ledger.yaml`` unless ``force_config`` is set.  An
    enclosing ledger directory is refused (P5).  ``branch`` selects the branch
    (default: the checked-out one); a missing branch is an error, never created.

    A failure rolls this attempt's writes back and resets the branch to the
    pre-adopt HEAD; the database directory is never removed (unlike a failed
    :func:`init_ledger`).  Adopt itself never drops an existing object: the
    legacy-schema drop is a separate, auditable commit.

    Returns the opened :class:`Ledger`; with ``dry_run=True`` returns an
    :class:`AdoptPlan` (after running the read-only preconditions) and writes
    nothing.
    """
    plan = _prepare_adopt(
        path,
        name,
        presets,
        id_prefix=id_prefix,
        author=author,
        force_config=force_config,
        env=env,
    )
    target = plan.path

    driver = EmbeddedDriver(target, branch=branch, env=plan.env)
    try:
        _check_adoptable(driver, plan.name)
        selected = branch if branch is not None else _active_branch(driver)
        # Read before any DDL: this is the hash the meta key records, never the
        # hash of the adoption commit.
        pre_head = _head_hash(driver)
    except BaseException:
        _close_driver(driver)
        raise

    if dry_run:
        _close_driver(driver)
        return AdoptPlan(
            path=plan.path,
            name=plan.name,
            config=plan.config,
            schema=plan.schema,
            ledger_yaml=plan.ledger_yaml,
            statements=plan.statements,
            author=plan.author,
            preset_copies=plan.preset_copies,
            env=plan.env,
            branch=selected,
            adopted_from_commit=pre_head,
        )

    config_path = target / LEDGER_FILE
    previous_config = (
        config_path.read_text(encoding="utf-8") if config_path.is_file() else None
    )
    preset_writes: list[tuple[Path, bytes | None]] = []
    presets_dir = target / "presets"
    presets_root_existed = presets_dir.exists()
    try:
        # The ledger is self-contained: its presets live in <ledger>/presets/.
        # The mutable list is filled as each file is written, so the rollback
        # can undo a half-finished copy too (ticket-20 F6).
        _write_presets(target, plan.preset_copies, preset_writes)
        config_path.write_text(plan.ledger_yaml, encoding="utf-8")
        _verify_preset_copy(target, plan.schema)
        with driver.transaction():
            for statement in plan.statements:
                driver.execute(statement.sql)
            _write_meta(
                driver,
                plan.schema,
                plan.ledger_yaml,
                adopted_from_commit=pre_head,
            )
            # Generated dolt_tests are derived state (spec §4): the adopted
            # database is immediately checkable, like a freshly initialised one.
            install_tests(driver, plan.schema)
        driver.commit(f"adopt(ledger): {plan.name}", author=plan.author)
    except BaseException:
        # Never rmtree here: this is the production database.  Drop what this
        # attempt created and reset the branch to where it was.
        _rollback_adopt(
            plan.path,
            plan.name,
            branch=branch,
            pre_head=pre_head,
            statements=plan.statements,
            config_path=config_path,
            previous_config=previous_config,
            preset_writes=preset_writes,
            presets_root=None if presets_root_existed else presets_dir,
            env=plan.env,
        )
        raise

    ledger = Ledger(
        path=plan.path,
        name=plan.name,
        driver=driver,
        config=plan.config,
        schema=plan.schema,
        author=plan.author,
        branch=branch,
    )
    ledger.refresh_meta()
    return ledger


# ---------------------------------------------------------------------------
# Open
# ---------------------------------------------------------------------------


def open(
    path: str | Path | None = None,
    branch: str | None = None,
    *,
    dry_run: bool = False,
    no_commit: bool = False,
    allow_legacy_statuses: bool = False,
    env: Mapping[str, str] | None = None,
) -> Ledger:
    """Open an existing standalone ledger through the embedded driver (spec §6).

    ``path`` is the ledger directory (P15).  When omitted it is resolved from
    the current directory: the nearest directory holding both ``.dolt/`` and
    ``ledger.yaml`` (P3), never an environment variable.  The ledger's identity
    is ``ledger.yaml``'s ``name:`` (P2); the directory basename is only the Dolt
    database name.

    Every command runs ``dolt sql`` in the ledger directory: Tetramark neither
    requires nor manages a ``dolt sql-server``.  If the user runs one on that
    directory anyway, ``dolt sql`` auto-routes through it; that is Dolt's
    behaviour, not something Tetramark configures.

    ``branch`` selects the branch every read, write and commit of this command
    happens on.  It defaults to ``branch:`` in ``ledger.yaml`` and otherwise to
    the branch checked out in the repository.  Selection is session-scoped (a
    Dolt ``db/branch`` revision database), so other sessions and the server
    keep seeing the checked-out branch.  A missing branch is an error; it is
    never created implicitly.  ``env`` overrides the environment of the
    ``dolt`` subprocesses (tests/embedding only).
    """
    ref = resolve_ledger(path)
    ledger_path = ref.path

    # The configuration is needed to resolve the default branch, so read it
    # before opening a connection.
    config, schema = load_ledger_schema(
        ledger_path, allow_legacy_statuses=allow_legacy_statuses
    )
    selected = branch if branch is not None else (config.branch or None)
    driver = EmbeddedDriver(ledger_path, branch=selected, env=env)
    try:
        author = resolve_author(config.author, cwd=ledger_path, env=env)
        result = Ledger(
            path=ledger_path,
            name=config.name,
            driver=driver,
            config=config,
            schema=schema,
            author=author,
            branch=selected,
            dry_run=dry_run,
            no_commit=no_commit,
        )
        result.refresh_meta()
        result.check_fingerprint()
    except BaseException:
        _close_driver(driver)
        raise
    return result


def _close_driver(driver: Driver) -> None:
    close = getattr(driver, "close", None)
    if callable(close):
        close()


# ---------------------------------------------------------------------------
# The ledger object
# ---------------------------------------------------------------------------


class Ledger:
    """One opened ledger: directory, driver, config, schema, ids and author."""

    def __init__(
        self,
        *,
        path: Path,
        name: str,
        driver: Driver,
        config: LedgerConfig,
        schema: EffectiveSchema,
        author: str | None = None,
        branch: str | None = None,
        dry_run: bool = False,
        no_commit: bool = False,
    ) -> None:
        #: The ledger directory: ``.dolt/`` plus ``ledger.yaml``.
        self.path = Path(path)
        self.name = name
        self.driver = driver
        self.config = config
        self.schema = schema
        self.author = author
        #: Branch this ledger session reads and writes (``None`` = checked out).
        self.branch = branch
        self.dry_run = dry_run
        self.no_commit = no_commit
        self.ids = build_id_strategies(config)
        self.meta: dict[str, str] = {}
        #: Object-operation facades (spec §6): entities, stocks, aliquots,
        #: boxes and aliases.  The CLI is a thin layer over these.
        self.entities = EntityOps(self)
        self.stocks = StockOps(self)
        self.aliquots = AliquotOps(self)
        self.boxes = BoxOps(self)
        self.aliases = AliasOps(self)
        #: The write session of the most recent command, so a CLI ``--dry-run``
        #: can print the SQL the command would have run.
        self.last_write: WriteSession | None = None
        #: Fingerprint warnings collected while opening (also emitted via
        #: :func:`warnings.warn`).  Useful for the CLI's non-fatal notices.
        self.warnings: list[str] = []
        #: The per-ledger writer lock (P11); created on first use.
        self._write_lock: WriteLock | None = None
        #: Re-entrancy depth of :meth:`hold_write_lock` on this object.
        self._write_lock_depth = 0

    # -- identity ---------------------------------------------------------
    @property
    def database(self) -> str:
        return self.driver.database

    @property
    def fingerprint(self) -> str:
        return self.schema.fingerprint()

    @property
    def actor(self) -> str | None:
        """The commit author's name, recorded in ``aliquot_event.actor``."""
        name, _email = split_author(self.author)
        return name[:255] if name else None

    def id_strategy(self, object_type: str) -> IdStrategy:
        try:
            return self.ids[object_type]
        except KeyError:
            raise UsageError(
                f"no id strategy for {object_type!r} (expected entity or stock)"
            ) from None

    def next_id(self, object_type: str, *, today: date | None = None) -> str:
        """Allocate the next entity/stock id according to this ledger's config."""
        table = "entity" if object_type == "entity" else "stock"
        strategy = self.id_strategy(object_type)

        def existing(prefix: str) -> list[str]:
            rows = self.driver.query(
                f"SELECT `id` FROM `{table}` WHERE `id` LIKE %s", (f"{prefix}%",)
            )
            return [str(row["id"]) for row in rows]

        return strategy.next_id(existing, today or date.today())

    # -- ledger_meta ------------------------------------------------------
    def read_meta(self) -> dict[str, str]:
        """Read every ``ledger_meta`` row into :attr:`meta`."""
        try:
            rows = self.driver.query(
                "SELECT `key`, `value` FROM `ledger_meta` ORDER BY `key`"
            )
        except LedgerError:
            self.meta = {}
        else:
            self.meta = {str(row["key"]): str(row["value"]) for row in rows}
        return self.meta

    #: Alias kept short for callers.
    refresh_meta = read_meta

    def check_fingerprint(self, *, warn: bool = True) -> bool:
        """Compare the live fingerprint with the YAML one (spec §5)."""
        expected = self.fingerprint
        live = self.meta.get(META_FINGERPRINT)
        if live == expected:
            return True
        message = (
            f"ledger {self.name!r}: the live schema fingerprint does not match "
            f"{LEDGER_FILE} (live: {live or 'missing'}, expected: {expected}); "
            "run 'tm schema plan' to see the pending changes"
        )
        if warn:
            warnings.warn(message, UserWarning, stacklevel=2)
            self.warnings.append(message)
        return False

    # -- validation -------------------------------------------------------
    def check(
        self, *, group: str | None = None, verbose: bool = False
    ) -> CheckReport:
        """Run the generated ``dolt_tests`` (spec §4)."""
        return run_check(self.driver, self.schema, group=group, verbose=verbose)

    # -- reads ------------------------------------------------------------
    def find(self, query: str) -> list[dict[str, Any]]:
        """Search ids, aliases, names and kind string/text fields (ticket 11)."""
        from tetramark.ops.find import find

        return find(self, query)

    def history(self, ref: str) -> dict[str, Any]:
        """Aliquot events plus Dolt commits touching one object (ticket 11)."""
        from tetramark.ops.history import history

        return history(self, ref)

    # -- configuration ----------------------------------------------------
    def set_author(self, author: str) -> WriteSession:
        """Set the ledger's commit author (``tm config set author``).

        The new author goes into ``ledger.yaml``, into the ``ledger_yaml`` copy
        stored in ``ledger_meta``, and into :attr:`author` so the commit that
        records the change is made by the new identity.  Returns the (closed)
        :class:`WriteSession` so callers can print its SQL on a dry run.
        """
        text = str(author).strip()
        name, email = split_author(text)
        if not name or not email:
            raise UsageError(
                f"invalid author {author!r}: use the form \"Name <email>\""
            )

        config_path = self.path / LEDGER_FILE
        try:
            payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as error:
            raise UsageError(f"invalid {config_path}: {error}") from error
        if not isinstance(payload, dict):  # pragma: no cover - defensive
            raise UsageError(f"invalid {config_path}: expected a mapping")
        payload["author"] = text
        new_yaml = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)

        previous_author = self.author
        previous_config_author = self.config.author
        self.author = text
        self.config.author = text
        try:
            with self.write("set author", verb="config", object="ledger") as session:
                exists = session.query(
                    "SELECT 1 AS `present` FROM `ledger_meta` WHERE `key` = %s",
                    (META_LEDGER_YAML,),
                )
                if exists:
                    session.execute(
                        "UPDATE `ledger_meta` SET `value` = %s WHERE `key` = %s",
                        (new_yaml, META_LEDGER_YAML),
                    )
                else:
                    session.execute(
                        "INSERT INTO `ledger_meta` (`key`, `value`) VALUES (%s, %s)",
                        (META_LEDGER_YAML, new_yaml),
                    )
        except BaseException:
            self.author = previous_author
            self.config.author = previous_config_author
            raise

        if session.dry_run:
            self.author = previous_author
            self.config.author = previous_config_author
            return session
        config_path.write_text(new_yaml, encoding="utf-8")
        self.meta[META_LEDGER_YAML] = new_yaml
        return session

    # -- writes -----------------------------------------------------------
    def write(
        self,
        summary: str,
        *,
        verb: str = "update",
        object: str = "ledger",
        dry_run: bool | None = None,
        no_commit: bool | None = None,
    ) -> WriteSession:
        """One command = one transaction + one Dolt commit (spec §6).

        >>> with ledger.write("add tube", verb="add", object="aliquot") as session:
        ...     session.execute("INSERT ...", (1,))
        """
        if not _COMMIT_PART_RE.match(verb):
            raise UsageError(f"invalid commit verb {verb!r} (use e.g. 'add')")
        if not _COMMIT_PART_RE.match(object):
            raise UsageError(f"invalid commit object {object!r} (use e.g. 'aliquot')")
        message = f"{verb}({object}): {summary}"
        ensure_ascii(message)
        return WriteSession(
            self,
            message,
            dry_run=self.dry_run if dry_run is None else dry_run,
            no_commit=self.no_commit if no_commit is None else no_commit,
        )

    # -- writer lock (P11) ------------------------------------------------
    def hold_write_lock(
        self,
        *,
        command: str | None = None,
        timeout: float | None = None,
    ) -> AbstractContextManager[None]:
        """Hold the per-ledger writer lock for a read -> write -> commit window.

        ``command`` is the short label written to the lock file (the CLI passes
        e.g. ``"tm stock add"``); ``timeout`` overrides the default wait
        (10 s, or ``$TETRAMARK_LOCK_TIMEOUT`` seconds).  When another process
        holds the lock for longer, :class:`~tetramark.errors.LedgerBusyError`
        (exit code 2) is raised before the body runs.

        The lock is re-entrant per ``Ledger`` object: nested scopes -- the
        :class:`WriteSession` a command opens, or :meth:`set_author`'s session
        -- join the outermost one, which releases on success, on any exception
        and on ``KeyboardInterrupt``.

        >>> with ledger.hold_write_lock(command="tm stock add"):
        ...     ledger.stocks.add(...)
        """
        return self._write_lock_scope(command=command, timeout=timeout)

    #: Alias: ``with ledger.writing():`` reads naturally at a call site.
    writing = hold_write_lock

    @contextmanager
    def _write_lock_scope(
        self,
        *,
        command: str | None = None,
        timeout: float | None = None,
    ) -> Iterator[None]:
        first = self._write_lock_depth == 0
        if first:
            if self._write_lock is None:
                self._write_lock = WriteLock(self.path, command=command)
            else:
                self._write_lock.command = command
            self._write_lock.acquire(timeout=timeout)
        self._write_lock_depth += 1
        try:
            yield
        finally:
            self._write_lock_depth -= 1
            if self._write_lock_depth == 0:
                assert self._write_lock is not None
                self._write_lock.release()

    def write_lock_holder(self) -> LockHolder | None:
        """Who holds this ledger's write lock right now; ``None`` when free.

        A non-blocking probe for ``tm status`` (ticket .9): it never waits,
        never creates the lock file and never takes the lock.
        """
        return write_lock_holder(self)

    def _adopt_write_lock(self, lock: WriteLock) -> None:
        """Own a lock acquired before this ledger was opened (CLI decorator).

        The CLI takes the lock after resolving the directory but *before*
        :func:`open` reads ``ledger.yaml``, the schema and ``ledger_meta``, so
        even that part of a write command is inside the writer window.  The
        adopted scope counts as the outermost :meth:`hold_write_lock` level:
        write sessions opened by the command join it.
        """
        if self._write_lock is not None or self._write_lock_depth != 0:
            raise LedgerError("this ledger already holds or tracks a write lock")
        if not lock.held:
            raise LedgerError("cannot adopt a write lock that is not held")
        self._write_lock = lock
        self._write_lock_depth = 1

    def _release_adopted_write_lock(self) -> None:
        """Release a lock adopted with :meth:`_adopt_write_lock` (idempotent)."""
        lock = self._write_lock
        self._write_lock = None
        self._write_lock_depth = 0
        if lock is not None:
            lock.release()

    # -- lifecycle --------------------------------------------------------
    def close(self) -> None:
        _close_driver(self.driver)

    def __enter__(self) -> Ledger:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class WriteSession:
    """Statements of one command, run in one transaction and one commit.

    ``dry_run`` collects :attr:`statements` (see :attr:`sql`) and executes
    nothing.  ``no_commit`` executes normally but skips the Dolt commit.
    """

    def __init__(
        self,
        ledger: Ledger,
        message: str,
        *,
        dry_run: bool = False,
        no_commit: bool = False,
    ) -> None:
        self.ledger = ledger
        self.message = message
        self.dry_run = dry_run
        self.no_commit = no_commit
        self.statements: list[str] = []
        #: Hash of the Dolt commit the session made, if it made one.
        self.commit_hash: str | None = None
        self._transaction: AbstractContextManager[Driver] | None = None
        self._lock: AbstractContextManager[None] | None = None
        self._open = False

    # -- content ----------------------------------------------------------
    def execute(self, sql: str, params: Params = None) -> str:
        """Record (and, unless dry-run, execute) one statement."""
        self._require_open()
        rendered = render_sql(sql, params)
        self.statements.append(rendered)
        if not self.dry_run:
            self.ledger.driver.execute(rendered)
        return rendered

    def query(self, sql: str, params: Params = None) -> list[dict[str, Any]]:
        """Read through the ledger; inside a transaction this sees its writes."""
        self._require_open()
        return self.ledger.driver.query(sql, params)

    @property
    def sql(self) -> str:
        """The collected statements as one semicolon-terminated script."""
        if not self.statements:
            return ""
        return ";\n".join(self.statements) + ";"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.sql

    # -- context manager --------------------------------------------------
    def __enter__(self) -> WriteSession:
        if self._open:
            raise LedgerError("write session is already open")
        if not self.dry_run:
            # One writer per ledger (P11): hold the lock across the transaction
            # and the Dolt commit.  A command-level ``hold_write_lock`` (the
            # CLI write commands) is re-entrant, so this joins it.
            lock = self.ledger.hold_write_lock(
                command=f"write session: {self.message}"
            )
            lock.__enter__()
            self._lock = lock
            try:
                self._transaction = self.ledger.driver.transaction()
                self._transaction.__enter__()
            except BaseException:
                self._release_lock()
                raise
        self._open = True
        self.ledger.last_write = self
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        if not self._open:  # pragma: no cover - defensive
            return False
        self._open = False

        if self.dry_run:
            return False

        transaction = self._transaction
        self._transaction = None
        assert transaction is not None
        try:
            transaction.__exit__(exc_type, exc, traceback)
            if exc_type is not None:
                return False
            if self.statements and not self.no_commit:
                self.commit_hash = self.ledger.driver.commit(
                    self.message, self.ledger.author
                )
            return False
        finally:
            # Released after the transaction closed and the commit was made --
            # and on every error path (rollback, failed commit, SIGINT).
            self._release_lock()

    def _release_lock(self) -> None:
        lock = self._lock
        self._lock = None
        if lock is not None:
            lock.__exit__(None, None, None)

    def _require_open(self) -> None:
        if not self._open:
            raise LedgerError(
                "a write session must be used inside 'with ... as session'"
            )
