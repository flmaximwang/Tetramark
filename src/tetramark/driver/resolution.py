"""Ledger resolution: find the ledger directory a command operates on.

A ledger is one self-contained directory: the Dolt database (``.dolt/``) and
its configuration (``ledger.yaml``) live together, next to the docs or the
repository they describe.  Resolution has exactly two sources, in this order
(P3):

1. an explicit path -- the CLI's ``-l/--ledger PATH``;
2. walking up from the current directory to the nearest directory that holds
   both ``.dolt/`` and ``ledger.yaml``.

There is deliberately **no environment variable**.  ``$TETRAMARK_LEDGER`` was
abandoned in the 0.5.0 design review: a command that silently reads another
ledger than the one you are standing in is worse than a clear error.

Two 0.4 layouts get their own error instead of a confusing "no ledger":

* a directory holding ``workspace.yaml`` met before any ledger is a **legacy
  workspace** (P4): the error names it and tells the user to ``cd`` into the
  ledger directory -- ``ledger.yaml`` already carries the author, so no setting
  has to move;
* a directory holding ``.dolt/`` without ``ledger.yaml`` is **not a ledger**
  (the bare legacy databases of a lab workspace look like this), and the error
  says so.

Identity is ``ledger.yaml``'s ``name:`` (P2).  The path is not identity, and the
directory basename is only the Dolt database name handed to
:class:`~tetramark.driver.embedded.EmbeddedDriver`; copy or rename a ledger
directory freely.

:func:`refuse_nested` is the other half of the "no nested ledgers" rule (P5):
``tm init``/``tm adopt`` must refuse to create a ledger inside an existing one.
"""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

import yaml

from tetramark.errors import UsageError

__all__ = [
    "DOLT_DIR",
    "LEDGER_FILE",
    "WORKSPACE_FILE",
    "LedgerRef",
    "is_ledger_dir",
    "ledger_dir",
    "ledger_name",
    "refuse_nested",
    "resolve_ledger",
]

#: The ledger configuration file; with ``.dolt/`` it defines a ledger directory.
LEDGER_FILE = "ledger.yaml"

#: The Dolt database directory inside a ledger.
DOLT_DIR = ".dolt"

#: The 0.4 workspace marker.  Only read to produce the P4 migration error.
WORKSPACE_FILE = "workspace.yaml"


@dataclass(frozen=True)
class LedgerRef:
    """One resolved ledger: its directory and the identity from ``ledger.yaml``."""

    path: Path
    name: str

    @property
    def config_path(self) -> Path:
        """``<ledger>/ledger.yaml``."""
        return self.path / LEDGER_FILE


def is_ledger_dir(path: str | Path) -> bool:
    """True when ``path`` holds both ``.dolt/`` and ``ledger.yaml``."""
    candidate = Path(path)
    return (candidate / DOLT_DIR).is_dir() and (candidate / LEDGER_FILE).is_file()


def ledger_name(path: str | Path) -> str:
    """Read the ledger identity from ``<path>/ledger.yaml`` (P2).

    The name is what ``ledger.yaml`` says, never the directory basename.  A
    missing, unreadable or nameless configuration is a usage error.
    """
    config_path = Path(path) / LEDGER_FILE
    try:
        payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except OSError as error:
        raise UsageError(f"cannot read {config_path}: {error}") from error
    except yaml.YAMLError as error:
        raise UsageError(f"invalid {config_path}: {error}") from error
    if not isinstance(payload, dict):
        raise UsageError(f"invalid {config_path}: expected a mapping")
    name = payload.get("name")
    if not isinstance(name, str) or not name.strip():
        raise UsageError(
            f"invalid {config_path}: 'name' must be a non-empty string; "
            "it is the ledger's identity"
        )
    return name.strip()


def ledger_dir(path: str | Path | None = None, *, cwd: str | Path | None = None) -> Path:
    """The ledger directory selected by ``path`` or by walking up from ``cwd``.

    ``path`` (the CLI's ``-l/--ledger``) wins when given and is validated
    directly.  Otherwise the walk starts at ``cwd`` (default: the process
    working directory) and returns the nearest directory containing both
    ``.dolt/`` and ``ledger.yaml``.
    """
    if path is not None:
        return _explicit_ledger_dir(path)
    origin = Path(cwd) if cwd is not None else Path.cwd()
    return _discover_ledger_dir(origin)


def resolve_ledger(
    path: str | Path | None = None, *, cwd: str | Path | None = None
) -> LedgerRef:
    """Resolve the ledger directory and read its identity from ``ledger.yaml``."""
    directory = ledger_dir(path, cwd=cwd)
    return LedgerRef(path=directory, name=ledger_name(directory))


