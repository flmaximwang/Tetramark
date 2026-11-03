"""Shared CLI glue for ``tm <obj> add-form`` (Tetramark-se9).

The commands are deliberately **not** decorated with
:func:`~tetramark.cli.options.write_command`: the form reads the ledger while
the user answers, and the write lock is taken only when the review screen is
confirmed with ``y`` (see :func:`tetramark.form.session.run_add_form`).

Without a terminal the command refuses with a usage error that points at
``tm <obj> add`` -- the form is for people, not for agents or scripts.
"""

from __future__ import annotations

import sys
from typing import Any, Callable

import typer

from tetramark.cli.messages import (
    aliquots_add_message,
    box_add_message,
    entity_add_message,
    stock_add_message,
)
from tetramark.cli.options import json_output, resolve_ledger
from tetramark.cli.output import emit_write
from tetramark.errors import EXIT_USAGE
from tetramark.form.session import run_add_form

__all__ = ["run_add_form_command"]

_MESSAGES: dict[str, Callable[[dict[str, Any]], str]] = {
    "entity": entity_add_message,
    "stock": stock_add_message,
    "aliquot": aliquots_add_message,
    "box": box_add_message,
}


def _stdin_is_tty() -> bool:
    """Whether stdin is a terminal (tests monkeypatch this hook)."""
    try:
        return bool(sys.stdin.isatty())
    except (AttributeError, ValueError):  # pragma: no cover - closed stdin
        return False


def _make_source() -> Any:
    """The real terminal source (imported lazily: prompt_toolkit on demand)."""
    from tetramark.form.tui import PromptToolkitSource

    return PromptToolkitSource()


def run_add_form_command(
    ctx: typer.Context, object_type: str, *, as_json: bool
) -> None:
    """Run the form for one object and report the result like ``add``."""
    if not _stdin_is_tty():
        typer.echo(
            f"Error: 'tm {object_type} add-form' needs an interactive terminal; "
            f"use 'tm {object_type} add ...' instead (agents and scripts should "
            "use add)",
            err=True,
        )
        raise typer.Exit(EXIT_USAGE)
    ledger = resolve_ledger(ctx)
    try:
        result = run_add_form(
            ledger,
            object_type,
            source=_make_source(),
            lock_command=f"tm {object_type} add-form",
        )
        if result is None:
            typer.echo("Cancelled; nothing written.")
            return
        emit_write(
            ledger,
            result,
            _MESSAGES[object_type](result),
            as_json=json_output(ctx, as_json),
        )
    finally:
        ledger.close()
