"""Read, set and clear one parent relation between two objects (spec 6.7).

A relation is one child object pointing at one parent object through an
*existing* column (spec 6.7): ``stock.parent_stock_id``,
``stock.source_aliquot_id``, ``box.parent_box_id`` and a kind's
``type: ref`` fields.  This module is the single implementation of the
read/write rules behind ``tm parent`` / ``tm child`` and ``ledger.relations``;
the CLI is a thin presentation layer over it.

The product design is settled (Tetramark-ka8, user-approved 2026-11-08):
a bare call reads, every write is explicit (``--set`` / ``--clear``), a write
is one ``UPDATE`` inside one :meth:`~tetramark.core.ledger.Ledger.write`
session (one write lock, one Dolt commit), overwriting is silent and reports
the previous and the new parent, cycles are rejected, and ``--dry-run`` /
``--no-commit`` both *preview* the intended SQL and write nothing.  The last
rule is specific to these two commands; every other command keeps the global
``--no-commit`` meaning ("execute, do not commit").

Deferred on purpose (recorded as ``TODO(Tetramark-ka8)`` at the validation
site and in ``docs/spec/06-cli.md`` 6.7): no same-entity rule is enforced
(``tm stock add --parent`` never enforced one either), and the ledger-local
rules L02/L03 are not evaluated before a write; ``tm check`` reports them.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from contextlib import nullcontext
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from tetramark.core.objects import (
    OBJECT_TYPES,
    _check_acyclic,
    _check_child_placement,
    _fetch_core,
    _fetch_kind_row,
    _load_record,
    _quote,
    _refuse_box_inside_grid,
    _update,
)
from tetramark.errors import LedgerError, UsageError
from tetramark.schema.ddl import table_name
from tetramark.schema.model import FieldType

if TYPE_CHECKING:  # pragma: no cover - import only for type checkers
    from tetramark.core.ledger import Ledger

__all__ = [
    "RelationOps",
    "clear_parent",
    "clear_relation",
    "get_children",
    "get_parent",
    "get_relation",
    "set_parent",
]

#: Reserved relation field names of the core objects (spec 6.7 matrix rows 1-3).
_CORE_RELATION_FIELDS: dict[str, tuple[str, ...]] = {
    "stock": ("parent_stock", "source_aliquot"),
    "box": ("parent_box",),
}

_ALIQUOT_EXPLANATION = (
    "an aliquot is a generic tube with no kind table, so it has no relation "
    "fields of its own (its box placement belongs to tm aliquot move)"
)


# ---------------------------------------------------------------------------
# Field model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _RelationField:
    """One legal relation field of a child object.

    ``table``/``column`` are the physical storage, ``name`` is the bare field
    name ``--via`` takes, and ``parent_object``/``parent_kind`` say which
    parents it accepts (``parent_kind`` is ``None`` for a core-object target).
    """

    name: str
    table: str
    column: str
    parent_object: str
    parent_kind: str | None = None
    lineage: bool = False
    core: bool = False
    slot_column: str | None = None


def _kind_name(object_type: str, kind_name: str | None) -> str | None:
    if object_type == "aliquot" or kind_name is None:
        return None
    return kind_name


def _kind_fields(
    ledger: Ledger, object_type: str, kind_name: str
) -> list[_RelationField]:
    """The ``type: ref`` fields a kind declares, as relation fields."""
    kind = ledger.schema.kinds.get(kind_name)
    if kind is None:  # pragma: no cover - a resolved object always has a kind
        return []
    fields: list[_RelationField] = []
    for name, field in kind.fields.items():
        if field.type is not FieldType.ref or field.to is None:
            continue
        target = str(field.to)
        target_kind = ledger.schema.kinds.get(target)
        if target_kind is not None:
            parent_object = target_kind.object.value
            parent_kind: str | None = target
        elif target in OBJECT_TYPES:
            parent_object = target
            parent_kind = None
        else:  # pragma: no cover - the loader rejects such a target
            continue
        fields.append(
            _RelationField(
                name=name,
                table=table_name(object_type, kind_name),
                column=name,
                parent_object=parent_object,
                parent_kind=parent_kind,
                lineage=bool(field.lineage),
            )
        )
    return fields


def _core_fields(object_type: str) -> list[_RelationField]:
    if object_type == "stock":
        return [
            _RelationField(
                name="parent_stock",
                table="stock",
                column="parent_stock_id",
                parent_object="stock",
                core=True,
            ),
            _RelationField(
                name="source_aliquot",
                table="stock",
                column="source_aliquot_id",
                parent_object="aliquot",
                core=True,
            ),
        ]
    if object_type == "box":
        return [
            _RelationField(
                name="parent_box",
                table="box",
                column="parent_box_id",
                parent_object="box",
                core=True,
                slot_column="parent_slot",
            ),
        ]
    return []


def _legal_fields(
    ledger: Ledger, object_type: str, kind_name: str | None
) -> list[_RelationField]:
    fields = _core_fields(object_type)
    kind = _kind_name(object_type, kind_name)
    if kind is not None:
        fields.extend(_kind_fields(ledger, object_type, kind))
    return fields


def _object_label(object_type: str, kind_name: str | None) -> str:
    """``mouse_cohort`` for a kind object, ``aliquot`` for the core tube."""
    return kind_name or object_type


def _field_target_label(field: _RelationField) -> str:
    return field.parent_kind or field.parent_object


def _accepts(
    field: _RelationField, parent_object: str, parent_kind: str | None
) -> bool:
    if field.parent_object != parent_object:
        return False
    return field.parent_kind is None or field.parent_kind == parent_kind


def _object_kind(row: Mapping[str, Any]) -> str | None:
    value = row.get("kind")
    return None if value is None else str(value)


def _kind_of_row(
    session: Any, object_type: str, row: Mapping[str, Any]
) -> str:
    """The kind shown for an object: its own kind, or its stock's for a tube."""
    if object_type != "aliquot":
        return _object_kind(row) or ""
    stock_id = row.get("stock_id")
    if not stock_id:
        return ""
    stock = _fetch_core(session, "stock", str(stock_id))
    return "" if stock is None else str(stock.get("kind") or "")