def refuse_nested(path: str | Path) -> Path:
    """Refuse a ledger directory that sits inside an existing ledger (P5).

    Returns the resolved ``path`` when no enclosing ledger exists; raises
    :class:`~tetramark.errors.UsageError` otherwise.  ``path`` need not exist
    yet: the check walks its parents, so ``tm init`` can call it before
    creating anything.
    """
    target = Path(path).expanduser().resolve()
    for candidate in target.parents:
        if not is_ledger_dir(candidate):
            continue
        try:
            enclosing = ledger_name(candidate)
        except UsageError:  # the enclosing ledger's config is broken, not ours
            enclosing = candidate.name
        raise UsageError(
            f"refusing to create a ledger at {target}: it is inside the "
            f"existing ledger {enclosing!r} at {candidate}. Ledgers do not nest "
            "(P5); use a directory outside it, or copy the ledger directory "
            "instead of creating one inside it"
        )
    return target


# ---------------------------------------------------------------------------
# Resolution internals
# ---------------------------------------------------------------------------


def _explicit_ledger_dir(path: str | Path) -> Path:
    """Validate an explicitly given ledger directory (no upward search)."""
    target = Path(path).expanduser()
    if target.is_file():
        if target.name != LEDGER_FILE:
            raise UsageError(
                f"{target} is not a ledger directory: pass the directory that "
                f"holds {LEDGER_FILE} and {DOLT_DIR}/"
            )
        target = target.parent
    if not target.exists():
        raise UsageError(
            f"no ledger at {target}: the path does not exist. Pass an existing "
            "ledger directory (one with .dolt/ and ledger.yaml)"
        )
    if not target.is_dir():
        raise UsageError(f"{target} is not a ledger directory")
    target = target.resolve()
    if is_ledger_dir(target):
        return target
    if (target / WORKSPACE_FILE).is_file():
        raise UsageError(_workspace_message(target))
    if (target / DOLT_DIR).is_dir():
        raise UsageError(_bare_database_message(target))
    raise UsageError(_not_a_ledger_message(target))


def _discover_ledger_dir(origin: Path) -> Path:
    """Walk up from ``origin`` to the nearest ledger (P3, P4)."""
    start = Path(origin).expanduser()
    if start.is_file():
        start = start.parent
    start = start.resolve()
    for candidate in (start, *start.parents):
        if is_ledger_dir(candidate):
            return candidate
        # P4: the old workspace marker wins over a bare Dolt database in the
        # same directory (a 0.4 workspace could hold unformatted databases).
        if (candidate / WORKSPACE_FILE).is_file():
            raise UsageError(_workspace_message(candidate))
        if (candidate / DOLT_DIR).is_dir():
            raise UsageError(_bare_database_message(candidate))
    raise UsageError(
        f"no ledger found from {start}: neither it nor any parent directory "
        f"contains both {DOLT_DIR}/ and {LEDGER_FILE}. Pass --ledger PATH, cd "
        "into a ledger directory, or create one with 'tm init'"
    )


def _workspace_message(workspace: Path) -> str:
    """P4: name the old workspace and point at the ledger directory."""
    name = workspace.name or str(workspace)
    example = _example_ledger(workspace)
    if example is None:
        example = workspace / "<ledger>"
    return (
        f"found the 0.4 workspace {name!r} at {workspace} ({WORKSPACE_FILE}): "
        "workspaces were removed in 0.5.0. A ledger is one self-contained "
        "directory now -- cd into the ledger directory itself and re-run the "
        f"command:\n  cd {example}\n"
        f"(or pass it explicitly: tm --ledger {example})"
    )


def _example_ledger(workspace: Path) -> Path | None:
    """A ledger directory to suggest: the first one below the old workspace.

    ``workspace.yaml`` is never parsed: its settings were removed in 0.5.0
    (P7). The file only marks a legacy layout, so the suggestion is a
    convenience for the ``cd`` hint.
    """
    with suppress(OSError):
        ledgers = sorted(
            entry for entry in workspace.iterdir() if is_ledger_dir(entry)
        )
        if ledgers:
            return ledgers[0].resolve()
    return None


def _bare_database_message(database: Path) -> str:
    """P3(d): ``.dolt/`` without ``ledger.yaml`` is not (yet) a ledger."""
    return (
        f"{database} is not a ledger: it has a Dolt database ({DOLT_DIR}/) but "
        f"no {LEDGER_FILE}. A 0.5.0 ledger needs both; 'tm adopt' formats an "
        "existing bare Dolt database in place"
    )


def _not_a_ledger_message(target: Path) -> str:
    return (
        f"{target} is not a ledger: it holds neither {LEDGER_FILE} nor "
        f"{DOLT_DIR}/. Pass the ledger directory itself, cd into it, or create "
        "one with 'tm init'"
    )
