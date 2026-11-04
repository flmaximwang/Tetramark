"""``tm status``: one standalone ledger at a glance (spec R3, Tetramark-l8g.9).

There is no workspace, server or ledger registry in 0.5.0: a command operates on
the ledger directory it resolves (``-l/--ledger PATH`` or the upward walk), and
``tm status`` prints **that** ledger.  It never lists sibling ledgers.

Human output is one aligned label/value block::

    path            /Users/me/lab/zsqlab_10
    name            zsqlab_10
    branch          main
    spec_version    5
    presets         molbio
    author          Wang <wang@example.invalid>
    write lock      free
    portability     no warnings

A held write lock (P11) is shown as e.g.
``write lock      pid 4711 (tm stock add) since 2026-11-03T05:00:00Z on host``;
each portability warning (P13, an absolute ``file://`` remote/backup URL that
leaves the ledger) is one indented line under ``portability``.

``--json`` (or the global ``--json`` / ``--format json``) prints exactly this
shape, with these stable types -- tests assert the key set and types verbatim::

    {
      "path": "/abs/path/to/ledger",          str
      "name": "zsqlab_10",                    str   (ledger.yaml "name:")
      "branch": "main",                       str | null
      "spec_version": 5,                      int | null  (ledger_meta)
      "presets": ["molbio"],                  list[str]   (ledger.yaml order)
      "author": "Name <email>",               str | null
      "lock": {                               object, always all five keys
        "held": false,                        bool
        "pid": null,                          int | null
        "command": null,                      str | null
        "since": null,                        str | null (UTC ISO-8601)
        "host": null                          str | null
      },
      "portability_warnings": [               list[object], all four keys
        {"kind": "backup", "name": "old",     str ("remote" | "backup")
         "url": "file:///old/backup",         str
         "path": "/old/backup"}               str
      ]
    }

Only the human and JSON shapes exist: ``--format csv`` is a usage error.
Exit codes: 0 for a readable ledger; 2 when no ledger / not a ledger can be
resolved (the resolver's ``UsageError``); 3 (or 1) when the database itself
cannot be read -- the command adds no error handling of its own.

The lock is probed with :func:`tetramark.core.writelock.write_lock_holder`:
a non-blocking, non-creating, never-waiting ``flock`` attempt (``tm status``
must never make a reader look like a writer).  Portability warnings come from
:func:`tetramark.ops.portability.absolute_file_urls`, the same helper ``tm
check`` uses -- this module does not parse URLs itself.
"""

from __future__ import annotations

from typing import Any

import typer

from tetramark.cli.options import resolve_ledger
from tetramark.cli.output import emit_payload, output_format
from tetramark.core.ledger import META_SPEC_VERSION, Ledger
from tetramark.core.writelock import LockHolder
from tetramark.errors import UsageError
from tetramark.ops.portability import absolute_file_urls

__all__ = [
    "LOCK_KEYS",
    "STATUS_KEYS",
    "WARNING_KEYS",
    "collect_status",
    "render_human",
    "status_command",
]

#: The exact top-level keys of the ``--json`` shape (printed sorted by key).
STATUS_KEYS: tuple[str, ...] = (
    "path",
    "name",
    "branch",
    "spec_version",
    "presets",
    "author",
    "lock",
    "portability_warnings",
)

#: The exact keys of the ``lock`` object (always present, ``None`` when free).
LOCK_KEYS: tuple[str, ...] = ("held", "pid", "command", "since", "host")

#: The exact keys of one portability warning.
WARNING_KEYS: tuple[str, ...] = ("kind", "name", "url", "path")

#: Label column of the human output (keys are padded to this width).
_LABEL_WIDTH = 16


