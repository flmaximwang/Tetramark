"""Related-object lines for ``show`` output (spec §6, Tetramark-obv).

``tm stock show`` and ``tm aliquot show`` resolve the objects their id columns
point at (the stock's entity, its parent stock, its source aliquot) into a
``related`` record.  The human output prints each one as a plain ``*`` line:
outside the rich key/value table, starting at column zero, so it can never be
mistaken for a field row or a table column.  The machine formats keep their old
shape: JSON gains ``related`` additively and CSV drops it (``without_related``),
so a CSV consumer sees exactly the columns it saw before.

The line builder is shared by both commands so the label column, the id widths
and the missing-value rules stay in one place.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from tetramark.cli.output import short_uuid_ids

__all__ = ["RELATED_LABEL_WIDTH", "related_lines", "without_related"]

#: The label column of a ``*`` line: ``* ``, the label padded to this width,
#: then two spaces, so every value starts at column 18 (spec §6).
RELATED_LABEL_WIDTH = 14

#: Actual control characters cannot survive in a one-line relation: a run of
#: them becomes the literal separator `` | `` (spec §6).  JSON keeps the value
#: unchanged and CSV never prints these lines.
_CONTROL_RUN = re.compile(r"[\x00-\x1f\x7f]+")
_CONTROL_SEPARATOR = " | "


def related_lines(related: Mapping[str, Any] | None) -> list[str]:
    """The ``*`` lines of one show record, in spec order: entity, parent, source.

    A ``null`` relation prints nothing (the whole line is omitted); the stock's
    own ``entity`` is always present, so its line always prints.  The lines are
    plain text: the caller echoes them, never a rich table.
    """
    if not related:
        return []
    lines: list[str] = []
    entity = related.get("entity")
    if isinstance(entity, Mapping):
        lines.append(_line("entity", _entity_text(entity)))
    parent = related.get("parent_stock")
    if isinstance(parent, Mapping):
        lines.append(_line("parent stock", _parent_stock_text(parent)))
    source = related.get("source_aliquot")
    if isinstance(source, Mapping):
        lines.append(_line("source aliquot", _source_aliquot_text(source)))
    return lines


def without_related(record: Mapping[str, Any]) -> dict[str, Any]:
    """The record as ``--format csv`` prints it: ``related`` is not a cell.

    The machine format keeps exactly the columns it had before this change, so
    a positional CSV consumer is not affected by the new relation lines.
    """
    return {key: value for key, value in record.items() if key != "related"}


def _line(label: str, text: str) -> str:
    """One ``*`` line: the label padded to the shared column, then the value."""
    return _clean(f"* {label:<{RELATED_LABEL_WIDTH}}  {text}")


def _clean(text: str) -> str:
    """Replace control-character runs with the literal `` | `` separator."""
    return _CONTROL_RUN.sub(_CONTROL_SEPARATOR, text)


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _short(value: Any) -> str:
    """A uuid id as its eight-character prefix; any other id unchanged."""
    text = _text(value)
    if not text:
        return ""
    return _text(short_uuid_ids([text])[0])


def _entity_text(entity: Mapping[str, Any]) -> str:
    """The stock's own entity: ``kind · name``, else the id with the reason."""
    if entity.get("missing"):
        return f"{_short(entity.get('id'))} (missing)"
    kind = _text(entity.get("kind"))
    name = entity.get("name")
    detail = (
        f"{_short(entity.get('id'))} (unnamed)"
        if name is None or _text(name) == ""
        else _text(name)
    )
    return f"{kind} · {detail}" if kind else detail


def _entity_reference(related_object: Mapping[str, Any]) -> str:
    """The entity part of a parent-stock / source-aliquot line."""
    entity_id = _short(related_object.get("entity_id"))
    if related_object.get("entity_missing"):
        return f"missing entity {entity_id}".strip()
    name = related_object.get("entity_name")
    if name is None or _text(name) == "":
        return f"unnamed entity {entity_id}".strip()
    return _text(name)


def _parent_stock_text(parent: Mapping[str, Any]) -> str:
    """``kind · <short id> (<its entity>)``, or ``<short id> (missing)``."""
    if parent.get("missing"):
        return f"{_short(parent.get('id'))} (missing)"
    kind = _text(parent.get("kind"))
    stock_id = _short(parent.get("id"))
    detail = f"{stock_id} ({_entity_reference(parent)})"
    return f"{kind} · {detail}" if kind else detail


def _source_aliquot_text(source: Mapping[str, Any]) -> str:
    """``<full aliquot id> · <status> · <kind> of <entity>``, or ``… (missing)``."""
    if source.get("missing"):
        return f"{_text(source.get('id'))} (missing)"
    aliquot_id = _text(source.get("id"))
    status = _text(source.get("status"))
    kind = _text(source.get("stock_kind"))
    entity = _entity_reference(source)
    return f"{aliquot_id} · {status} · {kind} of {entity}"