def _describe_row(
    session: Any, object_type: str, row: Mapping[str, Any]
) -> dict[str, Any]:
    return {
        "object": object_type,
        "kind": _kind_of_row(session, object_type, row),
        "id": str(row["id"]),
    }


def _describe_id(
    session: Any, object_type: str, object_id: str, *, kind_hint: str | None = None
) -> dict[str, Any]:
    row = _fetch_core(session, object_type, object_id)
    if row is None:
        return {"object": object_type, "kind": kind_hint or "", "id": str(object_id)}
    if kind_hint is not None:
        return {"object": object_type, "kind": kind_hint, "id": str(object_id)}
    return _describe_row(session, object_type, row)


# ---------------------------------------------------------------------------
# Reference resolution (spec 6.7 (b))
# ---------------------------------------------------------------------------


def _check_via_name(via: str) -> str:
    """Normalise ``--via``: a bare lineage-style field name, never qualified."""
    name = str(via).strip()
    if not name:
        raise UsageError("--via needs a field name")
    if "." in name:
        bare = name.rsplit(".", 1)[1]
        raise UsageError(
            f"--via takes a bare field name, not {name!r}; use --via {bare}"
        )
    return name


def _via_scope(ledger: Ledger, name: str) -> set[str] | None:
    """The object types a ``--via`` field can belong to (``None`` = unknown)."""
    child_types: set[str] = set()
    for object_type, field_names in _CORE_RELATION_FIELDS.items():
        if name in field_names:
            child_types.add(object_type)
    for kind_name, kind in ledger.schema.kinds.items():
        field = kind.fields.get(name)
        if field is not None and field.type is FieldType.ref:
            child_types.add(kind.object.value)
    return child_types or None


def _type_order(types: Iterable[str] | None) -> tuple[str, ...]:
    if types is None:
        return OBJECT_TYPES
    allowed = set(types)
    return tuple(object_type for object_type in OBJECT_TYPES if object_type in allowed)


def _ambiguous_reference(text: str, hits: Sequence[tuple[str, Mapping[str, Any]]]) -> UsageError:
    described = ", ".join(
        f"{object_type} {row['id']}"
        + (f" ({row['kind']})" if row.get("kind") else "")
        for object_type, row in sorted(hits, key=lambda item: (item[0], str(item[1]["id"])))
    )
    return UsageError(
        f"object reference {text!r} is ambiguous: it matches {described}; "
        "use the full id"
    )


