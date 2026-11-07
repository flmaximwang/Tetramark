"""The shared body of ``tm <object> note`` (entity, stock and box).

All three commands take the same flags and behave identically; only the object
kind and the human line differ.  The argument validation, the editor wiring and
the ``no changes`` rule therefore live here once, and the commands in
:mod:`tetramark.cli.entity`, :mod:`tetramark.cli.stock` and
:mod:`tetramark.cli.box` are thin wrappers with their own help text.

A bare REF opens the current note in ``$VISUAL``/``$EDITOR``/``vi`` through
:func:`tetramark.form.editor.edit_plain_text`; an unchanged buffer prints
exactly ``no changes`` and writes nothing.  ``--append TEXT`` adds a line (no
separator when the note is empty), a bare ``--append`` opens an empty buffer,
and ``--clear`` sets the column to NULL.  An empty result without ``--clear``
is refused by the core writer.

The current note is read with :func:`tetramark.core.objects.read_note`: one
resolution round trip and no aliquot load.  The human line mirrors each
group's ``edit`` command -- ``Edited entity <kind> <id>``, ``Edited stock
<kind> <id>`` and ``Edited box <name> (<kind>)`` -- and ``--json`` /
``--dry-run`` are handled by :func:`tetramark.cli.objects_common.emit_write`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import typer

from tetramark.cli.objects_common import emit_write
from tetramark.cli.options import json_output, resolve_ledger
from tetramark.core.ledger import Ledger
from tetramark.core.objects import read_note
from tetramark.errors import UsageError
from tetramark.form.editor import edit_plain_text

__all__ = [
    "APPEND_HELP",
    "CLEAR_HELP",
    "TEXT_HELP",
    "run_note_command",
]

#: The positional TEXT argument's help, shared by the three commands.
TEXT_HELP = (
    "Text to append; requires --append. Positional because this CLI cannot "
    "express an option with an optional value."
)
#: ``--append``'s help, shared by the three commands.
APPEND_HELP = "Append TEXT, or the editor's result when TEXT is omitted."
#: ``--clear``'s help, shared by the three commands.
CLEAR_HELP = "Remove the notes (set the notes column to NULL)."


def run_note_command(
    ctx: typer.Context,
    *,
    object_type: str,
    ref: str,
    text: str | None,
    append: bool,
    clear: bool,
    as_json: bool,
) -> None:
    """Write one entity/stock/box note from the shared editor/append/clear rules.

    ``object_type`` selects the facade and the human line; the three command
    wrappers own only their arguments and docstring.
    """
    if clear and (append or text is not None):
        raise UsageError("--clear cannot be combined with --append or TEXT")
    if text is not None and not append:
        raise UsageError(
            f"TEXT needs --append; run `tm {object_type} note REF` to edit "
            "the note"
        )
    ledger = resolve_ledger(ctx)
    try:
        writer = _note_writer(ledger, object_type)
        if clear:
            result = writer(ref, clear=True)
        elif append and text is not None:
            result = writer(ref, append=text)
        else:
            edited = _note_editor(ledger, object_type, ref, append=append)
            if edited is None:
                typer.echo("no changes")
                return
            result = (
                writer(ref, append=edited) if append else writer(ref, text=edited)
            )
        emit_write(
            ledger,
            result,
            _note_message(object_type, result),
            as_json=json_output(ctx, as_json),
        )
    finally:
        ledger.close()


def _note_writer(
    ledger: Ledger, object_type: str
) -> Callable[..., dict[str, Any]]:
    """The facade method that writes one object kind's note."""
    if object_type == "entity":
        return ledger.entities.note
    if object_type == "stock":
        return ledger.stocks.note
    return ledger.boxes.note


def _note_editor(
    ledger: Ledger, object_type: str, ref: str, *, append: bool
) -> str | None:
    """The editor result for a note command; ``None`` means unchanged.

    A plain edit starts from the current note; a bare ``--append`` starts from
    an empty buffer, because its result is the text to add, not the whole
    note.  An unchanged buffer is detected against exactly the text the editor
    was handed, so whitespace the stored note already carried never counts as
    an edit.
    """
    if append:
        edited = edit_plain_text(
            "", description=f"text to append to the {object_type}'s notes"
        )
        return edited if edited.strip() else None
    current = read_note(ledger, object_type, ref) or ""
    edited = edit_plain_text(
        current,
        description=f"the {object_type}'s notes; an empty result needs --clear",
    )
    return edited if edited != current.rstrip("\n") else None


def _note_message(object_type: str, result: dict[str, Any]) -> str:
    """The human line, matching the object group's ``edit`` command."""
    if object_type == "box":
        return f"Edited box {result['name']} ({result['kind']})"
    return f"Edited {object_type} {result['kind']} {result['id']}"
