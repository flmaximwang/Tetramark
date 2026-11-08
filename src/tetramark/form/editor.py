"""Ctrl+E: edit the current field in ``$VISUAL`` -> ``$EDITOR`` -> ``vi``.

The temp file holds the value first, then a git-style **scissors** line with
the field's name, description and pattern below it.  Everything from the
scissors line on is removed when the file comes back; lines that merely start
with ``#`` (a legitimate value in a notes/text field) are kept.  All of this is
plain Python, so it is covered by the Dolt-free fast tests.

:func:`edit_value` edits one form field (it needs the field's :class:`Step`);
:func:`edit_plain_text` edits free text such as a stock's notes and takes only
its label and description for the scissors metadata.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Mapping

from tetramark.form.spec import Step

__all__ = [
    "SCISSORS",
    "edit_plain_text",
    "edit_value",
    "editor_command",
    "editor_text",
    "plain_editor_text",
    "strip_editor_text",
]

#: The git-style scissors line (``git commit --verbose`` uses the same one).
SCISSORS = "# ------------------------ >8 ------------------------"

_SCISSORS_RE = re.compile(r"^#\s*-{2,}\s*>8\s*-{2,}\s*$")


def editor_command(env: Mapping[str, str] | None = None) -> list[str]:
    """``$VISUAL``, then ``$EDITOR``, then ``vi`` (as a command list)."""
    source = os.environ if env is None else env
    for name in ("VISUAL", "EDITOR"):
        value = source.get(name)
        if value and value.strip():
            return shlex.split(value)
    return ["vi"]


def editor_text(value: str, step: Step) -> str:
    """The temp file: the value, then the scissors line and its metadata."""
    lines = [value.rstrip("\n"), "", SCISSORS, f"# field: {step.key}"]
    if step.description:
        lines.append(f"# description: {step.description}")
    if step.pattern:
        lines.append(f"# pattern: {step.pattern}")
    return "\n".join(lines) + "\n"


def plain_editor_text(
    value: str,
    *,
    field: str = "notes",
    description: str | None = None,
) -> str:
    """The temp file of a free-text edit: the value, then scissors + metadata."""
    lines = [value.rstrip("\n"), "", SCISSORS, f"# field: {field}"]
    if description:
        lines.append(f"# description: {description}")
    return "\n".join(lines) + "\n"


def strip_editor_text(text: str) -> str:
    """Drop the scissors line and everything below it; keep ``#`` lines."""
    kept: list[str] = []
    for line in text.splitlines():
        if _SCISSORS_RE.match(line.strip()):
            break
        kept.append(line)
    while kept and not kept[-1].strip():
        kept.pop()
    return "\n".join(kept)


def _edit_buffer(
    text: str,
    *,
    env: Mapping[str, str] | None,
    run: Callable[..., object],
) -> str:
    """Write ``text`` to a temp file, run the editor on it, return the result."""
    handle = tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    )
    path = Path(handle.name)
    try:
        with handle:
            handle.write(text)
        run([*editor_command(env), str(path)], check=False)
        return strip_editor_text(path.read_text(encoding="utf-8"))
    finally:
        path.unlink(missing_ok=True)


def edit_value(
    value: str,
    step: Step,
    *,
    env: Mapping[str, str] | None = None,
    run: Callable[..., object] = subprocess.run,
) -> str:
    """Run the editor on ``value`` and return the edited text.

    ``run`` is injectable so the fast tests can edit the file without starting
    a real editor (a small script that rewrites the path it is given).
    """
    return _edit_buffer(editor_text(value, step), env=env, run=run)


def edit_plain_text(
    value: str,
    *,
    field: str = "notes",
    description: str | None = None,
    env: Mapping[str, str] | None = None,
    run: Callable[..., object] = subprocess.run,
) -> str:
    """Run the editor on free text (no form ``Step``) and return the text.

    ``value`` is the initial buffer -- empty for an append, the current note
    for a plain edit.  The scissors line and its metadata follow it and are
    stripped from the result exactly as in :func:`edit_value`; ``run`` is
    injectable the same way.
    """
    return _edit_buffer(
        plain_editor_text(value, field=field, description=description),
        env=env,
        run=run,
    )