def _resolve_any(
    session: Any, ledger: Ledger, ref: str, *, types: Iterable[str] | None = None
) -> tuple[str, dict[str, Any]]:
    """Resolve a bare ref across object kinds, never picking silently.

    The tiers of :func:`tetramark.core.objects._resolve_core` are applied per
    object type and the deciding tier must produce exactly one hit: (1) an
    exact id in any object type (:data:`OBJECT_TYPES` order), (2) a box name or
    an exact alias, (3) a unique id prefix.  A lower tier is consulted only
    when the higher one found nothing.  Ambiguity is a usage error naming the
    candidates with their object types; a miss is a LedgerError.
    """
    text = str(ref).strip()
    if not text:
        raise UsageError("an object id or alias must not be empty")
    allowed = _type_order(types)

    # Tier 1: a full id (the object type order is OBJECT_TYPES).
    for object_type in allowed:
        row = _fetch_core(session, object_type, text)
        if row is not None:
            return object_type, dict(row)

    # Tier 2: a box name or an exact alias.
    hits: dict[tuple[str, str], tuple[str, dict[str, Any]]] = {}
    if "box" in allowed:
        for row in session.query(
            f"SELECT * FROM {_quote('box')} WHERE {_quote('name')} = %s", (text,)
        ):
            hits[("box", str(row["id"]))] = ("box", dict(row))
    for alias_row in session.query(
        f"SELECT {_quote('object_type')} AS {_quote('object_type')}, "
        f"{_quote('object_id')} AS {_quote('object_id')} "
        f"FROM {_quote('alias')} WHERE {_quote('alias')} = %s",
        (text,),
    ):
        object_type = str(alias_row["object_type"])
        if object_type not in allowed:
            continue
        object_id = str(alias_row["object_id"])
        row = _fetch_core(session, object_type, object_id)
        if row is not None:
            hits[(object_type, object_id)] = (object_type, dict(row))
    if len(hits) == 1:
        return next(iter(hits.values()))
    if len(hits) > 1:
        raise _ambiguous_reference(text, list(hits.values()))

    # Tier 3: a unique id prefix across the searched object types.
    matches: list[tuple[str, dict[str, Any]]] = []
    for object_type in allowed:
        rows = session.query(
            f"SELECT * FROM {_quote(object_type)} "
            f"WHERE LEFT({_quote('id')}, %s) = %s "
            f"ORDER BY {_quote('id')}",
            (len(text), text),
        )
        matches.extend((object_type, dict(row)) for row in rows)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        same_type = {object_type for object_type, _row in matches}
        if len(same_type) == 1:
            object_type = next(iter(same_type))
            shown = ", ".join(str(row["id"]) for _type, row in matches[:10])
            more = f" and {len(matches) - 10} more" if len(matches) > 10 else ""
            raise UsageError(
                f"{object_type} id {text!r} is ambiguous: it prefixes "
                f"{len(matches)} objects ({shown}{more}); use a longer prefix "
                "or the full id"
            )
        raise _ambiguous_reference(text, matches)

    raise LedgerError(f"unknown object {text!r}")


# ---------------------------------------------------------------------------
# Field selection (spec 6.7 (d))
# ---------------------------------------------------------------------------


def _field_by_name(fields: Sequence[_RelationField], name: str) -> _RelationField:
    for field in fields:
        if field.name == name:
            return field
    raise AssertionError(f"{name!r} was checked against the legal field set")


def _legal_field_error(
    object_type: str, object_id: str, kind_name: str | None, name: str,
    fields: Sequence[_RelationField],
) -> UsageError:
    choices = ", ".join(sorted(field.name for field in fields))
    label = _object_label(object_type, kind_name)
    if not choices:
        return UsageError(
            f"object {object_type} {object_id} ({label}) has no relation fields"
            f"{_ALIQUOT_EXPLANATION if object_type == 'aliquot' else ''}"
        )
    return UsageError(
        f"object {object_type} {object_id} ({label}) has no relation field "
        f"{name!r}; choose from: {choices}"
    )


def _select_field(
    ledger: Ledger,
    *,
    object_type: str,
    object_id: str,
    kind_name: str | None,
    parent_object: str,
    parent_kind: str | None,
    via: str | None,
) -> _RelationField:
    """Choose the one field for a child/parent pair (spec 6.7 (c)/(d))."""
    fields = _legal_fields(ledger, object_type, kind_name)
    label = _object_label(object_type, kind_name)

    if via is not None:
        name = _check_via_name(via)
        known = {field.name for field in fields}
        if name not in known:
            raise _legal_field_error(object_type, object_id, kind_name, name, fields)
        field = _field_by_name(fields, name)
        if not _accepts(field, parent_object, parent_kind):
            raise LedgerError(
                f"--via {name!r} points at {_field_target_label(field)}, but the "
                f"parent is {_object_label(parent_object, parent_kind)}: the "
                "field and the parent do not match"
            )
        return field

    candidates = [
        field
        for field in fields
        if _accepts(field, parent_object, parent_kind)
        and (field.core or field.lineage)
    ]
    if len(candidates) == 1:
        return candidates[0]
    if candidates:
        names = ", ".join(sorted(field.name for field in candidates))
        raise UsageError(
            f"{label} -> {_object_label(parent_object, parent_kind)} has more "
            f"than one relation field: {names}; pass --via FIELD"
        )
    raise LedgerError(
        f"no relation field connects {label} to "
        f"{_object_label(parent_object, parent_kind)}; pass --via FIELD "
        "(see docs/spec/06-cli.md 6.7 for the matrix)"
    )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def _stored_value(record: Mapping[str, Any], field: _RelationField) -> str | None:
    value = record.get(field.column)
    return None if value is None else str(value)


