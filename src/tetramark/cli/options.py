"""Shared CLI plumbing: the global option values and the help-preserving groups.

``GlobalOptions`` is resolved once by the root callback; :func:`group` builds the
command groups whose help answers ``-h`` and a bare invocation (see
:func:`help_on_no_command`).
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

import typer

from tetramark.core.ledger import Ledger, open as open_ledger
from tetramark.core.writelock import WriteLock
from tetramark.driver.resolution import resolve_ledger as resolve_ledger_dir
from tetramark.errors import EXIT_OK

#: Attribute :func:`write_command` sets on the wrapper (tests assert coverage).
WRITE_COMMAND_MARKER = "__tetramark_write_command__"

_F = TypeVar("_F", bound=Callable[..., Any])

#: ``--help`` is spelled ``-h`` too, on every command and command group. Click
#: inherits ``help_option_names`` from the root context down the command tree,
#: so the root alone would be enough; groups carry it too, so a group never
#: depends on how it was reached.
HELP_CONTEXT_SETTINGS: dict[str, list[str]] = {"help_option_names": ["-h", "--help"]}


def help_on_no_command(ctx: typer.Context) -> None:
    """Print this command's help and succeed when no subcommand was given.

    Asking for help is not a usage error: a bare ``tm`` or ``tm box`` exits 0
    after printing its help. Real misuse -- an unknown option, an unknown
    command, a missing argument -- still exits 2 through Click's own usage
    handling.
    """
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help(), color=ctx.color)
        raise typer.Exit(EXIT_OK)


def group(help: str, *, name: str | None = None) -> typer.Typer:
    """A command group whose help answers ``-h`` and a bare invocation."""
    return typer.Typer(
        name=name,
        help=help,
        no_args_is_help=False,
        invoke_without_command=True,
        context_settings=HELP_CONTEXT_SETTINGS,
        callback=help_on_no_command,
    )


@dataclass
class GlobalOptions:
    """Values of the global options, passed to subcommands via ``ctx.obj``."""

    #: ``-l/--ledger PATH``: the ledger directory; ``None`` = discover upward.
    ledger: Path | None = None
    branch: str | None = None
    dry_run: bool = False
    no_commit: bool = False
    json: bool = False
    format: str | None = None
    #: The ledger opened once by :func:`resolve_ledger` for this command.  A
    #: write command holds its lock while the command body runs.
    opened: Ledger | None = None


def _global_options(ctx: typer.Context) -> GlobalOptions:
    options = ctx.obj
    if not isinstance(options, GlobalOptions):
        raise RuntimeError("the CLI root callback did not set GlobalOptions")
    return options


def json_output(ctx: typer.Context, local: bool = False) -> bool:
    """True when either the command's ``--json`` or the global one is set."""
    options = ctx.obj
    return bool(local or (isinstance(options, GlobalOptions) and options.json))


def resolve_ledger(ctx: typer.Context) -> Ledger:
    """Open the ledger selected by ``-l/--ledger`` or discovered from the cwd.

    The ledger is opened once per command and cached on the options: a write
    command's decorator opens it first (to hold the write lock) and the
    command body then uses the same object instead of opening the schema and
    ``ledger_meta`` a second time.
    """
    options = _global_options(ctx)
    if options.opened is None:
        options.opened = open_ledger(
            options.ledger,
            branch=options.branch,
            dry_run=options.dry_run,
            no_commit=options.no_commit,
        )
    return options.opened


def _command_label(ctx: typer.Context) -> str:
    """``"tm stock add"``: the command path shown in the lock file/busy error."""
    names: list[str] = []
    node: typer.Context | None = ctx
    while node is not None:
        name = getattr(node, "info_name", None)
        if isinstance(name, str) and name and name not in names:
            names.append(name)
        node = getattr(node, "parent", None)
    return " ".join(reversed(names)) or "tm"


def write_command(func: _F) -> _F:
    """Run a CLI write command with the ledger's write lock held (P11).

    The lock is taken before :func:`open` reads ``ledger.yaml``, the schema and
    ``ledger_meta``, and held through the command body -- its reads,
    validation, write session and Dolt commit.  That covers the whole
    read -> validate -> write -> commit window of the command, so the
    read-then-write race between two ``tm`` processes is closed even though the
    embedded driver runs one ``dolt sql`` process per statement.  ``--dry-run``
    writes nothing and therefore takes no lock; the body opens the ledger
    itself in that case.  Reads are never decorated.
    """
    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        ctx = args[0] if args else kwargs.get("ctx")
        options = getattr(ctx, "obj", None)
        if (
            isinstance(options, GlobalOptions)
            and not options.dry_run
            and options.opened is None
        ):
            # Resolve the directory first (filesystem/YAML only, no database
            # read), take the lock, and open the ledger inside it.
            ref = resolve_ledger_dir(options.ledger)
            lock = WriteLock(ref.path, command=_command_label(ctx))
            lock.acquire()
            try:
                ledger = open_ledger(
                    options.ledger,
                    branch=options.branch,
                    dry_run=options.dry_run,
                    no_commit=options.no_commit,
                )
                ledger._adopt_write_lock(lock)
                options.opened = ledger
            except BaseException:
                lock.release()
                raise
            try:
                return func(*args, **kwargs)
            finally:
                ledger._release_adopted_write_lock()
        return func(*args, **kwargs)

    setattr(wrapper, WRITE_COMMAND_MARKER, True)
    return wrapper  # type: ignore[return-value]


def target_branch(ledger: Ledger) -> str:
    """The branch this command operates on (the checked-out one when unset)."""
    selected = getattr(ledger, "branch", None)
    if selected:
        return str(selected)
    try:
        rows = ledger.driver.query("SELECT active_branch() AS `branch`")
    except Exception:  # pragma: no cover - a dry run must not fail on this
        return ""
    if not rows:
        return ""
    value = rows[0].get("branch")
    return "" if value is None else str(value)


def echo_dry_run_sql(ledger: Ledger, sql: str) -> None:
    """Print a dry run's SQL, naming the target branch first (spec A3)."""
    branch = target_branch(ledger)
    if branch:
        typer.echo(f"-- target branch: {branch}")
    typer.echo(sql)
