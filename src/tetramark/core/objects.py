"""Object operations: entity, stock, aliquot, box and alias (spec §1, §6).

Every public function in this module is one *command*: it validates its input,
writes through :meth:`tetramark.core.ledger.Ledger.write` (one SQL transaction,
one Dolt commit) and returns a plain dictionary describing what changed.  The
CLI groups in :mod:`tetramark.cli` are thin wrappers around these functions;
the Python API is reached through ``ledger.entities``, ``ledger.stocks``,
``ledger.aliquots``, ``ledger.boxes`` and ``ledger.aliases``.

Validation split
----------------
The database enforces primary keys, foreign keys, ``UNIQUE``, ``NOT NULL`` and
enum checks.  This module adds the rules Dolt cannot express (spec §3):

* ``--set`` keys must be fields of the kind (unknown key → usage error) and
  values are coerced/validated from the schema (enum, pattern, sequence
  normalisation, ref resolution by id or alias);
* a stock's entity kind must satisfy ``entity_kinds``;
* placement: the slot exists, the slot's capacity is respected, no child box is
  in an aliquot's slot, and a slotless box's total capacity is respected;
* a stored aliquot in a slotted box needs a slot;
* box nesting must be acyclic and a slot holds aliquots *or* one child box.

Placement rules are checked through the open :class:`WriteSession`, so several
aliquots created by one command see each other (the embedded driver replays the
buffered statements in a rolled-back transaction when queried).
"""

from __future__ import annotations

import datetime as _dt
import re
import uuid
from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tetramark.core.events import purge_events, record_event
from tetramark.core.ids import IdError, aliquot_id, next_aliquot_id, parse_aliquot_id
from tetramark.errors import LedgerError, UsageError
from tetramark.schema.ddl import slot_rows, table_name
from tetramark.schema.model import Field, FieldType, Kind

if TYPE_CHECKING:  # pragma: no cover - import only for type checkers
    from tetramark.core.ledger import Ledger

__all__ = [
    "OBJECT_TYPES",
    "AliasOps",
    "AliquotOps",
    "BoxOps",
    "EntityOps",
    "StockOps",
    "add_alias",
    "add_aliquots",
    "add_box",
    "add_entity",
    "add_stock",
    "edit_entity",
    "edit_stock",
    "list_entities",
    "list_boxes",
    "list_stocks",
    "move_aliquot",
    "move_box",
    "purge_aliquot",
    "read_rename_map",
    "remove_alias",
    "renumber_stocks",
    "set_aliquot_status",
    "show_entity",
    "show_stock",
    "use_aliquot",
]

#: Core object types aliases can point at.
OBJECT_TYPES: tuple[str, ...] = ("entity", "stock", "aliquot", "box")

#: Core columns a ``--set`` may address on each object (everything else is a
#: kind field).  ``id``/``kind`` are deliberately absent: they are identity, not
#: editable data.
_CORE_EDITABLE: dict[str, frozenset[str]] = {
    "entity": frozenset({"name", "notes"}),
    "stock": frozenset({"notes"}),
    "box": frozenset({"name", "notes"}),
}

#: Core columns available to ``--where`` in addition to the kind fields.
_CORE_FILTERS: dict[str, frozenset[str]] = {
    "entity": frozenset({"id", "kind", "name", "created_at", "notes"}),
    "stock": frozenset(
        {
            "id",
            "kind",
            "entity_id",
            "parent_stock_id",
            "source_aliquot_id",
            "created_at",
            "notes",
        }
    ),
    "box": frozenset(
        {"id", "kind", "name", "parent_box_id", "parent_slot", "created_at", "notes"}
    ),
}

_BOOL_WORDS = {
    "true": True,
    "t": True,
    "yes": True,
    "y": True,
    "on": True,
    "1": True,
    "false": False,
    "f": False,
    "no": False,
    "n": False,
    "off": False,
    "0": False,
}