def _stored_slot(record: Mapping[str, Any], field: _RelationField) -> str | None:
    if field.slot_column is None:
        return None
    value = record.get(field.slot_column)
    return None if value is None else str(value)


def _relation_entry(
    *,
    field: _RelationField,
    child: Mapping[str, Any],
    parent: Mapping[str, Any],
    slot: str | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "via": field.name,
        "child": dict(child),
        "parent": dict(parent),
    }
    if field.name == "parent_box":
        entry["slot"] = slot
    return entry


def _parent_relations(
    session: Any,
    ledger: Ledger,
    *,
    object_type: str,
    record: Mapping[str, Any],
    via: str | None,
) -> list[dict[str, Any]]:
    object_id = str(record["id"])
    kind_name = _object_kind(record) if object_type != "aliquot" else None
    fields = _legal_fields(ledger, object_type, kind_name)
    if via is not None:
        name = _check_via_name(via)
        if name not in {field.name for field in fields}:
            raise _legal_field_error(object_type, object_id, kind_name, name, fields)
        fields = [field for field in fields if field.name == name]

    entries: list[dict[str, Any]] = []
    for field in fields:
        parent_id = _stored_value(record, field)
        if parent_id is None:
            continue
        parent = _describe_id(session, field.parent_object, parent_id, kind_hint=field.parent_kind)
        entries.append(
            _relation_entry(
                field=field,
                child=_describe_row(session, object_type, record),
                parent=parent,
                slot=_stored_slot(record, field),
            )
        )
    entries.sort(key=lambda entry: (entry["via"], entry["parent"]["id"]))
    return entries


def get_parent(ledger: Ledger, ref: str, *, via: str | None = None) -> dict[str, Any]:
    """Read every relation in which ``ref`` is the child (spec 6.7 (e)).

    Returns ``{"relations": [...]}``; an object with nothing set returns an
    empty list.  Never writes, never locks and never commits.
    """
    scope = _via_scope(ledger, _check_via_name(via)) if via is not None else None
    object_type, row = _resolve_any(ledger.driver, ledger, ref, types=scope)
    record = _load_record(ledger.driver, ledger, object_type, str(row["id"]))
    return {
        "relations": _parent_relations(
            ledger.driver, ledger, object_type=object_type, record=record, via=via
        )
    }


def _targets_parent(
    ledger: Ledger, field: Any, parent_object: str, parent_kind: str | None
) -> bool:
    target = str(field.to)
    target_kind = ledger.schema.kinds.get(target)
    if target_kind is not None:
        return (
            parent_object == target_kind.object.value and parent_kind == target
        )
    return parent_object == target


def _child_relations(
    session: Any,
    ledger: Ledger,
    *,
    parent_object: str,
    parent_record: Mapping[str, Any],
    via: str | None,
) -> list[dict[str, Any]]:
    """Every in-scope relation in which ``parent_record`` is the parent."""
    parent_id = str(parent_record["id"])
    parent_kind = _object_kind(parent_record) if parent_object != "aliquot" else None
    parent = _describe_row(session, parent_object, parent_record)
    via_name = _check_via_name(via) if via is not None else None

    # Core relation fields, by the core object the parent is.
    core_fields: list[str] = []
    if parent_object == "stock":
        core_fields.append("parent_stock")
    if parent_object == "aliquot":
        core_fields.append("source_aliquot")
    if parent_object == "box":
        core_fields.append("parent_box")

    # Every kind ``type: ref`` field whose ``to:`` accepts this parent.
    kind_fields: list[tuple[str, str, str]] = []
    for kind_name, kind in ledger.schema.kinds.items():
        for field_name, field in kind.fields.items():
            if field.type is not FieldType.ref or field.to is None:
                continue
            if not _targets_parent(
                ledger, field, parent_object, parent_kind
            ):
                continue
            kind_fields.append((kind_name, field_name, kind.object.value))

    valid_names = set(core_fields) | {field_name for _kind, field_name, _object in kind_fields}
    if via_name is not None and via_name not in valid_names:
        raise UsageError(
            f"--via {via_name!r} does not name a relation field that can point "
            f"at {parent_object} {parent['kind'] or parent_object} {parent_id}"
        )

    entries: list[dict[str, Any]] = []

    def add(
        field_name: str,
        child_object: str,
        child_row: Mapping[str, Any],
        child_kind: str | None,
        slot: str | None = None,
    ) -> None:
        if via_name is not None and field_name != via_name:
            return
        child = {
            "object": child_object,
            "kind": child_kind or _kind_of_row(session, child_object, child_row),
            "id": str(child_row["id"]),
        }
        entry: dict[str, Any] = {
            "via": field_name,
            "child": child,
            "parent": dict(parent),
        }
        if field_name == "parent_box":
            entry["slot"] = slot
        entries.append(entry)

    if "parent_stock" in core_fields:
        for row in session.query(
            f"SELECT * FROM {_quote('stock')} "
            f"WHERE {_quote('parent_stock_id')} = %s ORDER BY {_quote('id')}",
            (parent_id,),
        ):
            add("parent_stock", "stock", row, _object_kind(row))
    if "source_aliquot" in core_fields:
        for row in session.query(
            f"SELECT * FROM {_quote('stock')} "
            f"WHERE {_quote('source_aliquot_id')} = %s ORDER BY {_quote('id')}",
            (parent_id,),
        ):
            add("source_aliquot", "stock", row, _object_kind(row))
    if "parent_box" in core_fields:
        for row in session.query(
            f"SELECT * FROM {_quote('box')} "
            f"WHERE {_quote('parent_box_id')} = %s ORDER BY {_quote('id')}",
            (parent_id,),
        ):
            slot = None if row.get("parent_slot") is None else str(row["parent_slot"])
            add("parent_box", "box", row, _object_kind(row), slot)

    for kind_name, field_name, child_object in kind_fields:
        if via_name is not None and field_name != via_name:
            continue
        table = table_name(child_object, kind_name)
        rows = session.query(
            f"SELECT {_quote('id')} AS {_quote('id')} FROM {_quote(table)} "
            f"WHERE {_quote(field_name)} = %s ORDER BY {_quote('id')}",
            (parent_id,),
        )
        for row in rows:
            child_id = str(row["id"])
            child_row = _fetch_core(session, child_object, child_id)
            if child_row is None:  # pragma: no cover - the FK guarantees it
                continue
            add(field_name, child_object, child_row, kind_name)

    entries.sort(key=lambda entry: (entry["via"], entry["child"]["id"]))
    return entries


