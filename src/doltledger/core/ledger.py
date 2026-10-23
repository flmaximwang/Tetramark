"""Ledger lifecycle: ``init_ledger``, ``open`` and the one-commit write session.

A ledger is one Dolt database inside a :class:`~doltledger.driver.workspace.Workspace`
(spec §1.1).  Creating one means:

1. ``mkdir <data_dir>/<name>`` and ``dolt init`` in it;
2. write ``ledger.yaml`` (the human-editable config, copied into the DB);
3. load the effective schema and execute the generated DDL;
4. write ``ledger_meta`` (fingerprint, preset versions, the ``ledger.yaml`` text,
   ``spec_version``);
5. make exactly one Dolt commit, ``init(ledger): <name>``.

Opening one loads ``ledger.yaml``, the effective schema, the id strategies and
the commit author (``ledger.yaml`` → ``workspace.yaml`` → Git config), then
compares the live fingerprint with the YAML one.  A mismatch is a warning, not
an error: inspecting the ledger is still useful and ``ledger schema plan`` is
the fix.

Every successful write goes through :meth:`Ledger.write`, which groups the
statements of one command into one SQL transaction and one Dolt commit (spec
§6).  ``dry_run`` collects the SQL instead of executing it; ``no_commit``
executes but leaves the change uncommitted.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import warnings
from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from types import TracebackType
from typing import Any

import yaml

from doltledger.core.ids import IdStrategy, strategy_from_config
from doltledger.core.objects import (
    AliasOps,
    AliquotOps,
    BoxOps,
    EntityOps,
    StockOps,
)
from doltledger.driver.base import (
    Driver,
    Params,
    ensure_ascii,
    looks_like_error,
    render_sql,
)
from doltledger.driver.embedded import EmbeddedDriver
from doltledger.driver.workspace import Workspace
from doltledger.errors import LedgerError, UnreachableError, UsageError
from doltledger.schema.ddl import Statement, schema_statements
from doltledger.schema.loader import load_ledger_schema, load_schema
from doltledger.schema.model import (
    DEFAULT_STATUSES,
    SPEC_VERSION,
    EffectiveSchema,
    LedgerConfig,
)
from doltledger.schema.tests_gen import CheckReport, install_tests, run_check

__all__ = [
    "LEDGER_FILE",
    "META_FINGERPRINT",
    "META_LEDGER_YAML",
    "META_PRESET_VERSIONS",
    "META_SPEC_VERSION",
    "InitPlan",
    "Ledger",
    "WriteSession",
    "build_id_strategies",
    "init_ledger",
    "open",
    "resolve_author",
]

#: Config file inside a ledger directory.
LEDGER_FILE = "ledger.yaml"

#: Keys stored in ``ledger_meta``.  Other tickets (08/09) read and update them.
META_SPEC_VERSION = "spec_version"
META_FINGERPRINT = "fingerprint"
META_PRESET_VERSIONS = "preset_versions"
META_LEDGER_YAML = "ledger_yaml"
META_ORDER: tuple[str, ...] = (
    META_SPEC_VERSION,
    META_FINGERPRINT,
    META_PRESET_VERSIONS,
    META_LEDGER_YAML,
)

_LEDGER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_COMMIT_PART_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
_AUTHOR_RE = re.compile(r"^\s*(?P<name>[^<>]*?)\s*<(?P<email>[^<>]+)>\s*$")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _as_workspace(workspace: Workspace | str | Path) -> Workspace:
    if isinstance(workspace, Workspace):
        return workspace
    return Workspace.load(workspace)


def _check_name(name: str) -> str:
    if not isinstance(name, str) or not _LEDGER_NAME_RE.match(name):
        raise UsageError(
            f"invalid ledger name {name!r}: use letters, digits, '_' or '-' "
            "(starting with a letter or digit)"
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
    workspace_author: str | None,
    *,
    cwd: Path,
    env: Mapping[str, str] | None = None,
) -> str | None:
    """Resolve the commit author: ledger.yaml → workspace.yaml → Git config."""
    for candidate in (config_author, workspace_author):
        if candidate and str(candidate).strip():
            return str(candidate).strip()
    return _git_identity(cwd, env)


def _dolt_identity(
    author: str | None, *, cwd: Path, env: Mapping[str, str] | None
) -> tuple[str, str]:
    """Name and email for ``dolt init``, filling gaps from Git config."""
    name, email = split_author(author)
    name = name or _git_config("user.name", cwd, env)
    email = email or _git_config("user.email", cwd, env)
    if not name or not email:
        raise UsageError(
            "cannot initialise a ledger without an author: pass --author "
            "\"Name <email>\", set author in workspace.yaml, or configure "
            "git user.name/user.email"
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


def _promote_presets(presets: Sequence[str | Path], *, base_dir: Path) -> list[str]:
    """Turn existing preset paths into absolute ones so ``ledger.yaml`` reopens.

    Bare names (``base``, ``molbio``) stay bare: the loader resolves them as
    bundled presets.  A relative path is stored absolute because ``ledger.yaml``
    lives inside the ledger directory, not in the caller's cwd.
    """
    promoted: list[str] = []
    for raw in presets:
        text = str(raw).strip()
        if not text:
            raise UsageError("preset names must not be empty")
        candidate = Path(text).expanduser()
        if not candidate.is_absolute():
            candidate = base_dir / candidate
        if candidate.exists():
            promoted.append(str(candidate.resolve()))
        else:
            promoted.append(text)
    return promoted


def _ledger_yaml(
    *,
    name: str,
    presets: Sequence[str],
    id_prefix: str | None,
    author: str,
    statuses: Sequence[str] | None,
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
    payload["statuses"] = list(statuses or DEFAULT_STATUSES)
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

    workspace: Workspace
    name: str
    path: Path
    config: LedgerConfig
    schema: EffectiveSchema
    ledger_yaml: str
    statements: list[Statement]
    author: str

    @property
    def sql(self) -> str:
        """The generated DDL as one SQL script."""
        return ";\n".join(statement.sql for statement in self.statements) + ";"


def _prepare_init(
    workspace: Workspace | str | Path,
    name: str,
    presets: Sequence[str | Path] | str | Path | None,
    *,
    id_prefix: str | None,
    author: str | None,
    statuses: Sequence[str] | None,
) -> InitPlan:
    ws = _as_workspace(workspace)
    _check_name(name)

    target = ws.ledger_path(name)
    if target.exists():
        raise LedgerError(f"ledger {name!r} already exists at {target}")

    if presets is None:
        sources: list[str] = []
    elif isinstance(presets, (str, Path)):
        sources = [str(presets)]
    else:
        sources = [str(preset) for preset in presets]
    sources = _promote_presets(sources, base_dir=Path.cwd())
    if not sources:
        raise UsageError(
            "a ledger needs at least one preset: pass --preset NAME (or a preset path)"
        )

    resolved_author = resolve_author(
        author, ws.config.author, cwd=ws.data_dir, env=ws.env
    )
    identity_name, identity_email = _dolt_identity(
        resolved_author, cwd=ws.data_dir, env=ws.env
    )
    canonical_author = f"{identity_name} <{identity_email}>"

    payload, text = _ledger_yaml(
        name=name,
        presets=sources,
        id_prefix=id_prefix,
        author=canonical_author,
        statuses=statuses,
    )
    try:
        config = LedgerConfig(**payload)
    except Exception as error:  # pydantic ValidationError, rephrased below
        raise UsageError(f"invalid ledger configuration: {error}") from error

    # Fail before creating directories: a bad preset must not leave junk behind.
    build_id_strategies(config)
    schema = load_schema(config, base_dir=target)

    return InitPlan(
        workspace=ws,
        name=name,
        path=target,
        config=config,
        schema=schema,
        ledger_yaml=text,
        statements=schema_statements(schema),
        author=canonical_author,
    )


def _write_meta(
    driver: Driver,
    schema: EffectiveSchema,
    ledger_yaml: str,
) -> None:
    values = {
        META_SPEC_VERSION: str(SPEC_VERSION),
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


def init_ledger(
    workspace: Workspace | str | Path,
    name: str,
    presets: Sequence[str | Path] | str | Path | None = None,
    *,
    id_prefix: str | None = None,
    author: str | None = None,
    statuses: Sequence[str] | None = None,
    dry_run: bool = False,
) -> Ledger | InitPlan:
    """Create a ledger (spec §1.1, §3).

    Returns the opened :class:`Ledger`; with ``dry_run=True`` returns an
    :class:`InitPlan` and writes nothing.
    """
    plan = _prepare_init(
        workspace,
        name,
        presets,
        id_prefix=id_prefix,
        author=author,
        statuses=statuses,
    )
    if dry_run:
        return plan

    target = plan.path
    target.mkdir(parents=True, exist_ok=False)
    try:
        identity_name, identity_email = split_author(plan.author)
        assert identity_name and identity_email  # _dolt_identity checked this
        _run_dolt_init(target, identity_name, identity_email, plan.workspace.env)

        (target / LEDGER_FILE).write_text(plan.ledger_yaml, encoding="utf-8")

        driver = EmbeddedDriver(target, env=plan.workspace.env)
        with driver.transaction():
            for statement in plan.statements:
                driver.execute(statement.sql)
            _write_meta(driver, plan.schema, plan.ledger_yaml)
            # Generated dolt_tests are derived state (spec §4): install them
            # with the schema so a fresh ledger is immediately checkable.
            install_tests(driver, plan.schema)
        driver.commit(f"init(ledger): {name}", author=plan.author)
    except BaseException:
        # A half-initialised ledger is worse than none: never leave one behind.
        shutil.rmtree(target, ignore_errors=True)
        raise

    ledger = Ledger(
        workspace=plan.workspace,
        name=name,
        driver=driver,
        config=plan.config,
        schema=plan.schema,
        author=plan.author,
    )
    ledger.refresh_meta()
    return ledger


# ---------------------------------------------------------------------------
# Open
# ---------------------------------------------------------------------------


def open(
    workspace: Workspace | str | Path | None = None,
    ledger: str | None = None,
    *,
    dry_run: bool = False,
    no_commit: bool = False,
) -> Ledger:
    """Open an existing ledger, choosing a server or embedded driver (spec §6).

    ``workspace`` accepts a :class:`Workspace`, a data directory or a
    ``workspace.yaml`` path; when omitted the workspace is discovered from the
    current directory (then ``$DOLTLEDGER_WORKSPACE``).
    """
    ws = _as_workspace(workspace) if workspace is not None else Workspace.discover()
    if ws is None:
        raise UsageError(
            "no workspace found: pass --workspace PATH or run inside a directory "
            "containing workspace.yaml"
        )
    name = ledger or ws.config.default_ledger
    if not name:
        raise UsageError(
            "no ledger selected: pass --ledger or set default_ledger in workspace.yaml"
        )
    _check_name(name)

    path = ws.ledger_path(name)
    if not (path / LEDGER_FILE).is_file():
        raise UnreachableError(f"ledger {name!r} not found in {ws.data_dir}")
    driver = ws.driver_for(name)
    try:
        config, schema = load_ledger_schema(path)
        author = resolve_author(
            config.author, ws.config.author, cwd=path, env=ws.env
        )
        result = Ledger(
            workspace=ws,
            name=name,
            driver=driver,
            config=config,
            schema=schema,
            author=author,
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
    """One opened ledger: driver, config, schema, id strategies and author."""

    def __init__(
        self,
        *,
        workspace: Workspace,
        name: str,
        driver: Driver,
        config: LedgerConfig,
        schema: EffectiveSchema,
        author: str | None = None,
        dry_run: bool = False,
        no_commit: bool = False,
    ) -> None:
        self.workspace = workspace
        self.name = name
        self.driver = driver
        self.config = config
        self.schema = schema
        self.author = author
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

    # -- identity ---------------------------------------------------------
    @property
    def path(self) -> Path:
        return self.workspace.ledger_path(self.name)

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
            "run 'ledger schema plan' to see the pending changes"
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
        from doltledger.ops.find import find

        return find(self, query)

    def history(self, ref: str) -> dict[str, Any]:
        """Aliquot events plus Dolt commits touching one object (ticket 11)."""
        from doltledger.ops.history import history

        return history(self, ref)

    # -- configuration ----------------------------------------------------
    def set_author(self, author: str) -> WriteSession:
        """Set the ledger's commit author (``ledger config set author``).

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
            self._transaction = self.ledger.driver.transaction()
            self._transaction.__enter__()
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
        transaction.__exit__(exc_type, exc, traceback)
        if exc_type is not None:
            return False
        if self.statements and not self.no_commit:
            self.commit_hash = self.ledger.driver.commit(
                self.message, self.ledger.author
            )
        return False

    def _require_open(self) -> None:
        if not self._open:
            raise LedgerError(
                "a write session must be used inside 'with ... as session'"
            )