_INT_RE = re.compile(r"[+-]?\d+")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _quote(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


def _kind_for(ledger: Ledger, object_type: str, kind_name: str) -> Kind:
    """The resolved kind, or a usage error listing the valid choices."""
    kinds = ledger.schema.kinds_for(object_type)
    kind = kinds.get(kind_name)
    if kind is not None:
        return kind
    if ledger.schema.has_kind(kind_name):
        other = ledger.schema.kinds[kind_name].object.value
        raise UsageError(
            f"kind {kind_name!r} is a {other} kind, not a {object_type} kind"
        )
    valid = ", ".join(sorted(kinds)) or "(none defined)"
    raise UsageError(f"unknown {object_type} kind {kind_name!r}; choose from: {valid}")


def _normalise_values(values: Iterable[str] | Mapping[str, Any] | None) -> tuple[str, ...]:
    """Accept ``["a=1", "b=2"]`` or ``{"a": 1, "b": 2}``."""
    if values is None:
        return ()
    if isinstance(values, Mapping):
        return tuple(f"{key}={value}" for key, value in values.items())
    return tuple(str(item) for item in values)


def _normalise_aliases(
    alias: str | Iterable[str] | None, aliases: Iterable[str] | None
) -> list[str]:
    result: list[str] = []
    if alias is not None:
        result.extend([alias] if isinstance(alias, str) else list(alias))
    if aliases is not None:
        result.extend(list(aliases))
    cleaned: list[str] = []
    for value in result:
        text = str(value).strip()
        if not text:
            raise UsageError("aliases must not be empty")
        if text not in cleaned:
            cleaned.append(text)
    return cleaned


def _parse_pairs(
    values: Iterable[str] | Mapping[str, Any] | None, *, option: str = "--set"
) -> dict[str, str]:
    """Parse ``field=value`` pairs, rejecting malformed and duplicate keys."""
    parsed: dict[str, str] = {}
    for item in _normalise_values(values):
        if "=" not in item:
            raise UsageError(f"invalid {option} {item!r}: expected field=value")
        field, _, value = item.partition("=")
        field = field.strip()
        if not field:
            raise UsageError(f"invalid {option} {item!r}: the field name is empty")
        if field in parsed:
            raise UsageError(f"field {field!r} appears twice in {option}")
        parsed[field] = value
    return parsed


# ---------------------------------------------------------------------------
# Field coercion
# ---------------------------------------------------------------------------


def _resolve_ref(
    session: Any, ledger: Ledger, target: str, raw: str
) -> str:
    """Resolve a ``ref`` value by ID (kind or core table) or by alias."""
    kind = ledger.schema.kinds.get(target)
    if kind is not None:
        object_type = kind.object.value
        table = table_name(kind.object, target)
    elif target in OBJECT_TYPES:
        object_type = target
        table = target
    else:  # loader already rejects this; defensive
        raise UsageError(f"field refers to unknown target {target!r}")

    if session.query(
        f"SELECT {_quote('id')} FROM {_quote(table)} WHERE {_quote('id')} = %s",
        (raw,),
    ):
        return raw

    aliases = session.query(
        f"SELECT {_quote('object_id')} FROM {_quote('alias')} "
        f"WHERE {_quote('object_type')} = %s AND {_quote('alias')} = %s",
        (object_type, raw),
    )
    for row in aliases:
        object_id = row["object_id"]
        if session.query(
            f"SELECT {_quote('id')} FROM {_quote(table)} WHERE {_quote('id')} = %s",
            (object_id,),
        ):
            return str(object_id)

    # Give a precise message when the ID exists but belongs to another kind.
    rows = session.query(
        f"SELECT {_quote('kind')} FROM {_quote(object_type)} WHERE {_quote('id')} = %s",
        (raw,),
    )
    if rows:
        raise UsageError(
            f"{raw!r} is a {rows[0]['kind']!r} {object_type}, not a {target!r}"
        )
    raise UsageError(
        f"cannot resolve {raw!r} for reference to {target!r} "
        "(no such id, and no alias for it)"
    )


def _coerce_field(
    field: Field,
    raw: str,
    *,
    ledger: Ledger,
    session: Any,
    label: str,
) -> Any:
    """Coerce one textual ``--set`` value according to the field's schema."""
    kind = field.type
    value: Any

    if kind is FieldType.computed:
        raise UsageError(f"field {label!r} is computed and cannot be set")

    if kind in (FieldType.string, FieldType.text):
        value = raw
        if kind is FieldType.string and len(value) > field.string_length:
            raise UsageError(
                f"field {label!r} is limited to {field.string_length} characters "
                f"({len(value)} given)"
            )
    elif kind is FieldType.int:
        text = raw.strip()
        if not _INT_RE.fullmatch(text):
            raise UsageError(f"field {label!r} must be an integer (got {raw!r})")
        value = int(text)
    elif kind is FieldType.decimal:
        try:
            value = Decimal(raw.strip())
        except (InvalidOperation, ValueError):
            raise UsageError(
                f"field {label!r} must be a decimal number (got {raw!r})"
            ) from None
        if not value.is_finite():
            raise UsageError(f"field {label!r} must be a finite number (got {raw!r})")
    elif kind is FieldType.bool:
        word = raw.strip().lower()
        if word not in _BOOL_WORDS:
            raise UsageError(
                f"field {label!r} must be a boolean (true/false, yes/no, 1/0; "
                f"got {raw!r})"
            )
        value = _BOOL_WORDS[word]
    elif kind is FieldType.date:
        try:
            value = _dt.date.fromisoformat(raw.strip())
        except ValueError:
            raise UsageError(
                f"field {label!r} must be an ISO date YYYY-MM-DD (got {raw!r})"
            ) from None
    elif kind is FieldType.datetime:
        try:
            value = _dt.datetime.fromisoformat(raw.strip())
        except ValueError:
            raise UsageError(
                f"field {label!r} must be an ISO datetime (got {raw!r})"
            ) from None
    elif kind is FieldType.enum:
        value = raw
        if value not in (field.values or []):
            choices = ", ".join(field.values or [])
            raise UsageError(
                f"field {label!r} must be one of: {choices} (got {raw!r})"
            )
    elif kind is FieldType.sequence:
        value = re.sub(r"\s+", "", raw).upper()
        if not value:
            raise UsageError(f"field {label!r} is a sequence and must not be empty")
    elif kind is FieldType.ref:
        assert field.to is not None
        value = _resolve_ref(session, ledger, field.to, raw)
    else:  # pragma: no cover - FieldType is exhaustive
        raise UsageError(f"field {label!r} has an unsupported type {kind!r}")

    if field.pattern is not None and isinstance(value, str):
        if re.fullmatch(field.pattern, value) is None:
            raise UsageError(
                f"field {label!r} does not match the pattern {field.pattern!r} "
                f"(got {value!r})"
            )
    return value


def _split_values(
    session: Any,
    ledger: Ledger,
    kind: Kind,
    kind_name: str,
    object_type: str,
    raw: dict[str, str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split ``--set`` keys into core columns and kind-table fields."""
    valid = set(kind.fields) | set(_CORE_EDITABLE[object_type])
    unknown = sorted(set(raw) - valid)
    if unknown:
        choices = ", ".join(sorted(valid)) or "(none)"
        raise UsageError(
            f"unknown field(s) {', '.join(repr(name) for name in unknown)} "
            f"for {object_type} kind {kind_name!r}; valid fields: {choices}"
        )

    core: dict[str, Any] = {}
    fields: dict[str, Any] = {}
    for name, value in raw.items():
        if name in kind.fields:
            fields[name] = _coerce_field(
                kind.fields[name], value, ledger=ledger, session=session, label=name
            )
        else:
            core[name] = value
    return core, fields


def _require_fields(kind: Kind, kind_name: str, fields: Mapping[str, Any]) -> None:
    missing = [
        name
        for name, schema_field in kind.fields.items()
        if schema_field.required and not schema_field.has_default and name not in fields
    ]
    if missing:
        raise UsageError(
            f"missing required field(s) for kind {kind_name!r}: "
            + ", ".join(sorted(missing))
        )


def _mirror_name(kind: Kind, core: dict[str, Any], fields: dict[str, Any]) -> None:
    """Keep ``entity.name`` and a kind's own ``name`` field in sync.

    A kind that declares ``name`` (the common case, via the ``named_fields``
    mixin) gets the value in its own table; the core ``entity.name`` column is
    filled with the same text so ``find``/``ls`` see one name.
    """
    if "name" not in kind.fields:
        return
    if fields.get("name") is not None and core.get("name") is None:
        core["name"] = fields["name"]
    elif core.get("name") is not None and fields.get("name") is None:
        fields["name"] = core["name"]


def _parse_quantity_unit(quantity: Any, unit: Any) -> tuple[Decimal | None, str | None]:
    if quantity is None and unit is None:
        return None, None
    if quantity is None or unit is None:
        raise UsageError("--quantity and --unit must be given together")
    try:
        amount = Decimal(str(quantity).strip())
    except (InvalidOperation, ValueError):
        raise UsageError(f"quantity must be a decimal number (got {quantity!r})") from None
    if not amount.is_finite() or amount < 0:
        raise UsageError(f"quantity must be a finite, non-negative number (got {quantity!r})")
    text = str(unit).strip()
    if not text:
        raise UsageError("unit must not be empty")
    if len(text) > 32:
        raise UsageError("unit must be at most 32 characters")
    return amount, text


def _parse_count(value: Any, *, option: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise UsageError(f"{option} must be an integer (got {value!r})")
    if value < 0:
        raise UsageError(f"{option} must not be negative")
    return value


# ---------------------------------------------------------------------------
# SQL helpers
# ---------------------------------------------------------------------------


def _insert(session: Any, table: str, payload: Mapping[str, Any]) -> None:
    columns = list(payload)
    if not columns:
        raise LedgerError(f"refusing an empty insert into {table!r}")
    sql = (
        f"INSERT INTO {_quote(table)} "
        f"({', '.join(_quote(column) for column in columns)}) "
        f"VALUES ({', '.join('%s' for _ in columns)})"
    )
    session.execute(sql, tuple(payload[column] for column in columns))


def _update(
    session: Any, table: str, key: Mapping[str, Any], payload: Mapping[str, Any]
) -> None:
    if not payload:
        return
    assignments = ", ".join(f"{_quote(column)} = %s" for column in payload)
    conditions = " AND ".join(f"{_quote(column)} = %s" for column in key)
    sql = (
        f"UPDATE {_quote(table)} SET {assignments} WHERE {conditions}"
    )
    session.execute(sql, (*payload.values(), *key.values()))


def _fetch_core(session: Any, object_type: str, object_id: str) -> dict[str, Any] | None:
    rows = session.query(
        f"SELECT * FROM {_quote(object_type)} WHERE {_quote('id')} = %s",
        (object_id,),
    )
    return dict(rows[0]) if rows else None


def _fetch_kind_row(
    session: Any, kind_name: str, object_type: str, object_id: str
) -> dict[str, Any]:
    rows = session.query(
        f"SELECT * FROM {_quote(table_name(object_type, kind_name))} "
        f"WHERE {_quote('id')} = %s",
        (object_id,),
    )
    return dict(rows[0]) if rows else {}


def _resolve_core(session: Any, object_type: str, ref: str) -> dict[str, Any]:
    """Find a core row by ID, then by alias (ID wins, as ``find`` does)."""
    row = _fetch_core(session, object_type, ref)
    if row is not None:
        return row
    aliases = session.query(
        f"SELECT {_quote('object_id')} FROM {_quote('alias')} "
        f"WHERE {_quote('object_type')} = %s AND {_quote('alias')} = %s",
        (object_type, ref),
    )
    for alias_row in aliases:
        row = _fetch_core(session, object_type, str(alias_row["object_id"]))
        if row is not None:
            return row
    raise LedgerError(f"unknown {object_type} {ref!r}")


def _resolve_box(session: Any, ref: str) -> dict[str, Any]:
    """Find a box by its human name (preferred), then id, then alias."""
    rows = session.query(
        f"SELECT * FROM {_quote('box')} WHERE {_quote('name')} = %s", (ref,)
    )
    if rows:
        return dict(rows[0])
    return _resolve_core(session, "box", ref)


def _load_record(
    session: Any, ledger: Ledger, object_type: str, object_id: str
) -> dict[str, Any]:
    core = _fetch_core(session, object_type, object_id)
    if core is None:  # pragma: no cover - callers just resolved it
        raise LedgerError(f"unknown {object_type} {object_id!r}")
    if object_type == "aliquot":
        stock = _fetch_core(session, "stock", str(core["stock_id"]))
        if stock is None:  # pragma: no cover - FK guarantees it
            raise LedgerError(f"aliquot {object_id!r} has no stock")
        kind_name = str(stock["kind"])
        # Only stock kinds with aliquot_fields have an extension table.
        has_extension = bool(ledger.schema.kind(kind_name).aliquot_fields)
    else:
        kind_name = str(core["kind"])
        has_extension = True
    kind_row = (
        _fetch_kind_row(session, kind_name, object_type, object_id)
        if has_extension
        else {}
    )
    record = dict(core)
    for key, value in kind_row.items():
        if key != "id":
            record[key] = value
    record["object"] = object_type
    return record


# ---------------------------------------------------------------------------
# Box geometry / placement
# ---------------------------------------------------------------------------


def _box_geometry(kind: Kind) -> Any:
    if kind.geometry is None:  # pragma: no cover - box kinds always have one
        raise LedgerError(f"box kind {kind} has no geometry")
    return kind.geometry


def _check_aliquot_placement(
    session: Any,
    ledger: Ledger,
    box_row: Mapping[str, Any],
    slot: str | None,
    *,
    exclude_aliquot: str | None = None,
) -> str | None:
    """Validate that an aliquot may occupy ``slot`` in ``box_row``.

    Returns the normalised slot (``None`` for a slotless box).  Raises
    :class:`LedgerError` when the slot is missing, occupied by a child box, or
    already at capacity, and when a slotless box is at its capacity.
    """
    box_kind = ledger.schema.kind(str(box_row["kind"]))
    geometry = _box_geometry(box_kind)
    box_id = str(box_row["id"])
    label = f"{box_row['name']}"

    if geometry.is_slotted:
        text = slot.strip() if isinstance(slot, str) else slot
        if not text:
            raise LedgerError(
                f"box {label!r} has slots: every stored aliquot needs --slot"
            )
        exists = session.query(
            f"SELECT {_quote('label')} FROM {_quote('slot')} "
            f"WHERE {_quote('box_id')} = %s AND {_quote('label')} = %s",
            (box_id, text),
        )
        if not exists:
            raise LedgerError(f"box {label!r} has no slot {text!r}")
        child = session.query(
            f"SELECT {_quote('id')} FROM {_quote('box')} "
            f"WHERE {_quote('parent_box_id')} = %s AND {_quote('parent_slot')} = %s",
            (box_id, text),
        )
        if child:
            raise LedgerError(
                f"slot {text!r} in box {label!r} is occupied by child box "
                f"{child[0]['id']!r}"
            )
        sql = (
            f"SELECT COUNT(*) AS {_quote('n')} FROM {_quote('aliquot')} "
            f"WHERE {_quote('box_id')} = %s AND {_quote('slot')} = %s"
        )
        params: list[Any] = [box_id, text]
        if exclude_aliquot is not None:
            sql += f" AND {_quote('id')} <> %s"
            params.append(exclude_aliquot)
        used = int(session.query(sql, tuple(params))[0]["n"])
        if used >= box_kind.slot_capacity:
            raise LedgerError(
                f"slot {text!r} in box {label!r} is full "
                f"(capacity {box_kind.slot_capacity})"
            )
        return text

    if slot not in (None, ""):
        raise LedgerError(f"box {label!r} has no slots; omit --slot")
    capacity = geometry.capacity
    if capacity is not None:
        sql = (
            f"SELECT COUNT(*) AS {_quote('n')} FROM {_quote('aliquot')} "
            f"WHERE {_quote('box_id')} = %s"
        )
        params = [box_id]
        if exclude_aliquot is not None:
            sql += f" AND {_quote('id')} <> %s"
            params.append(exclude_aliquot)
        used = int(session.query(sql, tuple(params))[0]["n"])
        used += int(
            session.query(
                f"SELECT COUNT(*) AS {_quote('n')} FROM {_quote('box')} "
                f"WHERE {_quote('parent_box_id')} = %s",
                (box_id,),
            )[0]["n"]
        )
        if used >= capacity:
            raise LedgerError(
                f"box {label!r} is full (capacity {capacity} aliquots + child boxes)"
            )
    return None


def _check_child_placement(
    session: Any,
    ledger: Ledger,
    parent_row: Mapping[str, Any],
    slot: str | None,
    *,
    exclude_box: str | None = None,
) -> str | None:
    """Validate that a child box may occupy ``slot`` of ``parent_row``."""
    parent_kind = ledger.schema.kind(str(parent_row["kind"]))
    geometry = _box_geometry(parent_kind)
    parent_id = str(parent_row["id"])
    label = f"{parent_row['name']}"

    if geometry.is_slotted:
        text = slot.strip() if isinstance(slot, str) else slot
        if not text:
            raise LedgerError(f"box {label!r} has slots: --slot is required")
        exists = session.query(
            f"SELECT {_quote('label')} FROM {_quote('slot')} "
            f"WHERE {_quote('box_id')} = %s AND {_quote('label')} = %s",
            (parent_id, text),
        )
        if not exists:
            raise LedgerError(f"box {label!r} has no slot {text!r}")
        aliquot = session.query(
            f"SELECT {_quote('id')} FROM {_quote('aliquot')} "
            f"WHERE {_quote('box_id')} = %s AND {_quote('slot')} = %s",
            (parent_id, text),
        )
        if aliquot:
            raise LedgerError(
                f"slot {text!r} in box {label!r} is occupied by aliquot "
                f"{aliquot[0]['id']!r}"
            )
        sql = (
            f"SELECT {_quote('id')} FROM {_quote('box')} "
            f"WHERE {_quote('parent_box_id')} = %s AND {_quote('parent_slot')} = %s"
        )
        params: list[Any] = [parent_id, text]
        if exclude_box is not None:
            sql += f" AND {_quote('id')} <> %s"
            params.append(exclude_box)
        child = session.query(sql, tuple(params))
        if child:
            raise LedgerError(
                f"slot {text!r} in box {label!r} already holds child box "
                f"{child[0]['id']!r}"
            )
        return text

    if slot not in (None, ""):
        raise LedgerError(f"box {label!r} has no slots; omit --slot")
    capacity = geometry.capacity
    if capacity is not None:
        sql = (
            f"SELECT COUNT(*) AS {_quote('n')} FROM {_quote('aliquot')} "
            f"WHERE {_quote('box_id')} = %s"
        )
        used = int(session.query(sql, (parent_id,))[0]["n"])
        sql = (
            f"SELECT COUNT(*) AS {_quote('n')} FROM {_quote('box')} "
            f"WHERE {_quote('parent_box_id')} = %s"
        )
        params = [parent_id]
        if exclude_box is not None:
            sql += f" AND {_quote('id')} <> %s"
            params.append(exclude_box)
        used += int(session.query(sql, tuple(params))[0]["n"])
        if used >= capacity:
            raise LedgerError(
                f"box {label!r} is full (capacity {capacity} aliquots + child boxes)"
            )
    return None


def _check_acyclic(
    session: Any, box_id: str, parent_row: Mapping[str, Any]
) -> None:
    """Refuse a move that would put a box inside one of its descendants."""
    current: Mapping[str, Any] | None = parent_row
    seen: set[str] = set()
    while current is not None:
        current_id = str(current["id"])
        if current_id == box_id:
            raise LedgerError(
                f"cannot nest box {box_id!r} inside itself or one of its children"
            )
        if current_id in seen:  # corrupted pre-existing cycle; stop politely
            return
        seen.add(current_id)
        parent_id = current.get("parent_box_id")
        if not parent_id:
            return
        current = _fetch_core(session, "box", str(parent_id))


def _box_paths(rows: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    by_id = {str(row["id"]): row for row in rows}
    paths: dict[str, str] = {}
    for row in rows:
        parts: list[str] = []
        seen: set[str] = set()
        current: Mapping[str, Any] | None = row
        while current is not None:
            current_id = str(current["id"])
            if current_id in seen:
                break
            seen.add(current_id)
            parts.append(str(current["name"]))
            parent_id = current.get("parent_box_id")
            current = by_id.get(str(parent_id)) if parent_id else None
        paths[str(row["id"])] = "/".join(reversed(parts))
    return paths


# ---------------------------------------------------------------------------
# Alias rows (used by entity add and the alias command group)
# ---------------------------------------------------------------------------


def _check_object_type(object_type: str) -> str:
    text = str(object_type).strip().lower()
    if text not in OBJECT_TYPES:
        raise UsageError(
            f"unknown object type {object_type!r}; choose from: "
            + ", ".join(OBJECT_TYPES)
        )
    return text


def _add_alias_row(
    session: Any, object_type: str, object_id: str, alias: str
) -> None:
    text = str(alias).strip()
    if not text:
        raise UsageError("an alias must not be empty")
    existing = session.query(
        f"SELECT {_quote('object_id')} FROM {_quote('alias')} "
        f"WHERE {_quote('object_type')} = %s AND {_quote('alias')} = %s",
        (object_type, text),
    )
    if existing:
        owner = existing[0]["object_id"]
        if owner == object_id:
            raise LedgerError(
                f"{object_type} {object_id!r} already has the alias {text!r}"
            )
        raise LedgerError(
            f"alias {text!r} is already used by {object_type} {owner!r}"
        )
    _insert(
        session,
        "alias",
        {"object_type": object_type, "object_id": object_id, "alias": text},
    )


# ---------------------------------------------------------------------------
# Listings
# ---------------------------------------------------------------------------


def _list_records(
    ledger: Ledger,
    object_type: str,
    kind_name: str | None,
    where: Iterable[str] | Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    if kind_name is not None:
        _kind_for(ledger, object_type, kind_name)
        kinds = [kind_name]
    else:
        kinds = sorted(ledger.schema.kinds_for(object_type))

    filters = _parse_pairs(where, option="--where")
    valid: set[str] = set(_CORE_FILTERS[object_type])
    for name in kinds:
        valid |= set(ledger.schema.kinds[name].fields)
    unknown = sorted(set(filters) - valid)
    if unknown:
        raise UsageError(
            f"unknown field(s) {', '.join(repr(name) for name in unknown)} in --where; "
            f"valid fields: {', '.join(sorted(valid))}"
        )

    records: list[dict[str, Any]] = []
    for name in kinds:
        kind = ledger.schema.kinds[name]
        columns = set(_CORE_FILTERS[object_type]) | set(kind.fields)
        # A kind that cannot express every filter would otherwise contribute
        # rows that ignore the filter; leave it out entirely.
        if not set(filters).issubset(columns):
            continue
        conditions: list[str] = []
        params: list[Any] = []
        for field, raw in filters.items():
            if field in kind.fields:
                value = _coerce_field(
                    kind.fields[field],
                    raw,
                    ledger=ledger,
                    session=ledger.driver,
                    label=field,
                )
            else:
                value = raw
            conditions.append(f"{_quote(field)} = %s")
            params.append(value)
        sql = f"SELECT * FROM {_quote(f'v_{object_type}_{name}')}"
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += f" ORDER BY {_quote('id')}"
        for row in ledger.driver.query(sql, tuple(params)):
            record = dict(row)
            record["object"] = object_type
            records.append(record)
    return records


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------


def add_entity(
    ledger: Ledger,
    kind_name: str,
    *,
    values: Iterable[str] | Mapping[str, Any] | None = (),
    name: str | None = None,
    alias: str | Iterable[str] | None = None,
    aliases: Iterable[str] | None = None,
    id: str | None = None,
) -> dict[str, Any]:
    """Create an entity of ``kind_name`` and its kind-table row."""
    kind = _kind_for(ledger, "entity", kind_name)
    entity_id = id or ledger.next_id("entity")
    if id is not None:
        ledger.id_strategy("entity").require_valid(id)
    alias_list = _normalise_aliases(alias, aliases)

    with ledger.write(f"{kind_name} {entity_id}", verb="add", object="entity") as session:
        raw = _parse_pairs(values)
        if name is not None:
            if "name" in raw and raw["name"] != name:
                raise UsageError(
                    "the entity name was given twice (--name and --set name=...)"
                )
            raw["name"] = name
        core, fields = _split_values(
            session, ledger, kind, kind_name, "entity", raw
        )
        _mirror_name(kind, core, fields)
        _require_fields(kind, kind_name, fields)

        payload: dict[str, Any] = {"id": entity_id, "kind": kind_name}
        payload.update(core)
        _insert(session, "entity", payload)
        _insert(session, table_name("entity", kind_name), {"id": entity_id, **fields})
        for value in alias_list:
            _add_alias_row(session, "entity", entity_id, value)

    return {
        "object": "entity",
        "kind": kind_name,
        "id": entity_id,
        "name": core.get("name", fields.get("name")),
        "aliases": alias_list,
    }


def edit_entity(
    ledger: Ledger,
    ref: str,
    *,
    values: Iterable[str] | Mapping[str, Any] | None = (),
) -> dict[str, Any]:
    """Update core (``name``/``notes``) or kind fields of one entity."""
    raw = _parse_pairs(values)
    if not raw:
        raise UsageError("nothing to edit: pass at least one --set field=value")
    existing = _resolve_core(ledger.driver, "entity", ref)
    kind_name = str(existing["kind"])
    kind = ledger.schema.kind(kind_name)

    with ledger.write(
        f"{kind_name} {existing['id']}", verb="edit", object="entity"
    ) as session:
        core, fields = _split_values(
            session, ledger, kind, kind_name, "entity", raw
        )
        _mirror_name(kind, core, fields)
        if core:
            _update(session, "entity", {"id": existing["id"]}, core)
        if fields:
            _update(
                session,
                table_name("entity", kind_name),
                {"id": existing["id"]},
                fields,
            )
        record = _load_record(session, ledger, "entity", str(existing["id"]))
    # A dry run buffers the UPDATE without executing it; report the new values.
    record.update(core)
    record.update(fields)
    return record


def show_entity(ledger: Ledger, ref: str) -> dict[str, Any]:
    """One entity (core + kind fields), accepting an ID or an alias."""
    row = _resolve_core(ledger.driver, "entity", ref)
    return _load_record(ledger.driver, ledger, "entity", str(row["id"]))


def list_entities(
    ledger: Ledger,
    kind_name: str | None = None,
    *,
    where: Iterable[str] | Mapping[str, Any] | None = (),
) -> list[dict[str, Any]]:
    return _list_records(ledger, "entity", kind_name, where)


# ---------------------------------------------------------------------------
# Stocks
# ---------------------------------------------------------------------------


def _split_aliquot_values(
    session: Any,
    ledger: Ledger,
    kind: Kind,
    kind_name: str,
    values: Iterable[str] | Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Coerce ``aliquot_fields`` values supplied with a stock/aliquot add."""
    raw = _parse_pairs(values, option="--aliquot-set")
    unknown = sorted(set(raw) - set(kind.aliquot_fields))
    if unknown:
        choices = ", ".join(sorted(kind.aliquot_fields)) or "(none)"
        raise UsageError(
            f"unknown aliquot field(s) {', '.join(repr(name) for name in unknown)} "
            f"for stock kind {kind_name!r}; valid fields: {choices}"
        )
    data = {
        name: _coerce_field(
            kind.aliquot_fields[name],
            value,
            ledger=ledger,
            session=session,
            label=name,
        )
        for name, value in raw.items()
    }
    missing = [
        name
        for name, schema_field in kind.aliquot_fields.items()
        if schema_field.required
        and not schema_field.has_default
        and name not in data
    ]
    if missing:
        raise UsageError(
            f"missing required aliquot field(s) for kind {kind_name!r}: "
            + ", ".join(sorted(missing))
        )
    return data


def _insert_aliquot(
    session: Any,
    ledger: Ledger,
    *,
    stock_id: str,
    seq: int,
    box_row: Mapping[str, Any] | None,
    slot: str | None,
    quantity: Decimal | None,
    unit: str | None,
) -> str:
    alq_id = aliquot_id(stock_id, seq)
    placed_slot: str | None = None
    if box_row is not None:
        placed_slot = _check_aliquot_placement(session, ledger, box_row, slot)
    status = "stored" if box_row is not None else "pending"
    _insert(
        session,
        "aliquot",
        {
            "id": alq_id,
            "stock_id": stock_id,
            "seq": seq,
            "box_id": box_row["id"] if box_row is not None else None,
            "slot": placed_slot,
            "status": status,
            "quantity": quantity,
            "unit": unit,
        },
    )
    return alq_id


def _insert_aliquot_extension(
    session: Any,
    kind_name: str,
    alq_id: str,
    kind: Kind,
    values: Mapping[str, Any],
) -> None:
    if not kind.aliquot_fields:
        return
    _insert(
        session,
        table_name("aliquot", kind_name),
        {"id": alq_id, **values},
    )


def _place_slots(
    slots: Sequence[str | None], *, count: int, option: str = "--slot"
) -> list[str | None]:
    normalised = [None if item is None else str(item) for item in slots]
    if len(normalised) > count:
        raise UsageError(
            f"{option} was given {len(normalised)} time(s) for only {count} aliquot(s)"
        )
    return normalised + [None] * (count - len(normalised))


def add_stock(
    ledger: Ledger,
    kind_name: str,
    *,
    entity: str,
    values: Iterable[str] | Mapping[str, Any] | None = (),
    parent: str | None = None,
    source_aliquot: str | None = None,
    aliquots: int = 1,
    box: str | None = None,
    slots: Sequence[str | None] = (),
    quantity: Any = None,
    unit: Any = None,
    aliquot_values: Iterable[str] | Mapping[str, Any] | None = (),
    id: str | None = None,
) -> dict[str, Any]:
    """Create a stock, its kind row, and its aliquots (1 by default)."""
    kind = _kind_for(ledger, "stock", kind_name)
    count = _parse_count(aliquots, option="--aliquots")
    amount, unit_text = _parse_quantity_unit(quantity, unit)
    if count == 0 and (box is not None or list(slots)):
        raise UsageError("cannot place aliquots when --aliquots is 0")
    if box is None and list(slots):
        raise UsageError("--slot requires --box")
    slot_list = _place_slots(slots, count=count)

    stock_id = id or ledger.next_id("stock")
    if id is not None:
        ledger.id_strategy("stock").require_valid(id)

    with ledger.write(
        f"{kind_name} {stock_id} x{count}", verb="add", object="stock"
    ) as session:
        entity_row = _resolve_core(session, "entity", entity)
        if kind.entity_kinds is not None and entity_row["kind"] not in kind.entity_kinds:
            allowed = ", ".join(kind.entity_kinds)
            raise LedgerError(
                f"stock kind {kind_name!r} only accepts entity kinds [{allowed}], "
                f"not {entity_row['kind']!r}"
            )
        parent_row = (
            _resolve_core(session, "stock", parent) if parent is not None else None
        )
        source_row = (
            _resolve_core(session, "aliquot", source_aliquot)
            if source_aliquot is not None
            else None
        )
        raw = _parse_pairs(values)
        core, fields = _split_values(
            session, ledger, kind, kind_name, "stock", raw
        )
        _require_fields(kind, kind_name, fields)

        payload: dict[str, Any] = {
            "id": stock_id,
            "kind": kind_name,
            "entity_id": entity_row["id"],
            "parent_stock_id": parent_row["id"] if parent_row is not None else None,
            "source_aliquot_id": source_row["id"] if source_row is not None else None,
        }
        payload.update(core)
        _insert(session, "stock", payload)
        _insert(session, table_name("stock", kind_name), {"id": stock_id, **fields})

        box_row = _resolve_box(session, box) if box is not None else None
        if box_row is not None:
            geometry = _box_geometry(ledger.schema.kind(str(box_row["kind"])))
            if geometry.is_slotted and count > len(
                [slot for slot in slot_list if slot]
            ):
                raise LedgerError(
                    f"box {box_row['name']!r} has slots: give one --slot per aliquot"
                )
        alq_values = _split_aliquot_values(
            session, ledger, kind, kind_name, aliquot_values
        )

        created: list[str] = []
        for index in range(1, count + 1):
            alq_id = _insert_aliquot(
                session,
                ledger,
                stock_id=stock_id,
                seq=index,
                box_row=box_row,
                slot=slot_list[index - 1],
                quantity=amount,
                unit=unit_text,
            )
            _insert_aliquot_extension(
                session, kind_name, alq_id, kind, alq_values
            )
            record_event(
                session,
                aliquot_id=alq_id,
                event="create",
                amount=amount,
                unit=unit_text,
                to_box_id=box_row["id"] if box_row is not None else None,
                to_slot=slot_list[index - 1] if box_row is not None else None,
                new_status="stored" if box_row is not None else "pending",
                actor=ledger.actor,
            )
            created.append(alq_id)

    return {
        "object": "stock",
        "kind": kind_name,
        "id": stock_id,
        "entity_id": entity_row["id"],
        "parent_stock_id": parent_row["id"] if parent_row is not None else None,
        "source_aliquot_id": source_row["id"] if source_row is not None else None,
        "aliquots": created,
    }


def edit_stock(
    ledger: Ledger,
    ref: str,
    *,
    values: Iterable[str] | Mapping[str, Any] | None = (),
) -> dict[str, Any]:
    raw = _parse_pairs(values)
    if not raw:
        raise UsageError("nothing to edit: pass at least one --set field=value")
    existing = _resolve_core(ledger.driver, "stock", ref)
    kind_name = str(existing["kind"])
    kind = ledger.schema.kind(kind_name)

    with ledger.write(
        f"{kind_name} {existing['id']}", verb="edit", object="stock"
    ) as session:
        core, fields = _split_values(
            session, ledger, kind, kind_name, "stock", raw
        )
        if core:
            _update(session, "stock", {"id": existing["id"]}, core)
        if fields:
            _update(
                session,
                table_name("stock", kind_name),
                {"id": existing["id"]},
                fields,
            )
        record = _load_record(session, ledger, "stock", str(existing["id"]))
    record.update(core)
    record.update(fields)
    return record


def show_stock(ledger: Ledger, ref: str) -> dict[str, Any]:
    row = _resolve_core(ledger.driver, "stock", ref)
    record = _load_record(ledger.driver, ledger, "stock", str(row["id"]))
    record["aliquots"] = ledger.driver.query(
        "SELECT `id`, `seq`, `status`, `quantity`, `unit`, `box_id`, `slot` "
        "FROM `aliquot` WHERE `stock_id` = %s ORDER BY `seq`",
        (row["id"],),
    )
    return record


def list_stocks(
    ledger: Ledger,
    kind_name: str | None = None,
    *,
    where: Iterable[str] | Mapping[str, Any] | None = (),
) -> list[dict[str, Any]]:
    return _list_records(ledger, "stock", kind_name, where)


# ---------------------------------------------------------------------------
# Aliquots
# ---------------------------------------------------------------------------


def add_aliquots(
    ledger: Ledger,
    stock_ref: str,
    *,
    count: int = 1,
    box: str | None = None,
    slots: Sequence[str | None] = (),
    quantity: Any = None,
    unit: Any = None,
    values: Iterable[str] | Mapping[str, Any] | None = (),
) -> dict[str, Any]:
    """Add ``count`` aliquots to an existing stock."""
    amount, unit_text = _parse_quantity_unit(quantity, unit)
    count = _parse_count(count, option="--count")
    if count < 1:
        raise UsageError("--count must be at least 1")
    if box is None and list(slots):
        raise UsageError("--slot requires --box")
    slot_list = _place_slots(slots, count=count)

    existing = _resolve_core(ledger.driver, "stock", stock_ref)
    stock_id = str(existing["id"])
    kind_name = str(existing["kind"])
    kind = ledger.schema.kind(kind_name)
    used_ids = [
        str(row["id"])
        for row in ledger.driver.query(
            "SELECT `id` FROM `aliquot` WHERE `stock_id` = %s", (stock_id,)
        )
    ]
    first_seq = parse_aliquot_id(next_aliquot_id(stock_id, used_ids))[1]

    with ledger.write(
        f"{stock_id} x{count}", verb="add", object="aliquot"
    ) as session:
        _resolve_core(session, "stock", stock_id)
        box_row = _resolve_box(session, box) if box is not None else None
        if box_row is not None:
            geometry = _box_geometry(ledger.schema.kind(str(box_row["kind"])))
            if geometry.is_slotted and count > len([slot for slot in slot_list if slot]):
                raise LedgerError(
                    f"box {box_row['name']!r} has slots: give one --slot per aliquot"
                )
        alq_values = _split_aliquot_values(
            session, ledger, kind, kind_name, values
        )
        created: list[str] = []
        for offset in range(count):
            alq_id = _insert_aliquot(
                session,
                ledger,
                stock_id=stock_id,
                seq=first_seq + offset,
                box_row=box_row,
                slot=slot_list[offset],
                quantity=amount,
                unit=unit_text,
            )
            _insert_aliquot_extension(
                session, kind_name, alq_id, kind, alq_values
            )
            record_event(
                session,
                aliquot_id=alq_id,
                event="create",
                amount=amount,
                unit=unit_text,
                to_box_id=box_row["id"] if box_row is not None else None,
                to_slot=slot_list[offset] if box_row is not None else None,
                new_status="stored" if box_row is not None else "pending",
                actor=ledger.actor,
            )
            created.append(alq_id)

    return {"object": "aliquot", "stock_id": stock_id, "ids": created}


def move_aliquot(
    ledger: Ledger,
    ref: str,
    *,
    box: str,
    slot: str | None = None,
) -> dict[str, Any]:
    """Move an aliquot to a box/slot, recording a ``move`` event."""
    existing = _resolve_core(ledger.driver, "aliquot", ref)
    alq_id = str(existing["id"])

    with ledger.write(alq_id, verb="move", object="aliquot") as session:
        row = _resolve_core(session, "aliquot", alq_id)
        box_row = _resolve_box(session, box)
        placed_slot = _check_aliquot_placement(
            session, ledger, box_row, slot, exclude_aliquot=alq_id
        )
        _update(
            session,
            "aliquot",
            {"id": alq_id},
            {"box_id": box_row["id"], "slot": placed_slot},
        )
        record_event(
            session,
            aliquot_id=alq_id,
            event="move",
            from_box_id=row.get("box_id"),
            from_slot=row.get("slot"),
            to_box_id=box_row["id"],
            to_slot=placed_slot,
            old_status=row.get("status"),
            new_status=row.get("status"),
            actor=ledger.actor,
        )
        record = _load_record(session, ledger, "aliquot", alq_id)
    # A dry run buffers the UPDATE; report the destination it would write.
    record["box_id"] = box_row["id"]
    record["slot"] = placed_slot
    return record


def use_aliquot(
    ledger: Ledger,
    ref: str,
    amount: Any,
    unit: str,
    *,
    purpose: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Consume ``amount`` of an aliquot; reaching zero marks it ``consumed``."""
    try:
        used = Decimal(str(amount).strip())
    except (InvalidOperation, ValueError):
        raise UsageError(f"amount must be a decimal number (got {amount!r})") from None
    if not used.is_finite() or used <= 0:
        raise UsageError(f"amount must be a positive number (got {amount!r})")
    unit_text = str(unit).strip()
    if not unit_text:
        raise UsageError("unit must not be empty")

    existing = _resolve_core(ledger.driver, "aliquot", ref)
    alq_id = str(existing["id"])

    with ledger.write(alq_id, verb="use", object="aliquot") as session:
        row = _resolve_core(session, "aliquot", alq_id)
        old_status = str(row.get("status"))
        if old_status == "consumed":
            raise LedgerError(f"aliquot {alq_id!r} is already consumed")
        if row.get("quantity") is None:
            raise LedgerError(f"aliquot {alq_id!r} has no quantity to use")
        current = Decimal(str(row["quantity"]))
        if row.get("unit") is None or str(row["unit"]) != unit_text:
            raise LedgerError(
                f"unit mismatch for aliquot {alq_id!r}: it holds "
                f"{row.get('unit')!r}, not {unit_text!r}"
            )
        remaining = current - used
        if remaining < 0:
            raise LedgerError(
                f"cannot use {used} {unit_text} from aliquot {alq_id!r}: "
                f"only {current} left"
            )
        new_status = "consumed" if remaining == 0 else old_status
        _update(
            session,
            "aliquot",
            {"id": alq_id},
            {"quantity": remaining, "status": new_status},
        )
        record_event(
            session,
            aliquot_id=alq_id,
            event="use",
            amount=used,
            unit=unit_text,
            old_status=old_status,
            new_status=new_status,
            actor=ledger.actor,
            purpose=purpose,
            note=note,
        )
        return {
            "object": "aliquot",
            "id": alq_id,
            "quantity": str(remaining),
            "unit": unit_text,
            "status": new_status,
        }


def set_aliquot_status(
    ledger: Ledger,
    ref: str,
    status: str,
    *,
    note: str | None = None,
) -> dict[str, Any]:
    """Change an aliquot's status, recording a ``status`` event."""
    new_status = str(status).strip()
    if new_status not in ledger.schema.statuses:
        raise UsageError(
            f"unknown status {status!r}; choose from: "
            + ", ".join(ledger.schema.statuses)
        )
    existing = _resolve_core(ledger.driver, "aliquot", ref)
    alq_id = str(existing["id"])

    with ledger.write(alq_id, verb="status", object="aliquot") as session:
        row = _resolve_core(session, "aliquot", alq_id)
        old_status = str(row.get("status"))
        if new_status == "stored" and row.get("box_id") is not None:
            box_row = _fetch_core(session, "box", str(row["box_id"]))
            if box_row is not None:
                geometry = _box_geometry(ledger.schema.kind(str(box_row["kind"])))
                if geometry.is_slotted and not row.get("slot"):
                    raise LedgerError(
                        f"aliquot {alq_id!r} is in slotted box "
                        f"{box_row['name']!r} but has no slot; move it to a slot first"
                    )
        _update(session, "aliquot", {"id": alq_id}, {"status": new_status})
        record_event(
            session,
            aliquot_id=alq_id,
            event="status",
            old_status=old_status,
            new_status=new_status,
            actor=ledger.actor,
            note=note,
        )
        return {
            "object": "aliquot",
            "id": alq_id,
            "old_status": old_status,
            "status": new_status,
        }


def purge_aliquot(ledger: Ledger, ref: str) -> dict[str, Any]:
    """Hard-delete an aliquot, its events and its kind extension row."""
    existing = _resolve_core(ledger.driver, "aliquot", ref)
    alq_id = str(existing["id"])
    stock = _resolve_core(ledger.driver, "stock", str(existing["stock_id"]))
    kind = ledger.schema.kind(str(stock["kind"]))

    with ledger.write(alq_id, verb="purge", object="aliquot") as session:
        _resolve_core(session, "aliquot", alq_id)
        if kind.aliquot_fields:
            session.execute(
                f"DELETE FROM {_quote(table_name('aliquot', str(stock['kind'])))} "
                f"WHERE {_quote('id')} = %s",
                (alq_id,),
            )
        events = purge_events(session, alq_id)
        session.execute(
            f"DELETE FROM {_quote('aliquot')} WHERE {_quote('id')} = %s", (alq_id,)
        )
    return {"object": "aliquot", "id": alq_id, "purged": True, "events": events}


# ---------------------------------------------------------------------------
# Boxes
# ---------------------------------------------------------------------------


def add_box(
    ledger: Ledger,
    kind_name: str,
    name: str,
    *,
    parent: str | None = None,
    slot: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Create a box and materialise its ``slot`` rows from the geometry."""
    kind = _kind_for(ledger, "box", kind_name)
    box_name = str(name).strip()
    if not box_name:
        raise UsageError("a box needs a non-empty name")
    if parent is None and slot is not None:
        raise UsageError("--slot requires --in PARENT")
    box_id = str(uuid.uuid4())
    geometry = _box_geometry(kind)

    with ledger.write(f"{kind_name} {box_id}", verb="add", object="box") as session:
        duplicate = session.query(
            f"SELECT {_quote('id')} FROM {_quote('box')} WHERE {_quote('name')} = %s",
            (box_name,),
        )
        if duplicate:
            raise LedgerError(f"box name {box_name!r} is already in use")
        parent_row = _resolve_box(session, parent) if parent is not None else None
        parent_slot: str | None = None
        if parent_row is not None:
            parent_slot = _check_child_placement(session, ledger, parent_row, slot)

        payload: dict[str, Any] = {
            "id": box_id,
            "name": box_name,
            "kind": kind_name,
            "parent_box_id": parent_row["id"] if parent_row is not None else None,
            "parent_slot": parent_slot,
        }
        if notes is not None:
            payload["notes"] = notes
        _insert(session, "box", payload)
        _insert(session, table_name("box", kind_name), {"id": box_id})
        rows = slot_rows(box_id, geometry)
        for row in rows:
            _insert(session, "slot", row)

    return {
        "object": "box",
        "kind": kind_name,
        "id": box_id,
        "name": box_name,
        "parent_box_id": parent_row["id"] if parent_row is not None else None,
        "parent_slot": parent_slot,
        "slots": len(rows),
    }


def move_box(
    ledger: Ledger,
    ref: str,
    *,
    parent: str,
    slot: str | None = None,
) -> dict[str, Any]:
    """Move an existing box inside ``parent`` (acyclic, exclusive slot)."""
    existing = _resolve_box(ledger.driver, ref)
    box_id = str(existing["id"])

    with ledger.write(box_id, verb="move", object="box") as session:
        row = _fetch_core(session, "box", box_id)
        if row is None:  # pragma: no cover - just resolved
            raise LedgerError(f"unknown box {ref!r}")
        parent_row = _resolve_box(session, parent)
        _check_acyclic(session, box_id, parent_row)
        parent_slot = _check_child_placement(
            session, ledger, parent_row, slot, exclude_box=box_id
        )
        _update(
            session,
            "box",
            {"id": box_id},
            {"parent_box_id": parent_row["id"], "parent_slot": parent_slot},
        )
        record = _load_record(session, ledger, "box", box_id)

    record["parent_box_id"] = parent_row["id"]
    record["parent_slot"] = parent_slot
    return record


def list_boxes(
    ledger: Ledger,
    kind_name: str | None = None,
    *,
    where: Iterable[str] | Mapping[str, Any] | None = (),
) -> list[dict[str, Any]]:
    records = _list_records(ledger, "box", kind_name, where)
    all_rows = ledger.driver.query(
        "SELECT `id`, `name`, `parent_box_id` FROM `box`"
    )
    paths = _box_paths(all_rows)
    for record in records:
        record["path"] = paths.get(str(record["id"]), str(record.get("name")))
    return records


# ---------------------------------------------------------------------------
# Aliases
# ---------------------------------------------------------------------------


def add_alias(
    ledger: Ledger, object_type: str, object_id: str, alias: str
) -> dict[str, Any]:
    """Attach an alias to any core object (unique per object type)."""
    checked_type = _check_object_type(object_type)
    text = str(alias).strip()
    if not text:
        raise UsageError("an alias must not be empty")
    resolved = _resolve_core(ledger.driver, checked_type, str(object_id))
    canonical_id = str(resolved["id"])

    with ledger.write(
        f"{checked_type} {canonical_id}", verb="add", object="alias"
    ) as session:
        _resolve_core(session, checked_type, canonical_id)
        _add_alias_row(session, checked_type, canonical_id, text)
    return {
        "object": "alias",
        "object_type": checked_type,
        "object_id": canonical_id,
        "alias": text,
    }


def remove_alias(
    ledger: Ledger, object_type: str | None, alias: str
) -> dict[str, Any]:
    """Remove one alias, or every alias with that text when no type is given."""
    checked_type = _check_object_type(object_type) if object_type is not None else None
    text = str(alias).strip()
    if not text:
        raise UsageError("an alias must not be empty")

    with ledger.write(
        checked_type or "any", verb="rm", object="alias"
    ) as session:
        if checked_type is not None:
            rows = session.query(
                f"SELECT {_quote('object_id')} FROM {_quote('alias')} "
                f"WHERE {_quote('object_type')} = %s AND {_quote('alias')} = %s",
                (checked_type, text),
            )
            objects = [
                {"object_type": checked_type, "object_id": str(row["object_id"])}
                for row in rows
            ]
        else:
            rows = session.query(
                f"SELECT {_quote('object_type')}, {_quote('object_id')} "
                f"FROM {_quote('alias')} WHERE {_quote('alias')} = %s",
                (text,),
            )
            objects = [
                {
                    "object_type": str(row["object_type"]),
                    "object_id": str(row["object_id"]),
                }
                for row in rows
            ]
        if not objects:
            raise LedgerError(
                f"{checked_type} has no alias {text!r}"
                if checked_type is not None
                else f"no alias {text!r} found"
            )
        if checked_type is not None:
            session.execute(
                f"DELETE FROM {_quote('alias')} "
                f"WHERE {_quote('object_type')} = %s AND {_quote('alias')} = %s",
                (checked_type, text),
            )
        else:
            session.execute(
                f"DELETE FROM {_quote('alias')} WHERE {_quote('alias')} = %s",
                (text,),
            )
    result: dict[str, Any] = {
        "object": "alias",
        "alias": text,
        "removed": True,
        "objects": objects,
    }
    if len(objects) == 1:
        result["object_type"] = objects[0]["object_type"]
        result["object_id"] = objects[0]["object_id"]
    return result


# ---------------------------------------------------------------------------
# Renumbering stocks (TM-06)
# ---------------------------------------------------------------------------

#: Separators accepted between the old and the new id in a ``--map`` file.
_RENAME_SEPARATORS: tuple[str, ...] = ("\t", ",")

#: ``id`` columns are ``VARCHAR(64)`` in the generated DDL; aliquot ids append
#: ``.<seq>``, so a generated id also has to leave room for that suffix.
_ID_MAX_LENGTH = 64


def read_rename_map(
    path: str | Path, *, auto: bool = False
) -> list[tuple[str, str | None]]:
    """Parse a ``--map`` file into ``(old, new)`` pairs.

    One rename per line: ``OLD<TAB>NEW`` or ``OLD,NEW``; with ``auto`` a line
    may contain only ``OLD`` and the new id is generated.  Blank lines and
    lines whose first non-space character is ``#`` are ignored.  Every problem
    is reported with the file name and line number.
    """
    file_path = Path(path).expanduser()
    try:
        text = file_path.read_text(encoding="utf-8")
    except OSError as error:
        raise UsageError(f"cannot read the mapping file {file_path}: {error}") from error

    renames: list[tuple[str, str | None]] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        separator = next((sep for sep in _RENAME_SEPARATORS if sep in line), None)
        if separator is None:
            old, new = line, None
        else:
            parts = [part.strip() for part in line.split(separator)]
            if len(parts) != 2:
                raise UsageError(
                    f"{file_path}: line {number}: expected one rename "
                    f"'OLD{separator}NEW' (got {line!r})"
                )
            old, new = parts
        if not old:
            raise UsageError(f"{file_path}: line {number}: the old id is empty")
        if new is not None and not new:
            raise UsageError(f"{file_path}: line {number}: the new id is empty")
        if new is None and not auto:
            raise UsageError(
                f"{file_path}: line {number}: missing the new id; "
                "write 'OLD<TAB>NEW' or pass --auto"
            )
        renames.append((old, new))
    if not renames:
        raise UsageError(f"the mapping file {file_path} contains no renames")
    return renames


def _resolve_stock_target(ledger: Ledger, ref: str) -> dict[str, Any]:
    """Resolve one REF to a stock row, with a precise error for other objects."""
    try:
        return _resolve_core(ledger.driver, "stock", ref)
    except LedgerError as error:
        # Imported late: ``ops.find`` imports this module.
        from tetramark.ops.find import resolve_any

        try:
            other = resolve_any(ledger, ref)
        except LedgerError:
            raise error from None
        raise UsageError(
            f"{ref!r} is a {other['object']} {other['id']}, not a stock"
        ) from None


def _alias_owners(ledger: Ledger, object_type: str) -> dict[str, str]:
    """Alias text → object id for one object type (the alias namespace)."""
    rows = ledger.driver.query(
        f"SELECT {_quote('alias')}, {_quote('object_id')} FROM {_quote('alias')} "
        f"WHERE {_quote('object_type')} = %s",
        (object_type,),
    )
    return {str(row["alias"]): str(row["object_id"]) for row in rows}


def _allocate_stock_ids(
    ledger: Ledger, count: int, *, reserved: Iterable[str] = ()
) -> list[str]:
    """Allocate ``count`` fresh strategy-shaped stock ids (one per rename).

    The strategy is asked for ids as if the already-chosen candidates existed,
    and ids taken by an existing stock *alias* are skipped too: ids and aliases
    share one namespace, so ``--auto`` must never produce a shadowing id.
    """
    strategy = ledger.id_strategy("stock")
    taken = {
        str(row["id"]) for row in ledger.driver.query("SELECT `id` FROM `stock`")
    }
    taken.update(_alias_owners(ledger, "stock"))
    taken.update(str(value) for value in reserved)
    today = _dt.date.today()

    def existing(prefix: str) -> list[str]:
        return sorted(value for value in taken if value.startswith(prefix))

    chosen: list[str] = []
    for _ in range(count):
        candidate = strategy.next_id(existing, today)
        while candidate in taken:
            # A taken value (typically an alias) must push the strategy on.
            taken.add(candidate)
            candidate = strategy.next_id(existing, today)
        taken.add(candidate)
        chosen.append(candidate)
    return chosen


def _ref_target_object(ledger: Ledger, field: Field) -> str | None:
    """The core object type a ``ref`` field points at (``None`` for non-refs)."""
    if field.type is not FieldType.ref or field.to is None:
        return None
    target = str(field.to)
    if target in OBJECT_TYPES:
        return target
    kind = ledger.schema.kinds.get(target)
    return None if kind is None else kind.object.value


def _renumber_ref_columns(
    ledger: Ledger,
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Kind ``ref`` columns that store a stock id or an aliquot id.

    The core tables are handled separately; this catches user-defined
    ``type: ref`` fields (including fields on aliquot extension tables), whose
    foreign keys also point at the renamed primary keys.
    """
    stock_columns: list[tuple[str, str]] = []
    aliquot_columns: list[tuple[str, str]] = []
    for kind_name in sorted(ledger.schema.kinds):
        kind = ledger.schema.kinds[kind_name]
        scopes: list[tuple[str, Mapping[str, Field]]] = [
            (table_name(kind.object.value, kind_name), kind.fields)
        ]
        if kind.object.value == "stock" and kind.aliquot_fields:
            scopes.append((table_name("aliquot", kind_name), kind.aliquot_fields))
        for table, fields in scopes:
            for field_name in sorted(fields):
                target = _ref_target_object(ledger, fields[field_name])
                if target == "stock":
                    stock_columns.append((table, field_name))
                elif target == "aliquot":
                    aliquot_columns.append((table, field_name))
    return stock_columns, aliquot_columns


def _emit_renumber_statements(
    ledger: Ledger,
    session: Any,
    plans: Sequence[Mapping[str, Any]],
    *,
    stock_ref_columns: Sequence[tuple[str, str]],
    aliquot_ref_columns: Sequence[tuple[str, str]],
    skip_stock_aliases: set[str],
    skip_aliquot_aliases: set[str],
) -> None:
    """Queue every reference update of one renumber batch in one session."""
    for plan in plans:
        old = str(plan["old"])
        new = str(plan["new"])
        kind_name = str(plan["kind"])
        kind = ledger.schema.kind(kind_name)

        session.execute("UPDATE `stock` SET `id` = %s WHERE `id` = %s", (new, old))
        session.execute(
            f"UPDATE {_quote(table_name('stock', kind_name))} "
            "SET `id` = %s WHERE `id` = %s",
            (new, old),
        )

        for aliquot in plan["aliquots"]:
            old_alq = str(aliquot["old"])
            new_alq = str(aliquot["new"])
            session.execute(
                "UPDATE `aliquot` SET `id` = %s, `stock_id` = %s WHERE `id` = %s",
                (new_alq, new, old_alq),
            )
            if kind.aliquot_fields:
                session.execute(
                    f"UPDATE {_quote(table_name('aliquot', kind_name))} "
                    "SET `id` = %s WHERE `id` = %s",
                    (new_alq, old_alq),
                )
            session.execute(
                "UPDATE `aliquot_event` SET `aliquot_id` = %s "
                "WHERE `aliquot_id` = %s",
                (new_alq, old_alq),
            )
            session.execute(
                "UPDATE `stock` SET `source_aliquot_id` = %s "
                "WHERE `source_aliquot_id` = %s",
                (new_alq, old_alq),
            )
            for table, column in aliquot_ref_columns:
                session.execute(
                    f"UPDATE {_quote(table)} SET {_quote(column)} = %s "
                    f"WHERE {_quote(column)} = %s",
                    (new_alq, old_alq),
                )

        session.execute(
            "UPDATE `stock` SET `parent_stock_id` = %s "
            "WHERE `parent_stock_id` = %s",
            (new, old),
        )
        for table, column in stock_ref_columns:
            session.execute(
                f"UPDATE {_quote(table)} SET {_quote(column)} = %s "
                f"WHERE {_quote(column)} = %s",
                (new, old),
            )
        session.execute(
            "UPDATE `alias` SET `object_id` = %s "
            "WHERE `object_type` = 'stock' AND `object_id` = %s",
            (new, old),
        )
        for aliquot in plan["aliquots"]:
            session.execute(
                "UPDATE `alias` SET `object_id` = %s "
                "WHERE `object_type` = 'aliquot' AND `object_id` = %s",
                (str(aliquot["new"]), str(aliquot["old"])),
            )

        # Keep the old handles findable.  An existing alias that already names
        # this very object was rewritten above, so inserting it again would
        # only violate the alias primary key (idempotent case).
        if old not in skip_stock_aliases:
            session.execute(
                "INSERT INTO `alias` (`object_type`, `object_id`, `alias`) "
                "VALUES ('stock', %s, %s)",
                (new, old),
            )
        for aliquot in plan["aliquots"]:
            old_alq = str(aliquot["old"])
            if old_alq not in skip_aliquot_aliases:
                session.execute(
                    "INSERT INTO `alias` (`object_type`, `object_id`, `alias`) "
                    "VALUES ('aliquot', %s, %s)",
                    (str(aliquot["new"]), old_alq),
                )


def renumber_stocks(
    ledger: Ledger,
    refs: Sequence[str] = (),
    *,
    new_id: str | None = None,
    auto: bool = False,
    map_path: str | Path | None = None,
) -> dict[str, Any]:
    """Renumber stocks (and their aliquots) to strategy-shaped ids.

    Exactly one of ``new_id`` (one REF), ``auto`` (generated ids) or
    ``map_path`` (a batch file) supplies the new ids; ``auto`` may be combined
    with ``map_path`` to generate the id of lines that only give the old one.
    The old stock and aliquot ids are kept as aliases, every referencing column
    is rewritten, and the whole batch is one transaction and one Dolt commit.
    """
    selected = [str(ref).strip() for ref in refs]
    if any(not ref for ref in selected):
        raise UsageError("a stock REF must not be empty")
    if new_id is not None and (auto or map_path is not None):
        raise UsageError("--id cannot be combined with --auto or --map")
    if new_id is None and not auto and map_path is None:
        raise UsageError("nothing to renumber: pass --id NEW, --auto or --map PATH")
    if new_id is not None and len(selected) != 1:
        raise UsageError(f"--id takes exactly one REF (got {len(selected)})")
    if map_path is not None and selected:
        raise UsageError("REF and --map cannot be combined")
    if map_path is None and not selected:
        raise UsageError("nothing to renumber: give at least one REF or --map")

    if map_path is not None:
        requested = read_rename_map(map_path, auto=auto)
    elif new_id is not None:
        requested = [(selected[0], str(new_id))]
    else:
        requested = [(ref, None) for ref in selected]

    # 1. Resolve every REF to an existing stock (id first, then alias).
    plans: list[dict[str, Any]] = []
    seen: set[str] = set()
    for ref, wanted in requested:
        row = _resolve_stock_target(ledger, ref)
        old = str(row["id"])
        if old in seen:
            raise UsageError(f"stock {old!r} appears more than once in the rename list")
        seen.add(old)
        plans.append(
            {"old": old, "new": wanted, "kind": str(row["kind"]), "aliquots": []}
        )

    # 2. Generate the ids the caller left open (--auto).
    missing = [plan for plan in plans if plan["new"] is None]
    if missing:
        reserved = {str(plan["new"]) for plan in plans if plan["new"] is not None}
        for plan, candidate in zip(
            missing, _allocate_stock_ids(ledger, len(missing), reserved=reserved)
        ):
            plan["new"] = candidate

    # 3. Validate every new id: shape, uniqueness, and the alias namespace.
    strategy = ledger.id_strategy("stock")
    existing_ids = {
        str(row["id"]) for row in ledger.driver.query("SELECT `id` FROM `stock`")
    }
    stock_aliases = _alias_owners(ledger, "stock")
    new_ids: dict[str, str] = {}
    for plan in plans:
        old = str(plan["old"])
        new = str(plan["new"])
        try:
            strategy.require_valid(new)
        except IdError as error:
            raise IdError(f"{error} (new id for stock {old!r})") from None
        if len(new) > _ID_MAX_LENGTH:
            raise LedgerError(
                f"stock id {new!r} is longer than {_ID_MAX_LENGTH} characters"
            )
        if new == old:
            raise UsageError(
                f"the new id for stock {old!r} must differ from the old id"
            )
        if new in new_ids:
            raise UsageError(
                f"the new id {new!r} is used for both {new_ids[new]!r} and {old!r}"
            )
        new_ids[new] = old
        if new in existing_ids:
            raise LedgerError(f"stock id {new!r} already exists")
        if new in stock_aliases:
            raise LedgerError(
                f"{new!r} is already an alias of stock {stock_aliases[new]!r}; "
                "stock ids and stock aliases share one namespace"
            )

    # 4. Collect the aliquots of every stock and the ids they would get.
    candidates: list[str] = []
    for plan in plans:
        rows = ledger.driver.query(
            "SELECT `id`, `seq` FROM `aliquot` WHERE `stock_id` = %s ORDER BY `seq`",
            (plan["old"],),
        )
        aliquots: list[dict[str, Any]] = []
        for row in rows:
            seq = int(row["seq"])
            old_alq = str(row["id"])
            new_alq = aliquot_id(str(plan["new"]), seq)
            if len(new_alq) > _ID_MAX_LENGTH:
                raise LedgerError(
                    f"aliquot id {new_alq!r} is longer than {_ID_MAX_LENGTH} characters"
                )
            aliquots.append({"old": old_alq, "new": new_alq, "seq": seq})
            candidates.append(new_alq)
        plan["aliquots"] = aliquots
    if candidates:
        placeholders = ", ".join("%s" for _ in candidates)
        clashes = ledger.driver.query(
            f"SELECT `id` FROM `aliquot` WHERE `id` IN ({placeholders})",
            tuple(candidates),
        )
        if clashes:
            raise LedgerError(
                "aliquot id(s) already exist: "
                + ", ".join(sorted(str(row["id"]) for row in clashes))
            )

    # 5. The old ids must be free as alias texts (they normally are: they are
    #    the primary keys we are about to vacate).
    skip_stock_aliases: set[str] = set()
    for plan in plans:
        old = str(plan["old"])
        owner = stock_aliases.get(old)
        if owner is None:
            continue
        if owner == old:
            skip_stock_aliases.add(old)
            continue
        raise LedgerError(
            f"cannot keep {old!r} as an alias: it is already an alias of "
            f"stock {owner!r}"
        )
    aliquot_aliases = _alias_owners(ledger, "aliquot")
    skip_aliquot_aliases: set[str] = set()
    for plan in plans:
        for aliquot in plan["aliquots"]:
            old_alq = str(aliquot["old"])
            owner = aliquot_aliases.get(old_alq)
            if owner is None:
                continue
            if owner == old_alq:
                skip_aliquot_aliases.add(old_alq)
                continue
            raise LedgerError(
                f"cannot keep {old_alq!r} as an alias: it is already an alias "
                f"of aliquot {owner!r}"
            )

    # 6. One transaction, one commit.  Foreign keys are NO ACTION, so a
    #    primary-key rename needs the checks off; Dolt applies the setting per
    #    session, and both drivers run the whole batch in one session.  The
    #    reset is queued in a ``finally`` so a failing statement cannot leave a
    #    session with the checks disabled.
    stock_ref_columns, aliquot_ref_columns = _renumber_ref_columns(ledger)
    count = len(plans)
    summary = (
        f"{plans[0]['old']} -> {plans[0]['new']}"
        if count == 1
        else f"{count} stock(s)"
    )
    try:
        summary.encode("ascii")
    except UnicodeEncodeError:
        # Dolt may truncate a non-ASCII commit message mid-byte (2.3.x bug);
        # fall back to the batch message instead of leaving writes uncommitted.
        summary = f"{count} stock(s)"
    with ledger.write(summary, verb="renumber", object="stock") as session:
        session.execute("SET FOREIGN_KEY_CHECKS = 0")
        try:
            _emit_renumber_statements(
                ledger,
                session,
                plans,
                stock_ref_columns=stock_ref_columns,
                aliquot_ref_columns=aliquot_ref_columns,
                skip_stock_aliases=skip_stock_aliases,
                skip_aliquot_aliases=skip_aliquot_aliases,
            )
        finally:
            session.execute("SET FOREIGN_KEY_CHECKS = 1")

    mapping: dict[str, str] = {}
    renames: list[dict[str, Any]] = []
    for plan in plans:
        mapping[str(plan["old"])] = str(plan["new"])
        aliquots = []
        for aliquot in plan["aliquots"]:
            mapping[str(aliquot["old"])] = str(aliquot["new"])
            aliquots.append({"old": aliquot["old"], "new": aliquot["new"]})
        renames.append(
            {
                "old": plan["old"],
                "new": plan["new"],
                "kind": plan["kind"],
                "aliquots": aliquots,
            }
        )
    return {
        "object": "stock",
        "renames": renames,
        "mapping": mapping,
        "commit": session.commit_hash,
    }


# ---------------------------------------------------------------------------
# Facades attached to Ledger
# ---------------------------------------------------------------------------


class EntityOps:
    """``ledger.entities`` — entity commands (spec §6)."""

    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger

    def add(
        self,
        kind: str,
        *,
        values: Iterable[str] | Mapping[str, Any] | None = (),
        name: str | None = None,
        alias: str | Iterable[str] | None = None,
        aliases: Iterable[str] | None = None,
        id: str | None = None,
    ) -> dict[str, Any]:
        return add_entity(
            self.ledger,
            kind,
            values=values,
            name=name,
            alias=alias,
            aliases=aliases,
            id=id,
        )

    def edit(
        self, ref: str, *, values: Iterable[str] | Mapping[str, Any] | None = ()
    ) -> dict[str, Any]:
        return edit_entity(self.ledger, ref, values=values)

    def show(self, ref: str) -> dict[str, Any]:
        return show_entity(self.ledger, ref)

    def ls(
        self,
        kind: str | None = None,
        *,
        where: Iterable[str] | Mapping[str, Any] | None = (),
    ) -> list[dict[str, Any]]:
        return list_entities(self.ledger, kind, where=where)


class StockOps:
    """``ledger.stocks`` — stock commands (spec §6)."""

    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger

    def add(
        self,
        kind: str,
        *,
        entity: str,
        values: Iterable[str] | Mapping[str, Any] | None = (),
        parent: str | None = None,
        source_aliquot: str | None = None,
        aliquots: int = 1,
        box: str | None = None,
        slots: Sequence[str | None] = (),
        quantity: Any = None,
        unit: Any = None,
        aliquot_values: Iterable[str] | Mapping[str, Any] | None = (),
        id: str | None = None,
    ) -> dict[str, Any]:
        return add_stock(
            self.ledger,
            kind,
            entity=entity,
            values=values,
            parent=parent,
            source_aliquot=source_aliquot,
            aliquots=aliquots,
            box=box,
            slots=slots,
            quantity=quantity,
            unit=unit,
            aliquot_values=aliquot_values,
            id=id,
        )

    def edit(
        self, ref: str, *, values: Iterable[str] | Mapping[str, Any] | None = ()
    ) -> dict[str, Any]:
        return edit_stock(self.ledger, ref, values=values)

    def show(self, ref: str) -> dict[str, Any]:
        return show_stock(self.ledger, ref)

    def renumber(
        self,
        refs: Sequence[str] = (),
        *,
        new_id: str | None = None,
        auto: bool = False,
        map_path: str | Path | None = None,
    ) -> dict[str, Any]:
        return renumber_stocks(
            self.ledger, refs, new_id=new_id, auto=auto, map_path=map_path
        )

    def ls(
        self,
        kind: str | None = None,
        *,
        where: Iterable[str] | Mapping[str, Any] | None = (),
    ) -> list[dict[str, Any]]:
        return list_stocks(self.ledger, kind, where=where)


class AliquotOps:
    """``ledger.aliquots`` — aliquot commands (spec §6)."""

    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger

    def add(
        self,
        stock: str,
        *,
        count: int = 1,
        box: str | None = None,
        slots: Sequence[str | None] = (),
        quantity: Any = None,
        unit: Any = None,
        values: Iterable[str] | Mapping[str, Any] | None = (),
    ) -> dict[str, Any]:
        return add_aliquots(
            self.ledger,
            stock,
            count=count,
            box=box,
            slots=slots,
            quantity=quantity,
            unit=unit,
            values=values,
        )

    def move(self, ref: str, *, box: str, slot: str | None = None) -> dict[str, Any]:
        return move_aliquot(self.ledger, ref, box=box, slot=slot)

    def use(
        self,
        ref: str,
        amount: Any,
        unit: str,
        *,
        purpose: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        return use_aliquot(
            self.ledger, ref, amount, unit, purpose=purpose, note=note
        )

    def status(
        self, ref: str, status: str, *, note: str | None = None
    ) -> dict[str, Any]:
        return set_aliquot_status(self.ledger, ref, status, note=note)

    def purge(self, ref: str) -> dict[str, Any]:
        return purge_aliquot(self.ledger, ref)


class BoxOps:
    """``ledger.boxes`` — box commands (spec §6)."""

    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger

    def add(
        self,
        kind: str,
        name: str,
        *,
        parent: str | None = None,
        slot: str | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        return add_box(
            self.ledger, kind, name, parent=parent, slot=slot, notes=notes
        )

    def move(self, ref: str, *, parent: str, slot: str | None = None) -> dict[str, Any]:
        return move_box(self.ledger, ref, parent=parent, slot=slot)

    def show(self, ref: str) -> dict[str, Any]:
        from tetramark.ops.render import box_view

        return box_view(self.ledger, ref)

    def ls(
        self,
        kind: str | None = None,
        *,
        where: Iterable[str] | Mapping[str, Any] | None = (),
    ) -> list[dict[str, Any]]:
        return list_boxes(self.ledger, kind, where=where)


class AliasOps:
    """``ledger.aliases`` — alias commands (spec §6)."""

    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger

    def add(self, object_type: str, object_id: str, alias: str) -> dict[str, Any]:
        return add_alias(self.ledger, object_type, object_id, alias)

    def rm(self, object_type: str | None, alias: str) -> dict[str, Any]:
        return remove_alias(self.ledger, object_type, alias)