def get_children(ledger: Ledger, ref: str, *, via: str | None = None) -> dict[str, Any]:
    """Read every relation in which ``ref`` is the parent (spec 6.7 (e))."""
    object_type, row = _resolve_any(ledger.driver, ledger, ref)
    record = _load_record(ledger.driver, ledger, object_type, str(row["id"]))
    return {
        "relations": _child_relations(
            ledger.driver,
            ledger,
            parent_object=object_type,
            parent_record=record,
            via=via,
        )
    }


def get_relation(
    ledger: Ledger, child: str, parent: str, *, via: str | None = None
) -> dict[str, Any]:
    """Read the relation(s) between one child and one parent ref.

    Raises :class:`~tetramark.errors.LedgerError` (exit 1) naming both ids when
    no such relation exists, like the other not-found commands.
    """
    scope = _via_scope(ledger, _check_via_name(via)) if via is not None else None
    child_object, child_row = _resolve_any(ledger.driver, ledger, child, types=scope)
    parent_object, parent_row = _resolve_any(ledger.driver, ledger, parent)
    child_record = _load_record(
        ledger.driver, ledger, child_object, str(child_row["id"])
    )
    relations = _parent_relations(
        ledger.driver, ledger, object_type=child_object, record=child_record, via=via
    )
    matched = [
        entry for entry in relations
        if entry["parent"]["id"] == str(parent_row["id"])
        and entry["parent"]["object"] == parent_object
    ]
    if not matched:
        raise LedgerError(
            f"no relation between {str(child_row['id'])} and {str(parent_row['id'])}"
        )
    return {"relations": matched}


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def _previous_descriptor(
    session: Any, field: _RelationField, previous_id: str | None
) -> dict[str, Any] | None:
    if previous_id is None:
        return None
    return _describe_id(
        session, field.parent_object, previous_id, kind_hint=field.parent_kind
    )


def _aliquot_owner(session: Any, aliquot_id: str) -> str:
    rows = session.query(
        f"SELECT {_quote('stock_id')} AS {_quote('stock_id')} "
        f"FROM {_quote('aliquot')} WHERE {_quote('id')} = %s",
        (aliquot_id,),
    )
    if not rows:  # pragma: no cover - the FK guarantees the aliquot exists
        raise LedgerError(f"unknown aliquot {aliquot_id!r}")
    return str(rows[0]["stock_id"])


def _stock_parents(session: Any, stock_id: str) -> list[str]:
    """The upward edges of one stock: parent_stock and source_aliquot."""
    row = _fetch_core(session, "stock", stock_id)
    if row is None:  # pragma: no cover - the FK guarantees the stock exists
        return []
    parents: list[str] = []
    if row.get("parent_stock_id"):
        parents.append(str(row["parent_stock_id"]))
    if row.get("source_aliquot_id"):
        parents.append(_aliquot_owner(session, str(row["source_aliquot_id"])))
    return parents