def status_command(
    ctx: typer.Context,
    as_json: bool = typer.Option(False, "--json", help="Print the fixed JSON shape."),
) -> None:
    """Show the ledger's path, name, branch, spec version, presets, author and warnings."""
    fmt = output_format(ctx, as_json)
    if fmt == "csv":
        raise UsageError(
            "tm status has no CSV output; pass --json for machine-readable output"
        )

    ledger = resolve_ledger(ctx)
    try:
        payload = collect_status(ledger)
    finally:
        ledger.close()

    if fmt == "json":
        emit_payload(payload, fmt="json")
    else:
        typer.echo(render_human(payload))


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------


def collect_status(ledger: Ledger) -> dict[str, Any]:
    """Read the status payload off one opened ledger (P2/R3).

    Reads, in order: the ledger directory and ``ledger.yaml`` identity already
    on the :class:`~tetramark.core.ledger.Ledger`, the session/active branch,
    ``ledger_meta.spec_version``, the recorded presets and effective author,
    the write-lock holder (non-blocking probe) and the portability findings.
    """
    branch = ledger.branch
    if branch is None:
        # ``active_branch()`` is one NOT NULL value, so the read can skip the
        # CSV header run: status adds at most one dolt process after open.
        rows = ledger.driver.query(
            "SELECT active_branch() AS `branch`", fill_columns=False
        )
        value = rows[0].get("branch") if rows else None
        branch = None if value is None else str(value)

    return {
        "path": str(ledger.path),
        "name": ledger.name,
        "branch": branch,
        "spec_version": _spec_version(ledger.meta.get(META_SPEC_VERSION)),
        "presets": list(ledger.config.presets),
        "author": ledger.author,
        "lock": _lock_payload(ledger.write_lock_holder()),
        "portability_warnings": absolute_file_urls(ledger.path),
    }


def _spec_version(raw: str | None) -> int | None:
    """``ledger_meta.spec_version`` as an int; ``None`` when absent/invalid."""
    if raw is None:
        return None
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _lock_payload(holder: LockHolder | None) -> dict[str, Any]:
    """The fixed ``lock`` object (all five keys, whether held or free)."""
    if holder is None:
        return {"held": False, "pid": None, "command": None, "since": None, "host": None}
    return {
        "held": True,
        "pid": holder.pid,
        "command": holder.command,
        "since": holder.since,
        "host": holder.host,
    }


# ---------------------------------------------------------------------------
# Human rendering
# ---------------------------------------------------------------------------


def render_human(status: dict[str, Any]) -> str:
    """Render the status payload as the fixed human block."""
    lines = [
        _line("path", status["path"]),
        _line("name", status["name"]),
        _line("branch", status["branch"] or "(unknown)"),
        _line(
            "spec_version",
            "(unknown)" if status["spec_version"] is None else str(status["spec_version"]),
        ),
        _line("presets", ", ".join(status["presets"]) or "(none)"),
        _line("author", status["author"] or "(none)"),
        _line("write lock", _lock_text(status["lock"])),
    ]
    warnings = status["portability_warnings"]
    if not warnings:
        lines.append(_line("portability", "no warnings"))
    else:
        lines.append(_line("portability", f"{len(warnings)} warning(s):"))
        for warning in warnings:
            lines.append(
                " " * _LABEL_WIDTH
                + f"{warning['kind']} {warning['name']!r}: {warning['url']}"
            )
    return "\n".join(lines)


def _line(label: str, value: str) -> str:
    return f"{label:<{_LABEL_WIDTH}}{value}"


def _lock_text(lock: dict[str, Any]) -> str:
    """``free`` or ``pid N (command) since ... on host`` for the human block."""
    if not lock["held"]:
        return "free"
    parts: list[str] = []
    if lock["pid"] is not None:
        parts.append(f"pid {lock['pid']}")
    if lock["command"]:
        parts.append(f"({lock['command']})")
    if not parts:
        parts.append("another process")
    if lock["since"]:
        parts.append(f"since {lock['since']}")
    if lock["host"]:
        parts.append(f"on {lock['host']}")
    return " ".join(parts)
