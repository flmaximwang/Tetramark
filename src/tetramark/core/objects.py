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
* ``box_id``/``slot`` is set exactly when the status is in stock (spec 5): placing
  a tube restores an in-stock status, leaving stock clears the position in the
  same write, and the event keeps the old position in ``from_*``;
* box nesting must be acyclic and a slot holds aliquots *or* one child box;
* an aliquot's status belongs to its stock kind's status set, creation accepts
  any status of the set and every later change follows the set's ``to:`` rules
  (:func:`ensure_status_change`; ``--force --note`` records the exception).

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
from tetramark.core.filters import (
    build_conditions,
    check_fields,
    coerce_value,
    filter_fields,
    parse_filters,
    regex_usage_error,
    validate_patterns,
)
from tetramark.core.ids import IdError, aliquot_id, next_aliquot_id, parse_aliquot_id
from tetramark.errors import LedgerError, UsageError, try_find_hint
from tetramark.schema.ddl import (
    CORE_COLUMNS,
    box_view_name,
    box_view_statements,
    slot_rows,
    table_name,
)
from tetramark.schema.model import (
    Field,
    FieldType,
    GeometryType,
    Kind,
    StatusSet,
    box_name_problem,
)

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
    "box_kind_columns",
    "box_kind_from",
    "edit_entity",
    "edit_stock",
    "list_aliquots",
    "list_entities",
    "list_boxes",
    "list_stocks",
    "move_aliquot",
    "move_box",
    "plain_decimal",
    "purge_aliquot",
    "read_rename_map",
    "remove_alias",
    "renumber_stocks",
    "set_aliquot_status",
    "show_entity",
    "show_stock",
    "use_aliquot",
    "validate_creation_placement",
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
    # An aliquot has no kind row, so these are the columns ``--where`` can
    # address.  ``status`` is deliberately absent: ``tm aliquot list``'s
    # dedicated ``--status`` filter also decides the default in-stock scope,
    # and a second spelling for the same thing would drift from it.
    "aliquot": frozenset(
        {
            "id",
            "stock_id",
            "seq",
            "box_id",
            "slot",
            "quantity",
            "unit",
            "label",
            "created_at",
            "notes",
        }
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


# ---------------------------------------------------------------------------
# Status sets (spec 4 legacy / spec 6 fixed)
# ---------------------------------------------------------------------------


def aliquot_status_set(ledger: Ledger, stock_kind: str) -> StatusSet:
    """The **effective** status set of a stock kind.

    Spec 6 hardcodes one fixed set for every stock kind; a legacy ledger (its
    stored preset copies declare per-kind sets) keeps its kind's set.  The
    schema resolves both cases, so every status path below is model-agnostic.
    """
    status_set = ledger.schema.status_set(stock_kind)
    if status_set is None:  # pragma: no cover - callers pass a stock kind
        raise LedgerError(
            f"kind {stock_kind!r} is not a stock kind, so it has no aliquot "
            "status set"
        )
    return status_set


def _status_choice(statuses: Sequence[str]) -> str:
    return ", ".join(statuses)


def validate_creation_status(ledger: Ledger, stock_kind: str, status: str) -> str:
    """Validate a status chosen when an aliquot is **created** (any set status)."""
    status_set = aliquot_status_set(ledger, stock_kind)
    text = str(status).strip()
    if text not in status_set.names:
        raise UsageError(
            f"unknown status {status!r} for stock kind {stock_kind!r}; choose "
            f"from: {_status_choice(status_set.names)}"
        )
    return text


def default_aliquot_status(
    ledger: Ledger, stock_kind: str, *, placed: bool
) -> str:
    """The status of a newly created aliquot that did not name one.

    Creation is not a transition (spec 4), so any status of the stock kind's
    set may be chosen; the default keeps the spec-3 behaviour where possible:
    ``stored`` when the tube is placed in a box and ``pending`` otherwise.  A
    set without one of those (e.g. ``cryo`` has no ``pending``) falls back to
    its first in-stock status, then to its first status.
    """
    status_set = aliquot_status_set(ledger, stock_kind)
    preferred = ("stored", "pending") if placed else ("pending", "stored")
    for name in preferred:
        if status_set.has(name):
            return name
    in_stock = status_set.in_stock_names
    if in_stock:
        return in_stock[0]
    return status_set.names[0]


def status_is_in_stock(status_set: StatusSet, status: str) -> bool:
    """True when ``status`` counts as physically in stock (spec 4/5)."""
    entry = status_set.root.get(status)
    return bool(entry is not None and entry.in_stock)


def _statuses_text(statuses: Sequence[str]) -> str:
    return ", ".join(statuses) if statuses else "(none defined)"


def validate_creation_placement(
    ledger: Ledger,
    stock_kind: str,
    status: str,
    *,
    placed: bool,
    aliquot_id: str | None = None,
) -> None:
    """Enforce "a position exactly when in stock" on a creation path (spec 5).

    Creating an aliquot is not a status transition (spec 4), so any status of
    the stock kind's set may be chosen; the position must agree with it: a
    placed aliquot needs an in-stock status and an in-stock aliquot needs a
    position.  The app commands and the CSV importer share this check so both
    report the same refusal.
    """
    status_set = aliquot_status_set(ledger, stock_kind)
    in_stock = status_is_in_stock(status_set, status)
    label = "" if aliquot_id is None else f" {aliquot_id!r}"
    if placed and not in_stock:
        raise LedgerError(
            f"aliquot{label} would be placed in a box with status {status!r}, "
            f"which is not in stock; a placed aliquot must be in stock: choose "
            f"an in-stock status ({_statuses_text(status_set.in_stock_names)}) "
            "or omit the box"
        )
    if not placed and in_stock:
        rest = [
            name
            for name in status_set.names
            if not status_is_in_stock(status_set, name)
        ]
        hint = "choose a status that is not in stock"
        if rest:
            hint += f" ({', '.join(rest)})"
        raise LedgerError(
            f"aliquot{label} would have status {status!r}, which is in stock, "
            "but no box position: an in-stock aliquot must be placed in a box "
            f"(pass --box, and --slot for a slotted box) or {hint}"
        )


def in_stock_status_for_move(
    ledger: Ledger, stock_kind: str, requested: str | None = None
) -> str:
    """The in-stock status a not-in-stock aliquot gets when it is placed.

    A tube that is not in stock has no position (spec 5), so every move places
    it and restores an in-stock status.  With exactly one in-stock status that
    is unambiguous; with several (e.g. the ``worm`` set's ``growing`` and
    ``starved``) the caller must choose through ``--status``.
    """
    status_set = aliquot_status_set(ledger, stock_kind)
    in_stock = status_set.in_stock_names
    if not in_stock:
        raise LedgerError(
            f"stock kind {stock_kind!r} has no in-stock status; an aliquot of "
            "this kind cannot be placed in a box"
        )
    if requested is None:
        if len(in_stock) > 1:
            raise LedgerError(
                f"aliquot is not in stock and stock kind {stock_kind!r} has "
                f"several in-stock statuses ({_statuses_text(in_stock)}); pass "
                "--status STATUS to choose the one to restore"
            )
        return in_stock[0]
    text = str(requested).strip()
    if not status_set.has(text):
        raise UsageError(
            f"unknown status {requested!r} for stock kind {stock_kind!r}; "
            f"choose from: {_status_choice(status_set.names)}"
        )
    if not status_is_in_stock(status_set, text):
        raise UsageError(
            f"status {text!r} is not in stock; placing a tube in a box needs an "
            f"in-stock status: {_statuses_text(in_stock)}"
        )
    return text


def ensure_status_change(
    ledger: Ledger,
    stock_kind: str,
    old_status: str,
    new_status: str,
    *,
    force: bool = False,
) -> None:
    """The one check every status-changing path goes through (spec 4).

    A change must follow the stock kind's ``to:`` transitions; ``force``
    overrides a rule (the caller records ``aliquot_event.forced = TRUE`` and
    therefore must have a note).  Creation does not come through here.
    """
    status_set = aliquot_status_set(ledger, stock_kind)
    old = str(old_status)
    new = str(new_status)
    if old not in status_set.names:
        raise LedgerError(
            f"aliquot status {old_status!r} is not in the status set of stock "
            f"kind {stock_kind!r} ({_status_choice(status_set.names)}); fix the "
            "stored data ('tm check' reports it)"
        )
    if new not in status_set.names:
        raise UsageError(
            f"unknown status {new_status!r} for stock kind {stock_kind!r}; "
            f"choose from: {_status_choice(status_set.names)}"
        )
    if new == old:
        raise LedgerError(
            f"aliquot already has status {old!r}; nothing to change"
        )
    if status_set.allows(old, new):
        return
    if force:
        return
    targets = status_set.root[old].to
    allowed = (
        _status_choice(targets)
        if targets
        else "(final status: no unforced change)"
    )
    raise LedgerError(
        f"cannot change an aliquot of stock kind {stock_kind!r} from {old!r} to "
        f"{new!r}: allowed next status(es): {allowed}. Pass --force --note TEXT "
        "to record a forced exception (aliquot_event.forced = TRUE)"
    )


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


def plain_decimal(value: Any) -> str:
    """A decimal number as a plain string, without trailing zeros.

    The database stores quantities in ``DECIMAL(18,6)``, so a tube that holds
    200 ml reads back as ``200.000000``.  A human line says ``200``; this
    helper renders the number that way (``Decimal("150.000000")`` becomes
    ``"150"``, ``Decimal("12.500000")`` becomes ``"12.5"``).  A value that is
    not a number is returned unchanged.
    """
    if value is None:
        return ""
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return str(value)
    if not number.is_finite():
        return str(value)
    return format(number.normalize(), "f")


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


def _object_aliases(
    session: Any, object_type: str, object_id: str
) -> list[str]:
    """Every alias of one object, sorted (the shape ``show`` reports).

    The alias table is the only home of aliases; ``show``/``box show`` use this
    helper so the CLI never hand-rolls the query (spec §6).  An object without
    aliases returns an empty list.
    """
    rows = session.query(
        f"SELECT {_quote('alias')} FROM {_quote('alias')} "
        f"WHERE {_quote('object_type')} = %s AND {_quote('object_id')} = %s "
        f"ORDER BY {_quote('alias')}",
        (object_type, object_id),
    )
    return [str(row["alias"]) for row in rows]


def _resolve_core(session: Any, object_type: str, ref: str) -> dict[str, Any]:
    """Find a core row by ID, exact alias, or unique ID prefix (ID wins first).

    A complete ID or an exact alias always beats an incomplete ID, as ``find``
    ranks an exact hit above a partial one.  An incomplete ID is accepted only
    when exactly one object matches; a prefix that hits several objects is a
    usage error that names the candidates, so a mistyped or ambiguous reference
    never silently picks one (the git rule for abbreviated hashes).
    """
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

    text = str(ref)
    if text:
        matches = session.query(
            f"SELECT * FROM {_quote(object_type)} "
            f"WHERE LEFT({_quote('id')}, %s) = %s "
            f"ORDER BY {_quote('id')}",
            (len(text), text),
        )
        if len(matches) == 1:
            return dict(matches[0])
        if len(matches) > 1:
            shown = ", ".join(str(match["id"]) for match in matches[:10])
            more = f" and {len(matches) - 10} more" if len(matches) > 10 else ""
            raise UsageError(
                f"{object_type} id {ref!r} is ambiguous: it prefixes "
                f"{len(matches)} objects ({shown}{more}); use a longer prefix "
                "or the full id"
            )
    raise LedgerError(f"unknown {object_type} {ref!r}")


def _resolve_core_with_hint(
    session: Any, object_type: str, ref: str
) -> dict[str, Any]:
    """``_resolve_core`` plus a ``try tm find`` suffix on a plain miss.

    Single-object resolution never applies ``find.rewrites`` (Tetramark-53r.1),
    so the user-facing ``--entity`` and ``tm alias add`` resolvers point at
    ``tm find``, the command that does.  An ambiguous id prefix stays a usage
    error and gets no hint.
    """
    try:
        return _resolve_core(session, object_type, ref)
    except UsageError:
        raise
    except LedgerError as error:
        raise LedgerError(f"{error}{try_find_hint(ref)}") from error


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
    # An aliquot is a generic tube: it has no kind row of its own (spec §3).
    kind_row = (
        {}
        if object_type == "aliquot"
        else _fetch_kind_row(session, str(core["kind"]), object_type, object_id)
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


def box_kind_columns(kind: Kind) -> dict[str, str]:
    """The ``v_box_<kind>`` row shape as qualified column expressions.

    Spec 5 replaced the per-kind ``v_box_<kind>`` views with one view per box,
    so every reader that needs a box + its kind fields joins the core ``box``
    row with its ``box_<kind>`` row.  This returns the old view's columns in
    order, mapped to the SQL expression that produces each one; a kind field
    that reuses a core column name (``name``) wins over the core column, as it
    did in the old view (the kind table is the domain-specific definition).
    The expressions use the aliases :func:`box_kind_from` declares (``c`` for
    ``box`` and ``k`` for ``box_<kind>``).
    """
    core = CORE_COLUMNS["box"]
    overridden = {name for name in kind.fields if name in core}
    columns: dict[str, str] = {}
    for name in core:
        if name not in overridden:
            columns[name] = f"c.{_quote(name)}"
    for name in kind.fields:
        columns[name] = f"k.{_quote(name)}"
    return columns


def box_kind_from(kind_name: str) -> str:
    """``FROM`` clause that reproduces a ``v_box_<kind>`` view row (spec 5)."""
    return (
        f"{_quote(table_name('box', kind_name))} k "
        f"JOIN {_quote('box')} c ON c.{_quote('id')} = k.{_quote('id')}"
    )


def _box_alias_list_sql() -> str:
    """The ``alias_list`` column the removed ``v_box_<kind>`` views exposed."""
    return (
        "(SELECT GROUP_CONCAT(`alias` ORDER BY `alias` SEPARATOR ';') "
        "FROM `alias` WHERE `object_type` = 'box' AND `object_id` = c.`id`)"
    )


def _create_box_views(
    session: Any, ledger: Ledger, boxes: Sequence[Mapping[str, Any]]
) -> None:
    """Create the per-box views of newly created boxes in the open write.

    Called at the end of the write that created the boxes (with their
    ``id``/``name``/``kind`` mappings), so the view and the box row land in the
    same Dolt commit (spec 5); in a dry run the statements are only recorded.
    """
    for statement in box_view_statements(ledger.schema, boxes):
        session.execute(statement.sql)


def _check_box_name(session: Any, box_name: str) -> None:
    """Refuse a box name that cannot name its generated view (spec 5)."""
    existing = [
        str(row["name"])
        for row in session.query(f"SELECT {_quote('name')} FROM {_quote('box')}")
    ]
    problem = box_name_problem(box_name, existing)
    if problem is not None:
        raise UsageError(problem)


def _refuse_box_inside_grid(ledger: Ledger, parent_row: Mapping[str, Any]) -> None:
    """Grid boxes hold tubes only, so nothing may nest a box inside one."""
    geometry = _box_geometry(ledger.schema.kind(str(parent_row["kind"])))
    if geometry.type is GeometryType.grid:
        raise LedgerError(
            f"box {parent_row['name']!r} is a grid box: grid boxes hold tubes "
            "only, so a box cannot be nested in it"
        )


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


#: The rows a box path needs.  ``box list``/``box show`` read them in the same
#: :meth:`Driver.query_many` batch as everything else (never a round trip more).
_BOX_PATH_ROWS_SQL = "SELECT `id`, `name`, `parent_box_id` FROM `box`"

#: The direct-occupancy counts ``box list`` prints: one row per box that has
#: something directly in it.  Aliquots count whatever their status, child boxes
#: count as one item each, and the contents of nested boxes do not roll up.  It
#: rides the box list's read batch.
_BOX_OCCUPIED_SQL = (
    "SELECT `box_id`, SUM(`n`) AS `occupied` FROM ("
    "SELECT `box_id`, COUNT(*) AS `n` FROM `aliquot` "
    "WHERE `box_id` IS NOT NULL GROUP BY `box_id` "
    "UNION ALL "
    "SELECT `parent_box_id` AS `box_id`, COUNT(*) AS `n` FROM `box` "
    "WHERE `parent_box_id` IS NOT NULL GROUP BY `parent_box_id`"
    ") `per_box` GROUP BY `box_id`"
)

#: The per-entity stock counts ``entity list`` prints: one aggregate over the
#: rows of the already-derived ``v_stock_status`` view, so the in-stock
#: statuses are not re-derived here.  It rides the list's read batch.
_ENTITY_STOCK_COUNTS_SQL = (
    "SELECT `entity_id`, "
    "SUM(CASE WHEN `status` = 'active' THEN 1 ELSE 0 END) AS `stocks_available`, "
    "SUM(`aliquots_in_stock`) AS `aliquots_in_stock` "
    "FROM `v_stock_status` GROUP BY `entity_id`"
)


# ---------------------------------------------------------------------------
# Batched read helpers (list/show)
# ---------------------------------------------------------------------------
#
# ``tm entity list`` / ``stock list`` / ``box list`` / ``box show`` and the
# ``show`` commands must not pay one ``dolt sql`` process per kind or per
# lookup.  A read batch is built up front and executed by one
# :meth:`Driver.query_many` call; only statements whose *SQL* depends on a
# resolved row are written with a scalar subquery that repeats the resolution
# the write side (:func:`_resolve_core` / :func:`_resolve_box`) performs, so
# name-before-id-before-alias precedence and the error messages stay identical.


def _resolved_id_sql(object_type: str, *, by_name: bool = False) -> str:
    """A SQL scalar for the id :func:`_resolve_core`/:func:`_resolve_box` picks.

    The precedence is the write-side one: a box is resolved by human name
    first (``by_name``), then by id, then by alias.  The alias branch checks
    that the object still exists, exactly as the resolver's per-alias fetch
    does.  ``LIMIT 1`` mirrors the resolver returning the first row (ids and
    box names are unique, so it is belt and braces against corrupt data).
    """
    table = _quote(object_type)
    key = _quote("id")
    parts: list[str] = []
    if by_name:
        parts.append(f"(SELECT {key} FROM {table} WHERE {_quote('name')} = %s LIMIT 1)")
    parts.append(f"(SELECT {key} FROM {table} WHERE {key} = %s LIMIT 1)")
    parts.append(
        f"(SELECT a.{_quote('object_id')} FROM {_quote('alias')} a "
        f"WHERE a.{_quote('object_type')} = '{object_type}' "
        f"AND a.{_quote('alias')} = %s "
        f"AND EXISTS (SELECT 1 FROM {table} o "
        f"WHERE o.{key} = a.{_quote('object_id')}) LIMIT 1)"
    )
    # Tetramark-o4u: an incomplete id is accepted when exactly one object
    # matches.  The HAVING clause yields no row for zero or several matches;
    # the caller turns "several" into the UsageError that names candidates.
    parts.append(
        f"(SELECT MIN({key}) FROM {table} "
        f"WHERE LEFT({key}, LENGTH(%s)) = %s HAVING COUNT(*) = 1)"
    )
    return "COALESCE(" + ", ".join(parts) + ")"


def _resolved_id_params(sql: str, ref: str) -> tuple[str, ...]:
    """The parameters for :func:`_resolved_id_sql` (one per placeholder)."""
    return (ref,) * sql.count("%s")


def _prefix_matches(ledger: Ledger, object_type: str, ref: str) -> list[dict[str, Any]]:
    """Rows whose id starts with ``ref`` (the o4u incomplete-id candidates)."""
    if not ref:
        return []
    table = _quote(object_type)
    return ledger.driver.query(
        f"SELECT {_quote('id')} FROM {table} "
        f"WHERE LEFT({_quote('id')}, %s) = %s ORDER BY {_quote('id')}",
        (len(ref), ref),
    )


def _raise_ambiguous_prefix(
    object_type: str, ref: str, matches: Sequence[Mapping[str, Any]]
) -> None:
    """The exact ``_resolve_core`` error for a prefix that hits several objects."""
    shown = ", ".join(str(match["id"]) for match in matches[:10])
    more = f" and {len(matches) - 10} more" if len(matches) > 10 else ""
    raise UsageError(
        f"{object_type} id {ref!r} is ambiguous: it prefixes "
        f"{len(matches)} objects ({shown}{more}); use a longer prefix "
        "or the full id"
    )


def _read_record_batch(
    ledger: Ledger,
    object_type: str,
    ref: str,
    *,
    extras: Sequence[tuple[str, Any]] = (),
) -> tuple[dict[str, Any], list[str], list[list[dict[str, Any]]]]:
    """Read one object's core row, kind row and aliases in one batch.

    Returns ``(record, aliases, extra_result_sets)``.  ``record`` has the same
    shape :func:`_load_record` builds: the core columns, then the resolved
    kind row's fields except ``id``, then ``object``.  ``extras`` are further
    independent statements that need the same resolved id (``show_stock``'s
    aliquots); their result sets are returned in order.  An unknown ``ref``
    raises the resolver's error after the batch (the reads are side-effect
    free, so a miss costs one process instead of one per lookup).
    """
    if not ref:
        # ``resolve_any`` has nothing to match; without this the unique-prefix
        # SQL branch would treat "" as a prefix of every id.
        raise LedgerError(f"unknown {object_type} {ref!r}")
    id_sql = _resolved_id_sql(object_type)
    params = _resolved_id_params(id_sql, ref)
    table = _quote(object_type)
    # The kind table is selected only after the core row says which kind the
    # object is: a schema may declare a kind whose table has not been applied
    # yet (adopt/migration in progress), and selecting every kind table here
    # would make show fail on an unrelated missing table.
    batch: list[tuple[str, Any]] = [
        (f"SELECT * FROM {table} WHERE {_quote('id')} = {id_sql}", params),
        (
            f"SELECT {_quote('object_id')}, {_quote('alias')} "
            f"FROM {_quote('alias')} "
            f"WHERE {_quote('object_type')} = '{object_type}' "
            f"AND {_quote('object_id')} = {id_sql} "
            f"ORDER BY {_quote('alias')}",
            params,
        ),
    ]
    batch.extend(extras)
    fetched = ledger.driver.query_many(batch)

    core_rows = fetched[0]
    if not core_rows:
        matches = _prefix_matches(ledger, object_type, ref)
        if len(matches) > 1:
            _raise_ambiguous_prefix(object_type, ref, matches)
        if len(matches) == 1:
            # The unique prefix branch normally resolved this inside the batch;
            # retry once with the full id if Dolt returned no row (defensive).
            return _read_record_batch(
                ledger, object_type, str(matches[0]["id"]), extras=extras
            )
        raise LedgerError(f"unknown {object_type} {ref!r}")
    core = dict(core_rows[0])
    kind_name = str(core["kind"])
    kind_rows = ledger.driver.query(
        f"SELECT * FROM {_quote(table_name(object_type, kind_name))} "
        f"WHERE {_quote('id')} = %s",
        (str(core["id"]),),
    )
    record = dict(core)
    if kind_rows:
        kind_row = dict(kind_rows[0])
    else:  # pragma: no cover - a core row always has its kind row
        kind_row = {}
    for key, value in kind_row.items():
        if key != "id":
            record[key] = value
    record["object"] = object_type
    aliases = [str(row["alias"]) for row in fetched[1]]
    return record, aliases, fetched[2:]


def _wrong_type_hint(ledger: Ledger, object_type: str, ref: str) -> str:
    """The ``: <ref> is a <type>; try tm <type> show <ref>`` suffix, or ``""``.

    ``tm aliquot show <stock>`` and ``tm stock show <aliquot>`` are easy
    mistakes, so their not-found error names the verb that would work (spec
    §6).  The ref is resolved like ``tm find`` does, through
    :func:`tetramark.ops.find.resolve_any`; the import is local because
    ``ops.find`` imports this module at module level.

    A ref that names nothing, an empty or ambiguous ref (``UsageError``) and a
    ref of a third object type get no type hint; the callers append the
    plain ``try tm find`` hint instead (Tetramark-53r.1).
    """
    text = str(ref).strip()
    if not text:
        return ""
    from tetramark.ops.find import resolve_any

    try:
        found = resolve_any(ledger, ref)
    except UsageError:
        # Empty or ambiguous: the resolver cannot name one object.
        return ""
    except LedgerError:
        # Names nothing at all.
        return ""
    found_type = str(found["object"])
    if object_type == "aliquot" and found_type == "stock":
        return f": {text} is a stock; try tm stock show {text}"
    if object_type == "stock" and found_type == "aliquot":
        return f": {text} is an aliquot; try tm aliquot show {text}"
    return ""


def _read_box_rows(
    ledger: Ledger, ref: str
) -> tuple[
    dict[str, Any],
    dict[str, str],
    list[str],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    """One box and everything ``box show`` renders, in one read batch.

    Returns ``(box_row, paths, aliases, slot_rows, aliquot_rows, child_rows)``.
    The view rows are fetched for every box (a slotless box matches none of the
    slotted rows), so the batch does not depend on the resolved kind; the
    caller picks the ones its geometry needs.
    """
    if not ref:
        raise LedgerError(f"unknown box {ref!r}")
    id_sql = _resolved_id_sql("box", by_name=True)
    params = _resolved_id_params(id_sql, ref)
    batch: list[tuple[str, Any]] = [
        (f"SELECT * FROM {_quote('box')} WHERE {_quote('id')} = {id_sql}", params),
        (_BOX_PATH_ROWS_SQL, None),
        (
            f"SELECT {_quote('alias')} FROM {_quote('alias')} "
            f"WHERE {_quote('object_type')} = 'box' "
            f"AND {_quote('object_id')} = {id_sql} "
            f"ORDER BY {_quote('alias')}",
            params,
        ),
        (
            "SELECT * FROM `v_box_slots` "
            f"WHERE `box_id` = {id_sql} ORDER BY `ordinal`",
            params,
        ),
        (
            "SELECT a.*, s.`entity_id` AS `entity_id` FROM `aliquot` a "
            "JOIN `stock` s ON s.`id` = a.`stock_id` "
            f"WHERE a.`box_id` = {id_sql} AND a.`slot` IS NULL "
            "ORDER BY a.`id`",
            params,
        ),
        (
            "SELECT `id`, `name` FROM `box` "
            f"WHERE `parent_box_id` = {id_sql} AND `parent_slot` IS NULL "
            "ORDER BY `name`",
            params,
        ),
    ]
    fetched = ledger.driver.query_many(batch)
    box_rows = fetched[0]
    if not box_rows:
        matches = _prefix_matches(ledger, "box", ref)
        if len(matches) > 1:
            _raise_ambiguous_prefix("box", ref, matches)
        if len(matches) == 1:
            return _read_box_rows(ledger, str(matches[0]["id"]))
        # ``_resolve_box`` falls through to ``_resolve_core``'s error.
        raise LedgerError(f"unknown box {ref!r}{try_find_hint(ref)}")
    return (
        dict(box_rows[0]),
        _box_paths(fetched[1]),
        [str(row["alias"]) for row in fetched[2]],
        fetched[3],
        fetched[4],
        fetched[5],
    )


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
    *,
    extra: Sequence[tuple[str, Any]] = (),
) -> tuple[list[dict[str, Any]], list[list[dict[str, Any]]]]:
    """Every row of every matching kind, fetched in one read batch.

    Returns ``(records, extra_result_sets)``: one SELECT per kind whose columns
    can express the filters (same SQL, order and parameters as the old
    per-kind queries) is built first and run by a single
    :meth:`Driver.query_many`, so the fan-out costs one process instead of one
    per kind.  ``extra`` statements are appended to the same batch (``box
    list`` uses that for the path rows); their results come back in order.
    Empty kinds still contribute nothing, and the records keep the kind order
    plus each query's ``ORDER BY``.

    The filters themselves are parsed and turned into SQL conditions by
    :mod:`tetramark.core.filters`, the one backend ``--where`` shares with
    ``tm find``: ``field=value`` is exact (with the schema's coercion for a
    kind field), ``field~pattern`` matches the column's text form.
    """
    if kind_name is not None:
        _kind_for(ledger, object_type, kind_name)
        kinds = [kind_name]
    else:
        kinds = sorted(ledger.schema.kinds_for(object_type))

    filters = parse_filters(where, option="--where")
    valid: set[str] = set(_CORE_FILTERS[object_type])
    for name in kinds:
        valid |= set(ledger.schema.kinds[name].fields)
    check_fields(filters, valid, option="--where")

    records: list[dict[str, Any]] = []
    batch: list[tuple[str, Any]] = []
    for name in kinds:
        kind = ledger.schema.kinds[name]
        columns = set(_CORE_FILTERS[object_type]) | set(kind.fields)
        # A kind that cannot express every filter would otherwise contribute
        # rows that ignore the filter; leave it out entirely.
        if not filter_fields(filters).issubset(columns):
            continue
        # Spec 5 dropped the per-kind box views; read the same row shape by
        # joining the core `box` row with its `box_<kind>` row.
        column_sql: dict[str, str] | None = (
            box_kind_columns(kind) if object_type == "box" else None
        )
        expressions = column_sql or {field: _quote(field) for field in columns}
        conditions, params = build_conditions(
            filters,
            expressions=expressions,
            coerce=lambda field, raw: coerce_value(
                kind, field, raw, ledger=ledger, session=ledger.driver
            ),
        )
        if column_sql is not None:
            select_items = [
                f"{expression} AS {_quote(column)}"
                for column, expression in column_sql.items()
            ]
            select_items.append(
                f"{_box_alias_list_sql()} AS {_quote('alias_list')}"
            )
            sql = f"SELECT {', '.join(select_items)} FROM {box_kind_from(name)}"
            order = f"c.{_quote('id')}"
        else:
            sql = f"SELECT * FROM {_quote(f'v_{object_type}_{name}')}"
            order = _quote("id")
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += f" ORDER BY {order}"
        batch.append((sql, tuple(params)))

    extra_start = len(batch)
    batch.extend(extra)
    # No kind carries the filters (a combination no kind can express), so no
    # query compiles their patterns: validate them here, otherwise an invalid
    # pattern would silently return an empty listing.
    if extra_start == 0:
        validate_patterns(ledger.driver, filters, option="--where")
    # One process for every kind (plus the batch-only ``extra`` reads); an
    # empty batch must not start one at all.
    try:
        fetched = ledger.driver.query_many(batch) if batch else []
    except LedgerError as error:
        # The database compiles a pattern, so only a failed query knows that
        # one is invalid; the filter backend turns that into a usage error
        # that names the filter the user has to fix.
        usage = regex_usage_error(ledger.driver, filters, error, option="--where")
        if usage is None:
            raise
        raise usage from error
    for rows in fetched[:extra_start]:
        for row in rows:
            record = dict(row)
            record["object"] = object_type
            records.append(record)
    return records, fetched[extra_start:]


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


def _rename_summary(object_id: str, old_name: Any, new_name: str) -> str:
    """Commit summary of a rename: ``OLD -> NEW`` when ASCII, else the id."""
    text = f"{old_name} -> {new_name}"
    return text if old_name is not None and text.isascii() else object_id


def _keep_old_name_alias(
    session: Any, object_type: str, object_id: str, old_name: Any
) -> bool:
    """Record a renamed object's previous name as one of its aliases.

    Returns ``True`` when a new alias row was written.  An object that already
    carries the old name as an alias keeps it unchanged; an alias owned by
    another object of the same type aborts the rename (the old name could not
    keep finding this object).
    """
    if old_name is None or not str(old_name).strip():
        return False
    text = str(old_name).strip()
    owners = session.query(
        f"SELECT {_quote('object_id')} FROM {_quote('alias')} "
        f"WHERE {_quote('object_type')} = %s AND {_quote('alias')} = %s",
        (object_type, text),
    )
    if owners:
        owner = str(owners[0]["object_id"])
        if owner == object_id:
            return False
        raise LedgerError(
            f"cannot keep the old name {text!r} as an alias: it is already an "
            f"alias of {object_type} {owner!r}"
        )
    _add_alias_row(session, object_type, object_id, text)
    return True


def _clean_new_name(new_name: str) -> str:
    text = str(new_name).strip()
    if not text:
        raise UsageError("the new name must not be empty")
    return text


def rename_entity(ledger: Ledger, ref: str, new_name: str) -> dict[str, Any]:
    """Give an entity a new name and keep the old name as an alias.

    ``entity.name`` and a kind's own ``name`` field change together (as
    ``edit --set name=...`` does); the previous name becomes an alias of the
    entity in the same commit, so ``find``/``show`` and every ``--entity REF``
    still resolve it.
    """
    name = _clean_new_name(new_name)
    existing = _resolve_core(ledger.driver, "entity", ref)
    entity_id = str(existing["id"])
    kind_name = str(existing["kind"])
    kind = ledger.schema.kind(kind_name)
    old_name = existing.get("name")
    if old_name == name:
        raise UsageError(f"entity {entity_id!r} is already named {name!r}")

    with ledger.write(
        _rename_summary(entity_id, old_name, name), verb="rename", object="entity"
    ) as session:
        core, fields = _split_values(
            session, ledger, kind, kind_name, "entity", {"name": name}
        )
        _mirror_name(kind, core, fields)
        if core:
            _update(session, "entity", {"id": entity_id}, core)
        if fields:
            _update(
                session, table_name("entity", kind_name), {"id": entity_id}, fields
            )
        _keep_old_name_alias(session, "entity", entity_id, old_name)
        aliases = _object_aliases(session, "entity", entity_id)

    if old_name is not None and str(old_name).strip() not in aliases:
        aliases = sorted({*aliases, str(old_name).strip()})  # dry run
    return {
        "object": "entity",
        "kind": kind_name,
        "id": entity_id,
        "name": name,
        "old_name": old_name,
        "aliases": aliases,
    }


def show_entity(ledger: Ledger, ref: str) -> dict[str, Any]:
    """One entity (core + kind fields), accepting an ID or an alias.

    ``aliases`` is the object's sorted alias list (spec §6); the generated
    views expose the same data as ``alias_list``.  A miss is a not-found
    error that points at ``tm find`` (which applies ``find.rewrites``).
    """
    try:
        record, aliases, _ = _read_record_batch(ledger, "entity", ref)
    except UsageError:
        # An ambiguous id prefix is a usage error; it keeps its exit code.
        raise
    except LedgerError as error:
        raise LedgerError(f"{error}{try_find_hint(ref)}") from error
    record["aliases"] = aliases
    return record


def list_entities(
    ledger: Ledger,
    kind_name: str | None = None,
    *,
    where: Iterable[str] | Mapping[str, Any] | None = (),
) -> list[dict[str, Any]]:
    # The stock counts ride in the same batch as the per-kind listings, so the
    # per-entity aggregate costs no extra round trip.
    records, extra = _list_records(
        ledger,
        "entity",
        kind_name,
        where,
        extra=[(_ENTITY_STOCK_COUNTS_SQL, None)],
    )
    counts = {str(row["entity_id"]): row for row in extra[0]}
    for record in records:
        row = counts.get(str(record["id"]), {})
        # Dolt/pymysql may return the SUM as a float on the opt-in server
        # driver; counts are ints on every backend (like ``list_stocks``).
        record["stocks_available"] = int(row.get("stocks_available") or 0)
        record["aliquots_in_stock"] = int(row.get("aliquots_in_stock") or 0)
    return records


# ---------------------------------------------------------------------------
# Stocks
# ---------------------------------------------------------------------------


def _insert_aliquot(
    session: Any,
    ledger: Ledger,
    *,
    stock_id: str,
    stock_kind: str,
    seq: int,
    box_row: Mapping[str, Any] | None,
    slot: str | None,
    quantity: Decimal | None,
    unit: str | None,
    status: str | None = None,
) -> str:
    alq_id = aliquot_id(stock_id, seq)
    placed_slot: str | None = None
    if box_row is not None:
        placed_slot = _check_aliquot_placement(session, ledger, box_row, slot)
    if status is None:
        status = default_aliquot_status(
            ledger, stock_kind, placed=box_row is not None
        )
    else:
        status = validate_creation_status(ledger, stock_kind, status)
    validate_creation_placement(
        ledger,
        stock_kind,
        status,
        placed=box_row is not None,
        aliquot_id=alq_id,
    )
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
    id: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    """Create a stock, its kind row, and its aliquots (1 by default).

    ``status`` chooses the aliquot status at **creation** (spec 4: any status
    of the stock kind's set is accepted, creation is not a transition).
    """
    kind = _kind_for(ledger, "stock", kind_name)
    count = _parse_count(aliquots, option="--aliquots")
    amount, unit_text = _parse_quantity_unit(quantity, unit)
    if count == 0 and (box is not None or list(slots)):
        raise UsageError("cannot place aliquots when --aliquots is 0")
    if count == 0 and status is not None:
        raise UsageError("cannot set an aliquot status when --aliquots is 0")
    if box is None and list(slots):
        raise UsageError("--slot requires --box")
    slot_list = _place_slots(slots, count=count)

    stock_id = id or ledger.next_id("stock")
    if id is not None:
        ledger.id_strategy("stock").require_valid(id)

    with ledger.write(
        f"{kind_name} {stock_id} x{count}", verb="add", object="stock"
    ) as session:
        entity_row = _resolve_core_with_hint(session, "entity", entity)
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
        chosen_status = (
            validate_creation_status(ledger, kind_name, status)
            if status is not None
            else default_aliquot_status(
                ledger, kind_name, placed=box_row is not None
            )
        )
        created: list[str] = []
        for index in range(1, count + 1):
            alq_id = _insert_aliquot(
                session,
                ledger,
                stock_id=stock_id,
                stock_kind=kind_name,
                seq=index,
                box_row=box_row,
                slot=slot_list[index - 1],
                quantity=amount,
                unit=unit_text,
                status=chosen_status,
            )
            record_event(
                session,
                aliquot_id=alq_id,
                event="create",
                amount=amount,
                unit=unit_text,
                to_box_id=box_row["id"] if box_row is not None else None,
                to_slot=slot_list[index - 1] if box_row is not None else None,
                new_status=chosen_status,
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
    """One stock, its sorted aliases (``aliases``) and its aliquots.

    A ref that names an aliquot is a not-found error that points at
    ``tm aliquot show`` (spec §6); any other miss points at ``tm find``,
    which applies the ledger's ``find.rewrites`` rules.
    """
    id_sql = _resolved_id_sql("stock")
    params = _resolved_id_params(id_sql, ref)
    try:
        record, aliases, extra = _read_record_batch(
            ledger,
            "stock",
            ref,
            extras=[
                (
                    "SELECT `id`, `seq`, `status`, `quantity`, `unit`, `box_id`, `slot` "
                    f"FROM `aliquot` WHERE `stock_id` = {id_sql} ORDER BY `seq`",
                    params,
                )
            ],
        )
    except UsageError:
        # An ambiguous id prefix is a usage error; it keeps its exit code.
        raise
    except LedgerError as error:
        hint = _wrong_type_hint(ledger, "stock", ref)
        if not hint:
            # A plain miss points at ``tm find`` (rewrite rules live there).
            raise LedgerError(f"{error}{try_find_hint(ref)}") from error
        raise LedgerError(f"{error}{hint}") from error
    record["aliases"] = aliases
    record["aliquots"] = extra[0]
    return record


def list_stocks(
    ledger: Ledger,
    kind_name: str | None = None,
    *,
    where: Iterable[str] | Mapping[str, Any] | None = (),
) -> list[dict[str, Any]]:
    records, _ = _list_records(ledger, "stock", kind_name, where)
    for record in records:
        # v_stock_<kind>'s SUM is a DOUBLE on the server driver, but a count is
        # an int on every backend (lineage int-casts its counts the same way).
        record["aliquots_in_stock"] = int(record["aliquots_in_stock"])
    return records


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
    status: str | None = None,
) -> dict[str, Any]:
    """Add ``count`` aliquots to an existing stock.

    ``status`` chooses the status at creation (any status of the stock kind's
    set, spec 4).
    """
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
        chosen_status = (
            validate_creation_status(ledger, kind_name, status)
            if status is not None
            else default_aliquot_status(
                ledger, kind_name, placed=box_row is not None
            )
        )
        created: list[str] = []
        for offset in range(count):
            alq_id = _insert_aliquot(
                session,
                ledger,
                stock_id=stock_id,
                stock_kind=kind_name,
                seq=first_seq + offset,
                box_row=box_row,
                slot=slot_list[offset],
                quantity=amount,
                unit=unit_text,
                status=chosen_status,
            )
            record_event(
                session,
                aliquot_id=alq_id,
                event="create",
                amount=amount,
                unit=unit_text,
                to_box_id=box_row["id"] if box_row is not None else None,
                to_slot=slot_list[offset] if box_row is not None else None,
                new_status=chosen_status,
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
    status: str | None = None,
    message: str,
) -> dict[str, Any]:
    """Move an aliquot to a box/slot, recording a ``move`` event.

    Position and stock membership are the same fact (spec 5): an aliquot that
    is not in stock has no position, so moving it into a box restores the
    stock kind's in-stock status.  A set with several in-stock statuses (e.g.
    ``worm``) needs ``status`` to choose one; ``status`` is validated as an
    in-stock status of the set.  An aliquot that is already in stock keeps its
    status; ``status`` then goes through the normal transition check
    (:func:`ensure_status_change`) and the result still has to be in stock.

    A move is a real-world action that cannot simply be reversed, so
    ``message`` is required (spec §1, §6): it is trimmed, refused when empty,
    and stored in ``aliquot_event.note`` (``purpose`` stays NULL).
    """
    message_text = str(message).strip()
    if not message_text:
        raise UsageError("a move message is required and must not be empty")
    existing = _resolve_core(ledger.driver, "aliquot", ref)
    alq_id = str(existing["id"])

    with ledger.write(alq_id, verb="move", object="aliquot") as session:
        row = _resolve_core(session, "aliquot", alq_id)
        stock_row = _fetch_core(session, "stock", str(row["stock_id"]))
        assert stock_row is not None  # FK guarantees it
        kind_name = str(stock_row["kind"])
        status_set = aliquot_status_set(ledger, kind_name)
        old_status = str(row.get("status"))
        if old_status not in status_set.names:
            raise LedgerError(
                f"aliquot status {old_status!r} is not in the status set of "
                f"stock kind {kind_name!r} "
                f"({_status_choice(status_set.names)}); fix the stored data "
                "('tm check' reports it)"
            )
        if status_is_in_stock(status_set, old_status):
            if status is None:
                new_status = old_status
            else:
                new_status = in_stock_status_for_move(ledger, kind_name, status)
                if new_status != old_status:
                    ensure_status_change(
                        ledger, kind_name, old_status, new_status
                    )
        else:
            new_status = in_stock_status_for_move(ledger, kind_name, status)

        box_row = _resolve_box(session, box)
        placed_slot = _check_aliquot_placement(
            session, ledger, box_row, slot, exclude_aliquot=alq_id
        )
        _update(
            session,
            "aliquot",
            {"id": alq_id},
            {
                "box_id": box_row["id"],
                "slot": placed_slot,
                "status": new_status,
            },
        )
        record_event(
            session,
            aliquot_id=alq_id,
            event="move",
            from_box_id=row.get("box_id"),
            from_slot=row.get("slot"),
            to_box_id=box_row["id"],
            to_slot=placed_slot,
            old_status=old_status,
            new_status=new_status,
            actor=ledger.actor,
            note=message_text,
        )
        record = _load_record(session, ledger, "aliquot", alq_id)
    # A dry run buffers the UPDATE; report the destination it would write.
    record["box_id"] = box_row["id"]
    record["slot"] = placed_slot
    record["old_status"] = old_status
    record["status"] = new_status
    return record


def use_aliquot(
    ledger: Ledger,
    ref: str,
    amount: Any = None,
    unit: str | None = None,
    *,
    message: str,
) -> dict[str, Any]:
    """Consume part or all of an aliquot, recording a ``use`` event.

    ``amount`` and ``unit`` come together: passing only one of them is a
    usage error.  Leaving both out means "use everything left" — the event
    then records the tube's remaining quantity and unit, exactly like an
    explicit ``use REF <rest> <unit>`` would.  A tube whose quantity is
    NULL can only be used whole: the event records a NULL amount and the
    tube is consumed, while a partial use of such a tube is refused.

    Reaching zero moves the tube to ``consumed`` (every status of the
    fixed set may go there) and clears its box slot in the same write; the
    event keeps the old position in ``from_*``.  A legacy ledger whose
    stored status set has no ``consumed`` (the bundled ``cryo`` set) keeps
    the current status: the set has no "used up" state, and the operator
    records the physical fate with ``tm aliquot status``.

    A use is an action in the physical world that cannot simply be
    reversed, so ``message`` is required (spec §1, §6): it is trimmed,
    refused when empty, and stored in ``aliquot_event.note`` (``purpose``
    stays NULL).
    """
    message_text = str(message).strip()
    if not message_text:
        raise UsageError("a use message is required and must not be empty")
    if (amount is None) != (unit is None):
        raise UsageError(
            "amount and unit must be given together: pass both, or neither "
            "(without an amount the whole tube is used)"
        )

    used: Decimal | None = None
    unit_text: str | None = None
    if amount is not None:
        try:
            used = Decimal(str(amount).strip())
        except (InvalidOperation, ValueError):
            raise UsageError(
                f"amount must be a decimal number (got {amount!r})"
            ) from None
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
        stored_unit = row.get("unit")
        stored_unit_text = None if stored_unit is None else str(stored_unit)
        stored_quantity = row.get("quantity")

        event_amount: Decimal | None
        event_unit: str | None
        remaining: Decimal | None
        emptied: bool
        if stored_quantity is None:
            # A tube without a quantity can only be used whole: the event
            # records a NULL amount and the tube is consumed.
            if used is not None:
                raise LedgerError(
                    f"aliquot {alq_id!r} has no quantity: it can only be used "
                    "whole (omit the amount)"
                )
            event_amount = None
            event_unit = stored_unit_text
            remaining = None
            emptied = True
        else:
            current = Decimal(str(stored_quantity))
            if used is None:
                # "Use everything left": the event records the remaining
                # quantity and unit, exactly like an explicit use of them.
                used = current
                unit_text = stored_unit_text
            elif stored_unit_text is None or stored_unit_text != unit_text:
                raise LedgerError(
                    f"unit mismatch for aliquot {alq_id!r}: it holds "
                    f"{stored_unit!r}, not {unit_text!r}"
                )
            remaining = current - used
            if remaining < 0:
                raise LedgerError(
                    f"cannot use {used} {unit_text} from aliquot {alq_id!r}: "
                    f"only {current} left"
                )
            event_amount = used
            event_unit = unit_text
            emptied = remaining == 0

        new_status = old_status
        #: ``(from_box_id, from_slot)`` of a position this use clears (spec 5).
        cleared_from: tuple[Any, Any] = (None, None)
        if emptied:
            stock_row = _fetch_core(session, "stock", str(row["stock_id"]))
            assert stock_row is not None  # FK guarantees it
            kind_name = str(stock_row["kind"])
            status_set = aliquot_status_set(ledger, kind_name)
            if status_set.has("consumed"):
                ensure_status_change(ledger, kind_name, old_status, "consumed")
                new_status = "consumed"
                # A consumed aliquot is not in stock, so it is not in a box:
                # clear the position in the same write; the event keeps it.
                if not status_is_in_stock(status_set, new_status):
                    cleared_from = (row.get("box_id"), row.get("slot"))
        updates: dict[str, Any] = {"status": new_status}
        if remaining is not None:
            updates["quantity"] = remaining
        if cleared_from != (None, None):
            updates["box_id"] = None
            updates["slot"] = None
        _update(session, "aliquot", {"id": alq_id}, updates)
        record_event(
            session,
            aliquot_id=alq_id,
            event="use",
            amount=event_amount,
            unit=event_unit,
            from_box_id=cleared_from[0],
            from_slot=cleared_from[1],
            to_box_id=None,
            to_slot=None,
            old_status=old_status,
            new_status=new_status,
            actor=ledger.actor,
            note=message_text,
        )
        return {
            "object": "aliquot",
            "id": alq_id,
            "used_amount": (
                None if event_amount is None else plain_decimal(event_amount)
            ),
            "used_unit": event_unit,
            "quantity": None if remaining is None else str(remaining),
            "unit": event_unit,
            "status": new_status,
            "message": message_text,
        }


def set_aliquot_status(
    ledger: Ledger,
    ref: str,
    status: str,
    *,
    note: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Change an aliquot's status, recording a ``status`` event.

    The change must follow the stock kind's status set (spec 4);
    ``force=True`` overrides a transition rule, must carry a note, and records
    ``aliquot_event.forced = TRUE`` so ``tm check`` can tell a deliberate
    exception from a hand-made illegal row.
    """
    note_text = None if note is None else str(note).strip()
    if force and not note_text:
        raise UsageError(
            "--force requires --note: record why the transition rules are "
            "overridden"
        )
    existing = _resolve_core(ledger.driver, "aliquot", ref)
    alq_id = str(existing["id"])

    with ledger.write(alq_id, verb="status", object="aliquot") as session:
        row = _resolve_core(session, "aliquot", alq_id)
        old_status = str(row.get("status"))
        stock_row = _fetch_core(session, "stock", str(row["stock_id"]))
        assert stock_row is not None  # FK guarantees it
        kind_name = str(stock_row["kind"])
        ensure_status_change(
            ledger, kind_name, old_status, status, force=force
        )
        status_set = aliquot_status_set(ledger, kind_name)
        target_text = str(status).strip()
        target = status_set.root[target_text]
        updates: dict[str, Any] = {"status": target_text}
        if target.in_stock:
            # In stock means physically placed (spec 5): a position is
            # required and is kept.  The position check that used to be only
            # "a slotted box needs a slot" is now part of this invariant.
            if row.get("box_id") is None:
                raise LedgerError(
                    f"aliquot {alq_id!r} has no box position; status "
                    f"{target_text!r} is in stock, so the tube must be placed "
                    f"first: run 'tm aliquot move {alq_id} --box NAME --slot "
                    "SLOT -m MSG'"
                )
            box_row = _fetch_core(session, "box", str(row["box_id"]))
            if box_row is not None:
                geometry = _box_geometry(ledger.schema.kind(str(box_row["kind"])))
                if geometry.is_slotted and not row.get("slot"):
                    raise LedgerError(
                        f"aliquot {alq_id!r} is in slotted box "
                        f"{box_row['name']!r} but has no slot; run "
                        f"'tm aliquot move {alq_id} --box {box_row['name']} "
                        "--slot SLOT -m MSG' to give it one"
                    )
        else:
            # A status that is not in stock has no position: clear it in the
            # same write; the event keeps the old position in from_*.
            updates["box_id"] = None
            updates["slot"] = None
        _update(session, "aliquot", {"id": alq_id}, updates)
        # The event records a position only when this change clears it; an
        # unchanged position is not a move (from_* and to_* stay NULL).
        cleared = not target.in_stock
        record_event(
            session,
            aliquot_id=alq_id,
            event="status",
            from_box_id=row.get("box_id") if cleared else None,
            from_slot=row.get("slot") if cleared else None,
            to_box_id=None,
            to_slot=None,
            old_status=old_status,
            new_status=target_text,
            forced=force,
            actor=ledger.actor,
            note=note_text,
        )
        return {
            "object": "aliquot",
            "id": alq_id,
            "old_status": old_status,
            "status": target_text,
            "forced": force,
        }


def purge_aliquot(ledger: Ledger, ref: str) -> dict[str, Any]:
    """Hard-delete an aliquot and its events.

    Spec 3 has no per-stock-kind extension tables (an aliquot is a generic
    tube), so there is no extension row to delete.
    """
    existing = _resolve_core(ledger.driver, "aliquot", ref)
    alq_id = str(existing["id"])

    with ledger.write(alq_id, verb="purge", object="aliquot") as session:
        _resolve_core(session, "aliquot", alq_id)
        events = purge_events(session, alq_id)
        session.execute(
            f"DELETE FROM {_quote('aliquot')} WHERE {_quote('id')} = %s", (alq_id,)
        )
    return {"object": "aliquot", "id": alq_id, "purged": True, "events": events}


# ---------------------------------------------------------------------------
# Aliquot reads
# ---------------------------------------------------------------------------


def _aliquot_id_sql() -> str:
    """The id of the aliquot ``REF`` names: an exact id, then an alias.

    An aliquot id is ``<stock>.<seq>`` and is never shortened (spec §6), so
    the unique-prefix branch of :func:`_resolved_id_sql` is deliberately not
    used here: a prefix of an aliquot id is not a handle.  The alias branch
    checks that the tube still exists, exactly as the resolver's per-alias
    fetch does, so an alias of a purged tube resolves to nothing.
    """
    table = _quote("aliquot")
    key = _quote("id")
    return (
        "COALESCE("
        f"(SELECT {key} FROM {table} WHERE {key} = %s LIMIT 1), "
        f"(SELECT a.{_quote('object_id')} FROM {_quote('alias')} a "
        f"WHERE a.{_quote('object_type')} = 'aliquot' "
        f"AND a.{_quote('alias')} = %s "
        f"AND EXISTS (SELECT 1 FROM {table} o "
        f"WHERE o.{key} = a.{_quote('object_id')}) LIMIT 1)"
        ")"
    )


def show_aliquot(ledger: Ledger, ref: str) -> dict[str, Any]:
    """One aliquot: its row, aliases, owning stock and location (spec §6).

    Everything is read in one batch: the aliquot row, its sorted aliases, the
    owning stock's kind and entity, and the path from ``v_aliquot_location``.
    ``in_stock`` is derived from the stock kind's status set (spec 4): it says
    whether the tube's status counts as physically in stock.  A tube that is
    not in stock carries no position, so its ``box_id``, ``slot`` and ``path``
    are NULL.

    The stock kind's own fields are not part of the record — ``tm stock show``
    owns them — and the record carries no event timeline (``tm history`` owns
    that).  A ref that names a stock is a not-found error that points at
    ``tm stock show`` (spec §6); any other miss points at ``tm find``,
    which applies the ledger's ``find.rewrites`` rules.
    """
    if not ref:
        raise LedgerError(f"unknown aliquot {ref!r}")
    id_sql = _aliquot_id_sql()
    params = (str(ref), str(ref))
    batch: list[tuple[str, Any]] = [
        (
            f"SELECT * FROM {_quote('aliquot')} WHERE {_quote('id')} = {id_sql}",
            params,
        ),
        (
            f"SELECT {_quote('alias')} FROM {_quote('alias')} "
            f"WHERE {_quote('object_type')} = 'aliquot' "
            f"AND {_quote('object_id')} = {id_sql} "
            f"ORDER BY {_quote('alias')}",
            params,
        ),
        (
            "SELECT s.`kind` AS `stock_kind`, s.`entity_id` AS `entity_id` "
            f"FROM {_quote('stock')} s WHERE s.{_quote('id')} = "
            f"(SELECT a.{_quote('stock_id')} FROM {_quote('aliquot')} a "
            f"WHERE a.{_quote('id')} = {id_sql})",
            params,
        ),
        (
            "SELECT COALESCE(l.`location`, l.`box_path`) AS `path` "
            f"FROM {_quote('v_aliquot_location')} l "
            f"WHERE l.{_quote('aliquot_id')} = {id_sql}",
            params,
        ),
    ]
    fetched = ledger.driver.query_many(batch)

    rows = fetched[0]
    if not rows:
        hint = _wrong_type_hint(ledger, "aliquot", ref) or try_find_hint(ref)
        raise LedgerError(f"unknown aliquot {ref!r}{hint}")
    row = dict(rows[0])
    stock_rows = fetched[2]
    stock_kind = str(stock_rows[0]["stock_kind"]) if stock_rows else ""
    entity_id = str(stock_rows[0]["entity_id"]) if stock_rows else ""
    location_rows = fetched[3]
    path = location_rows[0]["path"] if location_rows else None

    status = str(row["status"])
    in_stock = False
    if stock_kind:
        status_set = aliquot_status_set(ledger, stock_kind)
        in_stock = status_is_in_stock(status_set, status)

    return {
        "id": str(row["id"]),
        "stock_id": str(row["stock_id"]),
        "seq": int(row["seq"]),
        "status": status,
        "quantity": row["quantity"],
        "unit": row["unit"],
        "label": row["label"],
        "notes": row["notes"],
        "created_at": row["created_at"],
        "aliases": [str(alias["alias"]) for alias in fetched[1]],
        "stock_kind": stock_kind,
        "entity_id": entity_id,
        "box_id": row["box_id"],
        "slot": row["slot"],
        "path": path,
        "in_stock": in_stock,
        "object": "aliquot",
    }


# ---------------------------------------------------------------------------
# Aliquot listing
# ---------------------------------------------------------------------------


def _in_stock_scope_sql(ledger: Ledger) -> tuple[str, list[Any]]:
    """SQL for "this tube's status counts as in stock", plus its parameters.

    The in-stock statuses live on each stock kind's status set, so one OR
    branch per *distinct* set (the bundled sets are shared by several kinds)
    keeps the statement short.  A kind without an in-stock status gets no
    branch: no tube of it can match the positive scope, which is exactly
    right, and the negated scope then matches all of its tubes.
    """
    groups: dict[tuple[str, ...], list[str]] = {}
    for kind_name in sorted(ledger.schema.kinds_for("stock")):
        names = tuple(aliquot_status_set(ledger, kind_name).in_stock_names)
        if names:
            groups.setdefault(names, []).append(kind_name)
    if not groups:
        # A ledger whose stock kinds have no in-stock status at all (a custom
        # preset): the positive scope matches nothing and the negated one
        # matches every tube.
        return "FALSE", []
    parts: list[str] = []
    params: list[Any] = []
    for names, kinds in groups.items():
        kind_slots = ", ".join(["%s"] * len(kinds))
        status_slots = ", ".join(["%s"] * len(names))
        parts.append(
            f"(s.{_quote('kind')} IN ({kind_slots}) "
            f"AND a.{_quote('status')} IN ({status_slots}))"
        )
        params.extend(kinds)
        params.extend(names)
    return "(" + " OR ".join(parts) + ")", params


def _check_status_filter(
    ledger: Ledger,
    statuses: Sequence[str],
    *,
    kind_name: str | None,
    stock_label: str | None,
    stock_row: Mapping[str, Any] | None,
) -> None:
    """Reject a status the pinned stock kind does not define.

    With a stock or a stock kind pinned, the status must be in *that* kind's
    set.  Without one it only has to belong to some stock kind's set: a typo
    guard that still lets each row's own kind decide whether it is in stock.
    """
    if stock_row is not None:
        status_set = aliquot_status_set(ledger, str(stock_row["kind"]))
        label = f"stock {stock_label!r}"
    elif kind_name is not None:
        status_set = aliquot_status_set(ledger, kind_name)
        label = f"stock kind {kind_name!r}"
    else:
        known = {
            name
            for current in ledger.schema.kinds_for("stock")
            for name in aliquot_status_set(ledger, current).names
        }
        for status in statuses:
            if status not in known:
                raise UsageError(
                    f"unknown status {status!r}; choose from: "
                    f"{', '.join(sorted(known))}"
                )
        return
    for status in statuses:
        if not status_set.has(status):
            raise UsageError(
                f"unknown status {status!r} for {label}; choose from: "
                f"{', '.join(status_set.names)}"
            )


def _resolve_stock_filter(ledger: Ledger, ref: str) -> Mapping[str, Any]:
    """Resolve the ``STOCK`` argument of ``list_aliquots`` (with its hint).

    The not-found message names ``tm aliquot show`` when the ref is actually a
    tube, the same hint ``show_stock`` gives; an ambiguous prefix keeps its
    usage error.
    """
    try:
        return _resolve_core(ledger.driver, "stock", ref)
    except UsageError:
        raise
    except LedgerError as error:
        hint = _wrong_type_hint(ledger, "stock", ref)
        if not hint:
            raise
        raise LedgerError(f"{error}{hint}") from error


def list_aliquots(
    ledger: Ledger,
    stock: str | None = None,
    *,
    kind_name: str | None = None,
    box: str | None = None,
    statuses: str | Sequence[str] = (),
    in_stock: bool | None = None,
    all_statuses: bool = False,
    where: Iterable[str] | Mapping[str, Any] | None = (),
) -> list[dict[str, Any]]:
    """List tubes; by default only the ones that are physically in stock.

    A list answers the question a session starts with - which tubes can an
    experiment use - so tubes that left stock (consumed, missing, thawed, ...)
    are hidden unless asked for.  ``all_statuses`` shows every status and
    ``in_stock=False`` shows only the complement; an explicit ``statuses``
    filter turns the implicit in-stock scope off, because naming a status is
    already a deliberate choice.  Everything ANDs together:

    * ``stock``: one stock id or alias (the unique-prefix rule applies);
    * ``kind_name``: one stock kind (an aliquot has no kind of its own);
    * ``box``: a box name/id/alias plus every box nested inside it;
    * ``statuses``: the exact statuses to keep (an empty sequence leaves the
      default in-stock scope in charge);
    * ``where``: exact matches on the aliquot core columns except ``status``
      (that is what ``statuses`` is for).

    The records have exactly the same shape as :func:`show_aliquot` - the
    same keys, including the sorted ``aliases`` and the derived ``in_stock`` -
    so a list row and a single tube can be handled by the same code.  Rows are
    ordered by ``stock_id`` and then numeric ``seq``.  The rows and their
    aliases are read in one batched driver round trip.
    """
    if all_statuses and in_stock is not None:
        raise UsageError(
            "--all and --not-in-stock cannot be combined (all_statuses "
            "conflicts with an explicit in_stock filter)"
        )
    # An empty ref is not a prefix of every id: the unique-prefix branch of
    # the SQL resolver would match the ledger's only stock/box, silently
    # answering a different question.  The show readers guard the same way.
    if stock is not None and not str(stock):
        raise LedgerError(f"unknown stock {stock!r}")
    if box is not None and not str(box):
        raise LedgerError(f"unknown box {box!r}")
    wanted = (
        [statuses]
        if isinstance(statuses, str)
        else [str(status) for status in statuses]
    )
    normalised = tuple(dict.fromkeys(status.strip() for status in wanted))
    if any(not status for status in normalised):
        raise UsageError("a --status value must not be empty")

    filters = _parse_pairs(where, option="--where")
    if "status" in filters:
        raise UsageError(
            "filter the status with --status, not --where (the default scope "
            "is in stock)"
        )
    unknown = sorted(set(filters) - _CORE_FILTERS["aliquot"])
    if unknown:
        raise UsageError(
            f"unknown field(s) {', '.join(repr(name) for name in unknown)} in "
            "--where; valid fields: "
            + ", ".join(sorted(_CORE_FILTERS["aliquot"]))
        )

    if kind_name is not None:
        _kind_for(ledger, "stock", kind_name)

    stock_label = None if stock is None else str(stock)
    stock_row: Mapping[str, Any] | None = None
    if stock_label is not None and normalised:
        # The status vocabulary is the stock kind's, so pin it before the main
        # read when both filters are given.
        stock_row = _resolve_stock_filter(ledger, stock_label)
    if normalised:
        _check_status_filter(
            ledger,
            normalised,
            kind_name=kind_name,
            stock_label=stock_label,
            stock_row=stock_row,
        )

    if all_statuses:
        scope: str | None = None
    elif in_stock is False:
        scope = "not_in_stock"
    elif in_stock is True:
        scope = "in_stock"
    elif normalised:
        scope = None
    else:
        scope = "in_stock"

    conditions: list[str] = []
    params: list[Any] = []

    box_join = ""
    if box is not None:
        box_id_sql = _resolved_id_sql("box", by_name=True)
        params.extend(_resolved_id_params(box_id_sql, str(box)))
        box_join = (
            "JOIN (WITH RECURSIVE `box_tree` AS ("
            f"SELECT {_quote('id')} FROM {_quote('box')} "
            f"WHERE {_quote('id')} = {box_id_sql} "
            "UNION ALL "
            f"SELECT child.{_quote('id')} FROM {_quote('box')} child "
            f"JOIN `box_tree` parent "
            f"ON child.{_quote('parent_box_id')} = parent.{_quote('id')}"
            f") SELECT {_quote('id')} FROM `box_tree`) AS `bt` "
            f"ON `bt`.{_quote('id')} = a.{_quote('box_id')} "
        )

    if stock_label is not None:
        stock_id_sql = _resolved_id_sql("stock")
        conditions.append(f"a.{_quote('stock_id')} = ({stock_id_sql})")
        params.extend(_resolved_id_params(stock_id_sql, stock_label))
    if kind_name is not None:
        conditions.append(f"s.{_quote('kind')} = %s")
        params.append(kind_name)
    if normalised:
        status_slots = ", ".join(["%s"] * len(normalised))
        conditions.append(f"a.{_quote('status')} IN ({status_slots})")
        params.extend(normalised)
    for field, value in filters.items():
        conditions.append(f"a.{_quote(field)} = %s")
        params.append(value)
    if scope is not None:
        scope_sql, scope_params = _in_stock_scope_sql(ledger)
        if scope == "not_in_stock":
            scope_sql = f"NOT ({scope_sql})"
        conditions.append(scope_sql)
        params.extend(scope_params)

    where_sql = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    from_sql = (
        f"FROM {_quote('aliquot')} a "
        f"JOIN {_quote('stock')} s ON s.{_quote('id')} = a.{_quote('stock_id')} "
        + box_join
    )
    main_sql = (
        "SELECT "
        f"a.{_quote('id')}, a.{_quote('stock_id')}, a.{_quote('seq')}, "
        f"a.{_quote('box_id')}, a.{_quote('slot')}, a.{_quote('status')}, "
        f"a.{_quote('quantity')}, a.{_quote('unit')}, a.{_quote('label')}, "
        f"a.{_quote('created_at')}, a.{_quote('notes')}, "
        f"s.{_quote('kind')} AS {_quote('stock_kind')}, "
        f"s.{_quote('entity_id')} AS {_quote('entity_id')}, "
        f"COALESCE(l.{_quote('location')}, l.{_quote('box_path')}) "
        f"AS {_quote('path')} "
        + from_sql
        + f"LEFT JOIN {_quote('v_aliquot_location')} l "
        f"ON l.{_quote('aliquot_id')} = a.{_quote('id')} "
        + where_sql
        + f" ORDER BY a.{_quote('stock_id')}, a.{_quote('seq')}, a.{_quote('id')}"
    )
    # The aliases ride in the same batch: the correlated subquery repeats the
    # same FROM/WHERE so the two statements take identical parameters.
    alias_sql = (
        f"SELECT al.{_quote('object_id')} AS {_quote('object_id')}, "
        f"al.{_quote('alias')} AS {_quote('alias')} "
        f"FROM {_quote('alias')} al "
        f"WHERE al.{_quote('object_type')} = 'aliquot' "
        f"AND al.{_quote('object_id')} IN (SELECT a.{_quote('id')} "
        + from_sql
        + where_sql
        + f") ORDER BY al.{_quote('object_id')}, al.{_quote('alias')}"
    )
    fetched = ledger.driver.query_many(
        [(main_sql, tuple(params)), (alias_sql, tuple(params))]
    )
    rows, alias_rows = fetched[0], fetched[1]

    if not rows:
        # An empty result could mean "no tube matches" or a mistyped filter;
        # resolving only on the empty path keeps the happy path at one process.
        if stock_label is not None:
            _resolve_stock_filter(ledger, stock_label)
        if box is not None:
            _resolve_box(ledger.driver, str(box))

    aliases: dict[str, list[str]] = {}
    for row in alias_rows:
        aliases.setdefault(str(row["object_id"]), []).append(str(row["alias"]))

    status_sets: dict[str, StatusSet] = {}
    records: list[dict[str, Any]] = []
    for row in rows:
        stock_kind = str(row["stock_kind"])
        status = str(row["status"])
        status_set = status_sets.get(stock_kind)
        if status_set is None:
            status_set = aliquot_status_set(ledger, stock_kind)
            status_sets[stock_kind] = status_set
        records.append(
            {
                "id": str(row["id"]),
                "stock_id": str(row["stock_id"]),
                "seq": int(row["seq"]),
                "status": status,
                "quantity": row["quantity"],
                "unit": row["unit"],
                "label": row["label"],
                "notes": row["notes"],
                "created_at": row["created_at"],
                "aliases": aliases.get(str(row["id"]), []),
                "stock_kind": stock_kind,
                "entity_id": str(row["entity_id"]),
                "box_id": row["box_id"],
                "slot": row["slot"],
                "path": row["path"],
                "in_stock": status_is_in_stock(status_set, status),
                "object": "aliquot",
            }
        )
    return records


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
    """Create a box, its ``slot`` rows and its generated view in one commit."""
    kind = _kind_for(ledger, "box", kind_name)
    box_name = str(name).strip()
    if not box_name:
        raise UsageError("a box needs a non-empty name")
    if parent is None and slot is not None:
        raise UsageError("--slot requires --in PARENT")
    box_id = str(uuid.uuid4())
    geometry = _box_geometry(kind)

    with ledger.write(f"{kind_name} {box_id}", verb="add", object="box") as session:
        _check_box_name(session, box_name)
        duplicate = session.query(
            f"SELECT {_quote('id')} FROM {_quote('box')} WHERE {_quote('name')} = %s",
            (box_name,),
        )
        if duplicate:
            raise LedgerError(f"box name {box_name!r} is already in use")
        parent_row = _resolve_box(session, parent) if parent is not None else None
        parent_slot: str | None = None
        if parent_row is not None:
            _refuse_box_inside_grid(ledger, parent_row)
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
        # Spec 5: every box has its own view; create it in this same write.
        _create_box_views(
            session,
            ledger,
            [{"id": box_id, "name": box_name, "kind": kind_name}],
        )

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
    """Move an existing box inside ``parent`` (acyclic, exclusive slot).

    A grid box holds tubes only (spec 5), so it cannot become a parent; a box
    that was created in a grid box by a spec-4 ledger can still be moved out.
    """
    existing = _resolve_box(ledger.driver, ref)
    box_id = str(existing["id"])

    with ledger.write(box_id, verb="move", object="box") as session:
        row = _fetch_core(session, "box", box_id)
        if row is None:  # pragma: no cover - just resolved
            raise LedgerError(f"unknown box {ref!r}")
        parent_row = _resolve_box(session, parent)
        _refuse_box_inside_grid(ledger, parent_row)
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


def rename_box(ledger: Ledger, ref: str, new_name: str) -> dict[str, Any]:
    """Give a box a new name, keep the old one as an alias, rebuild its view.

    The new name follows the view-safe name rule (spec 5); a case-only change
    of the box's own name is allowed.  ``v_box_<old>`` is dropped and
    ``v_box_<new>`` created in the same commit as the row change and the
    old-name alias, so ``tm check`` never sees a stale view.
    """
    name = _clean_new_name(new_name)
    existing = _resolve_box(ledger.driver, ref)
    box_id = str(existing["id"])
    kind_name = str(existing["kind"])
    old_name = str(existing["name"])
    if old_name == name:
        raise UsageError(f"box {box_id!r} is already named {name!r}")

    with ledger.write(
        _rename_summary(box_id, old_name, name), verb="rename", object="box"
    ) as session:
        others = [
            str(row["name"])
            for row in session.query(
                f"SELECT {_quote('name')} FROM {_quote('box')} "
                f"WHERE {_quote('id')} <> %s",
                (box_id,),
            )
        ]
        problem = box_name_problem(name, others)
        if problem is not None:
            raise UsageError(problem)
        if name in others:
            raise LedgerError(f"box name {name!r} is already in use")
        alias_owner = session.query(
            f"SELECT {_quote('object_id')} FROM {_quote('alias')} "
            f"WHERE {_quote('object_type')} = 'box' AND {_quote('alias')} = %s "
            f"AND {_quote('object_id')} <> %s",
            (name, box_id),
        )
        if alias_owner:
            raise LedgerError(
                f"box name {name!r} is already an alias of box "
                f"{alias_owner[0]['object_id']!r}"
            )
        _update(session, "box", {"id": box_id}, {"name": name})
        if "name" in ledger.schema.kind(kind_name).fields:
            _update(
                session, table_name("box", kind_name), {"id": box_id}, {"name": name}
            )
        session.execute(f"DROP VIEW IF EXISTS {_quote(box_view_name(old_name))}")
        _create_box_views(
            session, ledger, [{"id": box_id, "name": name, "kind": kind_name}]
        )
        _keep_old_name_alias(session, "box", box_id, old_name)
        aliases = _object_aliases(session, "box", box_id)

    if old_name not in aliases:
        aliases = sorted({*aliases, old_name})  # dry run
    return {
        "object": "box",
        "kind": kind_name,
        "id": box_id,
        "name": name,
        "old_name": old_name,
        "aliases": aliases,
    }


def list_boxes(
    ledger: Ledger,
    kind_name: str | None = None,
    *,
    where: Iterable[str] | Mapping[str, Any] | None = (),
) -> list[dict[str, Any]]:
    # The path and occupancy rows ride in the same batch as the per-kind
    # listings, so neither costs an extra round trip.
    records, extra = _list_records(
        ledger,
        "box",
        kind_name,
        where,
        extra=[(_BOX_PATH_ROWS_SQL, None), (_BOX_OCCUPIED_SQL, None)],
    )
    paths = _box_paths(extra[0])
    occupied = {str(row["box_id"]): row["occupied"] for row in extra[1]}
    for record in records:
        kind = ledger.schema.kind(str(record["kind"]))
        geometry = _box_geometry(kind)
        capacity = (
            len(geometry.labels) * kind.slot_capacity
            if geometry.is_slotted
            else geometry.capacity
        )
        record["path"] = paths.get(str(record["id"]), str(record.get("name")))
        # The server driver may return the SUM as a float; the count is an int.
        record["occupied"] = int(occupied.get(str(record["id"]), 0))
        record["capacity"] = capacity
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
    resolved = _resolve_core_with_hint(ledger.driver, checked_type, str(object_id))
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
    ``type: ref`` fields on kind tables, whose foreign keys also point at the
    renamed primary keys.
    """
    stock_columns: list[tuple[str, str]] = []
    aliquot_columns: list[tuple[str, str]] = []
    for kind_name in sorted(ledger.schema.kinds):
        kind = ledger.schema.kinds[kind_name]
        scopes: list[tuple[str, Mapping[str, Field]]] = [
            (table_name(kind.object.value, kind_name), kind.fields)
        ]
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

    def rename(self, ref: str, new_name: str) -> dict[str, Any]:
        return rename_entity(self.ledger, ref, new_name)

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
        id: str | None = None,
        status: str | None = None,
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
            id=id,
            status=status,
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
        status: str | None = None,
    ) -> dict[str, Any]:
        return add_aliquots(
            self.ledger,
            stock,
            count=count,
            box=box,
            slots=slots,
            quantity=quantity,
            unit=unit,
            status=status,
        )

    def show(self, ref: str) -> dict[str, Any]:
        return show_aliquot(self.ledger, ref)

    def ls(
        self,
        stock: str | None = None,
        *,
        kind: str | None = None,
        box: str | None = None,
        status: str | Sequence[str] = (),
        in_stock: bool | None = None,
        all_statuses: bool = False,
        where: Iterable[str] | Mapping[str, Any] | None = (),
    ) -> list[dict[str, Any]]:
        return list_aliquots(
            self.ledger,
            stock,
            kind_name=kind,
            box=box,
            statuses=status,
            in_stock=in_stock,
            all_statuses=all_statuses,
            where=where,
        )

    def move(
        self,
        ref: str,
        *,
        box: str,
        slot: str | None = None,
        status: str | None = None,
        message: str,
    ) -> dict[str, Any]:
        return move_aliquot(
            self.ledger, ref, box=box, slot=slot, status=status, message=message
        )

    def use(
        self,
        ref: str,
        amount: Any = None,
        unit: str | None = None,
        *,
        message: str,
    ) -> dict[str, Any]:
        return use_aliquot(self.ledger, ref, amount, unit, message=message)

    def status(
        self,
        ref: str,
        status: str,
        *,
        note: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        return set_aliquot_status(
            self.ledger, ref, status, note=note, force=force
        )

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

    def rename(self, ref: str, new_name: str) -> dict[str, Any]:
        return rename_box(self.ledger, ref, new_name)

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