def _check_stock_cycle(
    session: Any, *, child_id: str, target_stock_id: str, via: str
) -> None:
    """Reject a stock edge that would make the parent_stock/source_aliquot graph cyclic."""
    seen: set[str] = set()
    stack = [target_stock_id]
    while stack:
        current = stack.pop()
        if current == child_id:
            raise LedgerError(
                f"cannot set {via} of stock {child_id!r} to stock "
                f"{target_stock_id!r}: it would create a cycle in the "
                "parent_stock/source_aliquot graph"
            )
        if current in seen:  # a corrupted pre-existing cycle; stop politely
            continue
        seen.add(current)
        stack.extend(_stock_parents(session, current))


def _check_ref_cycle(
    session: Any, *, table: str, column: str, child_id: str, parent_id: str,
    field_name: str,
) -> None:
    """Reject a ref-field loop along that same field and kind table."""
    seen: set[str] = set()
    current: str | None = parent_id
    while current is not None:
        if current == child_id:
            raise LedgerError(
                f"cannot set {field_name} of object {child_id!r} to "
                f"{parent_id!r}: it would create a {field_name} cycle"
            )
        if current in seen:  # a corrupted pre-existing cycle; stop politely
            return
        seen.add(current)
        rows = session.query(
            f"SELECT {_quote(column)} AS {_quote('parent')} "
            f"FROM {_quote(table)} WHERE {_quote('id')} = %s",
            (current,),
        )
        if not rows:
            return
        parent = rows[0]["parent"]
        current = None if parent is None else str(parent)


def _validate_set(
    session: Any,
    ledger: Ledger,
    *,
    object_type: str,
    record: Mapping[str, Any],
    field: _RelationField,
    parent_object: str,
    parent_row: Mapping[str, Any],
    slot: str | None,
) -> str | None:
    """Run the in-scope structural checks before the single UPDATE.

    TODO(Tetramark-ka8): the same-entity rule is deferred (no such rule exists
    in this repository; tm stock add --parent never required it), and the
    ledger-local rules L02 and L03 (rules/legacy.yaml, pellet <-> glycerol) are
    not enforced at write time: tm check reports them.  There is deliberately
    no post-write tm check and no rollback.
    """
    child_id = str(record["id"])
    parent_id = str(parent_row["id"])

    if field.core and field.name == "parent_stock":
        _check_stock_cycle(
            session, child_id=child_id, target_stock_id=parent_id,
            via="parent_stock",
        )
        return None
    if field.core and field.name == "source_aliquot":
        _check_stock_cycle(
            session, child_id=child_id,
            target_stock_id=_aliquot_owner(session, parent_id),
            via="source_aliquot",
        )
        return None
    if field.core and field.name == "parent_box":
        parent_box = _fetch_core(session, "box", parent_id)
        if parent_box is None:  # pragma: no cover - resolution guarantees it
            raise LedgerError(f"unknown box {parent_id!r}")
        _refuse_box_inside_grid(ledger, parent_box)
        _check_acyclic(session, child_id, parent_box)
        return _check_child_placement(
            session, ledger, parent_box, slot, exclude_box=child_id
        )

    # A kind ref field: its column lives in the child's own kind table.
    kind_name = str(record["kind"])
    if _fetch_kind_row(session, kind_name, object_type, child_id) == {}:
        raise LedgerError(f"{object_type} {child_id!r} has no {kind_name} row")
    _check_ref_cycle(
        session,
        table=field.table,
        column=field.name,
        child_id=child_id,
        parent_id=parent_id,
        field_name=field.name,
    )
    return None


def _apply_set(
    session: Any,
    field: _RelationField,
    *,
    object_type: str,
    child_id: str,
    parent_id: str,
    slot: str | None,
) -> None:
    if field.core and field.name == "parent_box":
        _update(
            session, "box", {"id": child_id},
            {"parent_box_id": parent_id, "parent_slot": slot},
        )
        return
    if field.core:
        _update(session, field.table, {"id": child_id}, {field.column: parent_id})
        return
    _update(session, field.table, {"id": child_id}, {field.name: parent_id})


def _apply_clear(
    session: Any, field: _RelationField, *, object_type: str, child_id: str
) -> None:
    if field.core and field.name == "parent_box":
        _update(
            session, "box", {"id": child_id},
            {"parent_box_id": None, "parent_slot": None},
        )
        return
    if field.core:
        _update(session, field.table, {"id": child_id}, {field.column: None})
        return
    _update(session, field.table, {"id": child_id}, {field.name: None})


def _write_guard(ledger: Ledger, *, preview: bool):
    """Hold the writer lock for a real write; a preview takes no lock at all."""
    if preview:
        return nullcontext()
    return ledger.hold_write_lock(command="tm parent")


def _resolve_child_for_field(
    ledger: Ledger, child: str, via: str | None
) -> tuple[str, dict[str, Any]]:
    scope = _via_scope(ledger, _check_via_name(via)) if via is not None else None
    return _resolve_any(ledger.driver, ledger, child, types=scope)


def _write_result(
    session: Any,
    *,
    action: str,
    field: _RelationField,
    object_type: str,
    record: Mapping[str, Any],
    parent: Mapping[str, Any] | None,
    previous_id: str | None,
    slot: str | None,
    previous_slot: str | None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "action": action,
        "via": field.name,
        "child": _describe_row(session, object_type, record),
        "parent": None if parent is None else dict(parent),
        "previous": _previous_descriptor(session, field, previous_id),
    }
    if field.name == "parent_box":
        result["slot"] = slot
        result["previous_slot"] = previous_slot
    return result


def set_parent(
    ledger: Ledger, child: str, parent: str, *,
    via: str | None = None, slot: str | None = None,
) -> dict[str, Any]:
    """Make ``parent`` the parent of ``child`` (one write, one commit).

    ``--dry-run`` and ``--no-commit`` preview the intended SQL and write
    nothing (a command-specific rule, spec 6.7 (i)).
    """
    preview = bool(ledger.dry_run or ledger.no_commit)
    with _write_guard(ledger, preview=preview):
        object_type, child_row = _resolve_child_for_field(ledger, child, via)
        parent_object, parent_row = _resolve_any(ledger.driver, ledger, parent)
        record = _load_record(
            ledger.driver, ledger, object_type, str(child_row["id"])
        )
        kind_name = _object_kind(record) if object_type != "aliquot" else None
        field = _select_field(
            ledger, object_type=object_type, object_id=str(record["id"]),
            kind_name=kind_name, parent_object=parent_object,
            parent_kind=_object_kind(parent_row), via=via,
        )
        if slot is not None and field.name != "parent_box":
            raise UsageError(
                "--slot is only valid for a parent_box relation with --set"
            )
        child_id = str(record["id"])
        parent_id = str(parent_row["id"])
        summary = f"{child_id} {field.name} -> {parent_id}"
        with ledger.write(summary, verb="parent", object=object_type,
                         dry_run=preview) as session:
            record = _load_record(session, ledger, object_type, child_id)
            new_slot = _validate_set(
                session, ledger, object_type=object_type, record=record,
                field=field, parent_object=parent_object,
                parent_row=parent_row, slot=slot,
            )
            previous_id = _stored_value(record, field)
            previous_slot = _stored_slot(record, field)
            _apply_set(
                session, field, object_type=object_type, child_id=child_id,
                parent_id=parent_id, slot=new_slot,
            )
            return _write_result(
                session, action="set", field=field, object_type=object_type,
                record=record, parent=_describe_row(session, parent_object, parent_row),
                previous_id=previous_id, slot=new_slot,
                previous_slot=previous_slot,
            )


def _required_clear_error(field: _RelationField) -> LedgerError:
    return LedgerError(
        f"field {field.name!r} is required and cannot be cleared"
    )


def _no_parent_relations(
    session: Any, object_type: str, record: Mapping[str, Any]
) -> dict[str, Any]:
    """The read-only no-op result of a clear with nothing set (no session)."""
    return {
        "action": "clear",
        "via": None,
        "child": _describe_row(session, object_type, record),
        "parent": None,
        "previous": None,
    }


def clear_parent(
    ledger: Ledger, child: str, *, via: str | None = None
) -> dict[str, Any]:
    """Remove ``child``'s parent relation (one write, one commit).

    With ``via`` that field is cleared; without it the single currently set
    in-scope field is cleared, several set fields ask for ``--via``, and
    nothing set is a read-only no-op (no session, no commit).
    """
    preview = bool(ledger.dry_run or ledger.no_commit)
    scope = _via_scope(ledger, _check_via_name(via)) if via is not None else None
    with _write_guard(ledger, preview=preview):
        object_type, child_row = _resolve_any(ledger.driver, ledger, child, types=scope)
        record = _load_record(ledger.driver, ledger, object_type, str(child_row["id"]))
        kind_name = _object_kind(record) if object_type != "aliquot" else None
        fields = _legal_fields(ledger, object_type, kind_name)
        if via is not None:
            name = _check_via_name(via)
            if name not in {field.name for field in fields}:
                raise _legal_field_error(
                    object_type, str(record["id"]), kind_name, name, fields
                )
            field = _field_by_name(fields, name)
            if _stored_value(record, field) is None:
                return _no_parent_relations(ledger.driver, object_type, record)
            if not field.core and _is_required(ledger, record, field):
                raise _required_clear_error(field)
        else:
            set_fields = [
                field for field in fields if _stored_value(record, field) is not None
            ]
            if not set_fields:
                return _no_parent_relations(ledger.driver, object_type, record)
            if len(set_fields) > 1:
                names = ", ".join(sorted(field.name for field in set_fields))
                raise UsageError(
                    f"object {object_type} {str(record['id'])} "
                    f"({_object_label(object_type, kind_name)}) has more than "
                    f"one parent relation set: {names}; pass --via FIELD"
                )
            field = set_fields[0]
            if not field.core and _is_required(ledger, record, field):
                raise _required_clear_error(field)
        child_id = str(record["id"])
        summary = f"{child_id} {field.name} cleared"
        with ledger.write(summary, verb="parent", object=object_type,
                         dry_run=preview) as session:
            record = _load_record(session, ledger, object_type, child_id)
            previous_id = _stored_value(record, field)
            if previous_id is None:
                return _no_parent_relations(session, object_type, record)
            previous_slot = _stored_slot(record, field)
            _apply_clear(session, field, object_type=object_type, child_id=child_id)
            return _write_result(
                session, action="clear", field=field, object_type=object_type,
                record=record, parent=None, previous_id=previous_id,
                slot=None, previous_slot=previous_slot,
            )


def _is_required(ledger: Ledger, record: Mapping[str, Any], field: _RelationField) -> bool:
    kind_name = _object_kind(record)
    if kind_name is None:
        return False
    kind = ledger.schema.kinds.get(kind_name)
    if kind is None:
        return False
    schema_field = kind.fields.get(field.name)
    return bool(schema_field is not None and schema_field.required)


def clear_relation(
    ledger: Ledger, child: str, parent: str, *, via: str | None = None
) -> dict[str, Any]:
    """Remove the relation between ``child`` and ``parent`` (the two-ref clear).

    The field is selected exactly as a ``--set`` selects it (one candidate is
    inferred, several need ``--via``), and the stored value must be that
    parent; otherwise the command reports "no relation between ..." and writes
    nothing.
    """
    preview = bool(ledger.dry_run or ledger.no_commit)
    with _write_guard(ledger, preview=preview):
        object_type, child_row = _resolve_child_for_field(ledger, child, via)
        parent_object, parent_row = _resolve_any(ledger.driver, ledger, parent)
        record = _load_record(
            ledger.driver, ledger, object_type, str(child_row["id"])
        )
        kind_name = _object_kind(record) if object_type != "aliquot" else None
        field = _select_field(
            ledger, object_type=object_type, object_id=str(record["id"]),
            kind_name=kind_name, parent_object=parent_object,
            parent_kind=_object_kind(parent_row), via=via,
        )
        if _stored_value(record, field) != str(parent_row["id"]):
            raise LedgerError(
                f"no relation between {str(record['id'])} and "
                f"{str(parent_row['id'])}"
            )
        if not field.core and _is_required(ledger, record, field):
            raise _required_clear_error(field)
        child_id = str(record["id"])
        summary = f"{child_id} {field.name} cleared"
        with ledger.write(summary, verb="parent", object=object_type,
                         dry_run=preview) as session:
            record = _load_record(session, ledger, object_type, child_id)
            previous_slot = _stored_slot(record, field)
            if _stored_value(record, field) != str(parent_row["id"]):
                raise LedgerError(
                    f"no relation between {child_id} and {str(parent_row['id'])}"
                )
            _apply_clear(session, field, object_type=object_type, child_id=child_id)
            return _write_result(
                session, action="clear", field=field, object_type=object_type,
                record=record, parent=None, previous_id=str(parent_row["id"]),
                slot=None, previous_slot=previous_slot,
            )


# ---------------------------------------------------------------------------
# Ledger facade
# ---------------------------------------------------------------------------


class RelationOps:
    """``ledger.relations`` — the Python API behind ``tm parent`` / ``tm child``."""

    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger

    def get_parent(self, ref: str, *, via: str | None = None) -> dict[str, Any]:
        return get_parent(self.ledger, ref, via=via)

    def get_children(self, ref: str, *, via: str | None = None) -> dict[str, Any]:
        return get_children(self.ledger, ref, via=via)

    def get_relation(
        self, child: str, parent: str, *, via: str | None = None
    ) -> dict[str, Any]:
        return get_relation(self.ledger, child, parent, via=via)

    def set_parent(
        self, child: str, parent: str, *,
        via: str | None = None, slot: str | None = None,
    ) -> dict[str, Any]:
        return set_parent(self.ledger, child, parent, via=via, slot=slot)

    def clear_parent(self, child: str, *, via: str | None = None) -> dict[str, Any]:
        return clear_parent(self.ledger, child, via=via)

    def clear_relation(
        self, child: str, parent: str, *, via: str | None = None
    ) -> dict[str, Any]:
        return clear_relation(self.ledger, child, parent, via=via)
