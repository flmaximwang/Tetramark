"""CSV import and export (spec §6, ticket 12).

One CSV file describes one kind (``ledger import KIND FILE.csv``) or, with a
``kind`` column, a mixture of kinds (``boxes.csv``, ``aliquots.csv``).  The
header carries the core columns plus the kind's own fields:

* ``entity`` — ``id``, ``kind``, ``name``, ``created_at``, ``notes``, the kind
  fields, ``alias``;
* ``stock`` — ``id``, ``kind``, ``entity``, ``parent_stock``,
  ``source_aliquot``, ``created_at``, ``notes``, ``aliquots``, the kind
  fields, ``alias``; optionally ``box``, ``slot``, ``quantity``, ``unit`` and
  ``status`` to create and place aliquots in the same row;
* ``box`` — ``id``, ``kind``, ``name``, ``parent``, ``parent_slot``,
  ``created_at``, ``notes``, the kind fields, ``alias``;
* ``aliquot`` — ``id``, ``kind``, ``stock``, ``seq``, ``box``, ``slot``,
  ``status``, ``quantity``, ``unit``, ``created_at``, ``notes``, the stock
  kind's aliquot fields, ``alias``;
* ``alias`` — ``object_type``, ``object_id``, ``alias``.

Aliases use a ``;``-separated ``alias`` column (and ``aliases.csv`` is also
exported, so an import is idempotent when the alias already points at the same
object).  ``slot`` on a stock/aliquot row may list several slots separated by
``;`` to match several aliquots.

The whole file is *planned* before anything is written: every row is validated
with the same helpers as the object commands (ticket 10), every problem is
collected with its row number, and only a clean file is written — in one
transaction and one Dolt commit.  ``export`` writes CSVs that round-trip
through ``import``, including ids, aliases, placement, status, quantity and
timestamps.
"""

from __future__ import annotations

import csv
import datetime as _dt
import hashlib
import re
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

from doltledger.core.events import record_event
from doltledger.core.ids import next_aliquot_id, parse_aliquot_id
from doltledger.core.objects import (
    _CORE_EDITABLE,
    _box_geometry,
    _coerce_field,
    _insert,
    _mirror_name,
    _parse_quantity_unit,
    _require_fields,
    _update,
)
from doltledger.errors import LedgerError, UsageError
from doltledger.schema.ddl import slot_rows, table_name
from doltledger.schema.model import Field, FieldType, Kind, ObjectType

if TYPE_CHECKING:  # pragma: no cover - imported for type checkers only
    from doltledger.core.ledger import Ledger

__all__ = [
    "CsvImportError",
    "ExportResult",
    "ImportResult",
    "RowError",
    "export_csv",
    "import_csv",
]

#: Core object types a CSV can describe.
_OBJECT_TYPES: tuple[str, ...] = ("entity", "stock", "aliquot", "box", "alias")

#: Columns every import/export of an object understands (in export order).
_CORE_COLUMNS: dict[str, tuple[str, ...]] = {
    "entity": ("id", "kind", "name", "created_at", "notes"),
    "stock": (
        "id",
        "kind",
        "entity",
        "parent_stock",
        "source_aliquot",
        "created_at",
        "notes",
        "aliquots",
    ),
    "box": ("id", "kind", "name", "parent", "parent_slot", "created_at", "notes"),
    "aliquot": (
        "id",
        "kind",
        "stock",
        "seq",
        "box",
        "slot",
        "status",
        "quantity",
        "unit",
        "created_at",
        "notes",
    ),
    "alias": ("object_type", "object_id", "alias"),
}

#: Extra optional stock placement columns (spec/ticket 12).
_STOCK_PLACEMENT = frozenset(
    {"box", "slot", "quantity", "unit", "status", "aliquots"}
)

_INT_RE = re.compile(r"[+-]?\d+")
_ALIAS_SEPARATOR = ";"


# ---------------------------------------------------------------------------
# Result and error values
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RowError:
    """One rejected row (``source`` is set when several files are imported)."""

    row: int
    message: str
    source: str | None = None

    def __str__(self) -> str:
        prefix = f"{self.source}: " if self.source else ""
        return f"{prefix}row {self.row}: {self.message}"


class CsvImportError(LedgerError):
    """Every row is validated first; this carries all collected problems."""

    def __init__(
        self,
        errors: Iterable[RowError],
        *,
        source: str | None = None,
        nothing_written: bool = True,
    ) -> None:
        self.errors: tuple[RowError, ...] = tuple(errors)
        where = f" of {source}" if source else ""
        suffix = "; nothing was written" if nothing_written else ""
        lines = [
            f"import{where} failed with {len(self.errors)} error(s){suffix}:",
            *(f"  {error}" for error in self.errors),
        ]
        super().__init__("\n".join(lines))


@dataclass(frozen=True)
class ImportResult:
    """What an import did (or would do with ``dry_run``)."""

    object: str
    kind: str | None
    files: tuple[Path, ...]
    rows: int
    counts: dict[str, int]
    dry_run: bool = False
    sql: str = ""
    commit_hash: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "object": self.object,
            "kind": self.kind,
            "files": [str(path) for path in self.files],
            "rows": self.rows,
            "counts": dict(self.counts),
            "dry_run": self.dry_run,
            "sql": self.sql,
            "commit_hash": self.commit_hash,
        }


@dataclass(frozen=True)
class ExportResult:
    """The files an export wrote, and how many rows each holds."""

    directory: Path
    files: tuple[Path, ...]
    counts: dict[str, int]

    def as_dict(self) -> dict[str, Any]:
        return {
            "directory": str(self.directory),
            "files": [str(path) for path in self.files],
            "counts": dict(self.counts),
        }


@dataclass
class _RawRow:
    """One CSV data row with the identity resolved during the pre-pass."""

    number: int
    data: dict[str, str]
    kind: str | None = None
    id: str | None = None
    stock_id: str | None = None
    seq: int | None = None
    #: ``(alias, should_insert)`` — an already-correct alias is skipped.
    aliases: list[tuple[str, bool]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class _PreparedFile:
    """A CSV read and identity-reserved, waiting to be planned."""

    object_type: str
    kind_name: str | None
    path: Path
    prepared: list[_RawRow] = field(default_factory=list)


@dataclass
class _RowPlan:
    """One validated row, reduced to the exact inserts it needs."""

    number: int
    object_type: str
    kind: str | None
    key: tuple[str, str]
    inserts: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    depends_on: set[tuple[str, str]] = field(default_factory=set)
    #: ``(table, key, payload)`` updates run after every row is inserted.  A
    #: stock's ``source_aliquot`` may point at an aliquot created later in the
    #: same import, so its FK cannot be satisfied at insert time.
    deferred_updates: list[tuple[str, dict[str, Any], dict[str, Any]]] = field(
        default_factory=list
    )


# ---------------------------------------------------------------------------
# Small value helpers
# ---------------------------------------------------------------------------


def _quote(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


def _text(data: Mapping[str, str], key: str) -> str | None:
    """A structural cell: trimmed, empty → ``None``."""
    value = data.get(key)
    if value is None:
        return None
    text = value.strip()
    return text or None


def _raw(data: Mapping[str, str], key: str) -> str | None:
    """A free-text cell: preserved, empty → ``None``."""
    value = data.get(key)
    if value is None or value == "":
        return None
    return value


def _split_aliases(value: str | None) -> list[str]:
    if value is None:
        return []
    result: list[str] = []
    for item in value.split(_ALIAS_SEPARATOR):
        text = item.strip()
        if text and text not in result:
            result.append(text)
    return result


def _split_list(value: str | None) -> list[str]:
    """Split a ``;``-separated cell (slots), keeping positions."""
    if value is None:
        return []
    return [item.strip() for item in value.split(_ALIAS_SEPARATOR)]


def _format_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, _dt.datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, _dt.date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()
    return str(value)


def _parse_created_at(value: str | None, *, label: str = "created_at") -> _dt.datetime | None:
    if not value:
        return None
    text = value.strip().replace("T", " ")
    try:
        return _dt.datetime.fromisoformat(text)
    except ValueError:
        raise UsageError(
            f"{label} must be an ISO datetime like 2026-06-24 12:34:56 (got {value!r})"
        ) from None


def _parse_positive_int(value: str | None, *, label: str, allow_zero: bool) -> int | None:
    if value is None:
        return None
    text = value.strip()
    if not _INT_RE.fullmatch(text):
        raise UsageError(f"{label} must be an integer (got {value!r})")
    number = int(text)
    minimum = 0 if allow_zero else 1
    if number < minimum:
        raise UsageError(f"{label} must be at least {minimum} (got {value!r})")
    return number


def _unique_value(field_schema: Field, value: Any) -> Any:
    """The value a UNIQUE constraint compares (sequence: its sha256).

    Dolt's JSON output returns DECIMAL and date/datetime columns as strings,
    while the planner has ``Decimal``/``date`` objects; normalise both sides so
    duplicates are caught before the database sees them.
    """
    if value is None:
        return None
    if field_schema.type is FieldType.sequence:
        return hashlib.sha256(str(value).encode("utf-8")).hexdigest()
    if field_schema.type is FieldType.decimal:
        try:
            return str(Decimal(str(value)))
        except Exception:  # pragma: no cover - coercion already validated
            return str(value)
    if field_schema.type is FieldType.date:
        if isinstance(value, _dt.datetime):
            return value.date().isoformat()
        if isinstance(value, _dt.date):
            return value.isoformat()
        return str(value)
    if field_schema.type is FieldType.datetime:
        if isinstance(value, _dt.datetime):
            return value.isoformat(sep=" ")
        return str(value)
    if field_schema.type is FieldType.bool:
        return bool(value) if not isinstance(value, str) else value.strip().lower() in (
            "1",
            "true",
            "t",
            "yes",
            "y",
            "on",
        )
    if field_schema.type is FieldType.int:
        try:
            return int(value)
        except (TypeError, ValueError):  # pragma: no cover - coercion validated
            return value
    return str(value) if not isinstance(value, (int, float)) else value


def _dedupe(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


# ---------------------------------------------------------------------------
# Import planning
# ---------------------------------------------------------------------------


class _Planner:
    """Validate rows against the live ledger and reduce them to inserts.

    A planner is stateful so ``import all`` can plan several files and still
    resolve references between them (an entity file before a stock file, a box
    file before an aliquot file).  Nothing here writes to the database.
    """

    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger
        self.driver = ledger.driver
        self.schema = ledger.schema
        self.plans: list[_RowPlan] = []
        self.errors: list[RowError] = []
        self._files: list[Path] = []
        #: ``(row number, aliquot id, source file)`` checked once all files
        #: have been planned.
        self._deferred_sources: list[tuple[int, str, str]] = []

        # Identity registries (existing rows + rows planned by this import).
        self._entity_kinds: dict[str, str] = {}
        self._stock_kinds: dict[str, str] = {}
        self._box_kinds: dict[str, str] = {}
        self._box_names: dict[str, str] = {}
        self._aliquot_ids: set[str] = set()
        self._aliquot_seqs: dict[str, set[int]] = {}
        self._aliases: dict[tuple[str, str], str] = {}
        self._box_parents: dict[str, str | None] = {}
        self._load_identity()

        # Placement state.
        self._slot_labels: set[tuple[str, str]] = set()
        self._slot_counts: dict[tuple[str, str], int] = {}
        self._child_slots: set[tuple[str, str]] = set()
        self._box_occupancy: dict[str, int] = {}
        self._load_placement()

        # Unique values already present (existing rows only; planned rows are
        # added after a row validates).
        self._unique_seen: dict[tuple[str, str], set[Any]] = {}
        self._load_unique_values()

    # -- snapshot of the live ledger --------------------------------------
    def _load_identity(self) -> None:
        for row in self.driver.query("SELECT `id`, `kind` FROM `entity`"):
            self._entity_kinds[str(row["id"])] = str(row["kind"])
        for row in self.driver.query("SELECT `id`, `kind` FROM `stock`"):
            self._stock_kinds[str(row["id"])] = str(row["kind"])
        for row in self.driver.query("SELECT `id`, `name`, `kind` FROM `box`"):
            box_id = str(row["id"])
            self._box_kinds[box_id] = str(row["kind"])
            self._box_names[str(row["name"])] = box_id
        for row in self.driver.query("SELECT `stock_id`, `seq` FROM `aliquot`"):
            stock_id = str(row["stock_id"])
            seq = int(row["seq"])
            self._aliquot_ids.add(f"{stock_id}.{seq}")
            self._aliquot_seqs.setdefault(stock_id, set()).add(seq)
        for row in self.driver.query(
            "SELECT `object_type`, `object_id`, `alias` FROM `alias`"
        ):
            self._aliases[
                (str(row["object_type"]), str(row["alias"]))
            ] = str(row["object_id"])
        for row in self.driver.query(
            "SELECT `id`, `parent_box_id` FROM `box`"
        ):
            self._box_parents[str(row["id"])] = (
                str(row["parent_box_id"])
                if row.get("parent_box_id") is not None
                else None
            )

    def _load_placement(self) -> None:
        for row in self.driver.query("SELECT `box_id`, `label` FROM `slot`"):
            self._slot_labels.add((str(row["box_id"]), str(row["label"])))
        for row in self.driver.query(
            "SELECT `box_id`, `slot`, COUNT(*) AS `n` FROM `aliquot` "
            "WHERE `box_id` IS NOT NULL AND `slot` IS NOT NULL "
            "GROUP BY `box_id`, `slot`"
        ):
            self._slot_counts[
                (str(row["box_id"]), str(row["slot"]))
            ] = int(row["n"])
        for row in self.driver.query(
            "SELECT `parent_box_id`, `parent_slot` FROM `box` "
            "WHERE `parent_box_id` IS NOT NULL AND `parent_slot` IS NOT NULL"
        ):
            self._child_slots.add(
                (str(row["parent_box_id"]), str(row["parent_slot"]))
            )
        for row in self.driver.query(
            "SELECT `box_id`, COUNT(*) AS `n` FROM `aliquot` "
            "WHERE `box_id` IS NOT NULL GROUP BY `box_id`"
        ):
            self._box_occupancy[str(row["box_id"])] = int(row["n"])
        for row in self.driver.query(
            "SELECT `parent_box_id`, COUNT(*) AS `n` FROM `box` "
            "WHERE `parent_box_id` IS NOT NULL GROUP BY `parent_box_id`"
        ):
            box_id = str(row["parent_box_id"])
            self._box_occupancy[box_id] = (
                self._box_occupancy.get(box_id, 0) + int(row["n"])
            )

    def _load_unique_values(self) -> None:
        for object_type in ("entity", "stock", "box"):
            for kind_name, kind in self.schema.kinds_for(object_type).items():
                table = table_name(object_type, kind_name)
                for field_name, field_schema in kind.fields.items():
                    if not field_schema.unique:
                        continue
                    column = (
                        f"{field_name}_sha256"
                        if field_schema.type is FieldType.sequence
                        else field_name
                    )
                    try:
                        rows = self.driver.query(
                            f"SELECT {_quote(column)} AS `v` FROM {_quote(table)}"
                        )
                    except LedgerError:  # pragma: no cover - schema/DB mismatch
                        continue
                    self._unique_seen[(kind_name, field_name)] = {
                        (
                            row["v"]
                            if field_schema.type is FieldType.sequence
                            else _unique_value(field_schema, row["v"])
                        )
                        for row in rows
                        if row.get("v") is not None
                    }
                if object_type != "stock" or not kind.aliquot_fields:
                    continue
                extension = table_name("aliquot", kind_name)
                for field_name, field_schema in kind.aliquot_fields.items():
                    if not field_schema.unique:
                        continue
                    column = (
                        f"{field_name}_sha256"
                        if field_schema.type is FieldType.sequence
                        else field_name
                    )
                    try:
                        rows = self.driver.query(
                            f"SELECT {_quote(column)} AS `v` FROM {_quote(extension)}"
                        )
                    except LedgerError:  # pragma: no cover - schema/DB mismatch
                        continue
                    self._unique_seen[(f"aliquot:{kind_name}", field_name)] = {
                        (
                            row["v"]
                            if field_schema.type is FieldType.sequence
                            else _unique_value(field_schema, row["v"])
                        )
                        for row in rows
                        if row.get("v") is not None
                    }

    # -- public planning entry points -------------------------------------
    def add_file(
        self, object_type: str, kind_name: str | None, path: Path
    ) -> int:
        """Plan every row of one CSV; return the number of data rows read."""
        prepared = self.prepare_file(object_type, kind_name, path)
        self.plan_file(prepared)
        return len(prepared.prepared)

    def prepare_file(
        self, object_type: str, kind_name: str | None, path: Path
    ) -> _PreparedFile:
        """Read a CSV and reserve its ids/names/aliases without planning rows.

        ``import all`` prepares every file first, so a reference to a kind
        exported to a later file still resolves when the rows are planned.
        """
        header, rows = _read_csv(path, object_type)
        self._files.append(path)
        self._current_file_name = path.name
        self._check_header(object_type, kind_name, header, path)

        prepared: list[_RawRow] = []
        for number, data in rows:
            raw = _RawRow(number=number, data=data)
            try:
                self._prepare(raw, object_type, kind_name)
            except (UsageError, LedgerError) as error:
                raw.errors.append(str(error))
            prepared.append(raw)
        return _PreparedFile(
            object_type=object_type,
            kind_name=kind_name,
            path=path,
            prepared=prepared,
        )

    def plan_file(self, prepared_file: _PreparedFile) -> None:
        """Validate and reduce the rows of one prepared file to plans."""
        self._current_file_name = prepared_file.path.name
        for raw in prepared_file.prepared:
            if raw.errors:
                self.errors.extend(
                    RowError(raw.number, message, prepared_file.path.name)
                    for message in raw.errors
                )
                continue
            try:
                plan = self._plan_row(
                    raw,
                    prepared_file.object_type,
                    prepared_file.kind_name,
                    prepared_file.path,
                )
            except (UsageError, LedgerError) as error:
                self.errors.append(
                    RowError(raw.number, str(error), prepared_file.path.name)
                )
                continue
            if plan is not None:
                self.plans.append(plan)

    def finish(self) -> list[_RowPlan]:
        """Raise collected errors, check box cycles, and order the plans."""
        self._check_deferred_sources()
        self._check_box_cycles()
        if self.errors:
            raise CsvImportError(self.errors)
        return _topological_order(self.plans, self.errors)

    def _check_deferred_sources(self) -> None:
        for row, aliquot_id, source in self._deferred_sources:
            if aliquot_id not in self._aliquot_ids:
                self.errors.append(
                    RowError(
                        row,
                        f"unknown source aliquot {aliquot_id!r}",
                        source or None,
                    )
                )

    # -- pre-pass: identity ------------------------------------------------
    def _prepare(
        self, raw: _RawRow, object_type: str, kind_name: str | None
    ) -> None:
        kind_cell = _text(raw.data, "kind")
        if object_type == "alias":
            return

        if object_type == "aliquot":
            # The kind of an aliquot is the kind of its stock.
            raw.kind = kind_name or kind_cell
            self._prepare_aliquot_identity(raw, kind_name=kind_name)
            return

        if kind_name is not None:
            if kind_cell is not None and kind_cell != kind_name:
                raise UsageError(
                    f"kind {kind_cell!r} does not match the imported kind {kind_name!r}"
                )
            raw.kind = kind_name
        else:
            if kind_cell is None:
                raise UsageError(
                    f"the 'kind' column is required when importing {object_type} "
                    "without a single KIND"
                )
            raw.kind = kind_cell

        kind = self.schema.kinds.get(raw.kind or "")
        if kind is None:
            valid = ", ".join(sorted(self.schema.kinds_for(object_type)))
            raise UsageError(
                f"unknown {object_type} kind {raw.kind!r}; choose from: {valid}"
            )
        if kind.object.value != object_type:
            raise UsageError(
                f"kind {raw.kind!r} is a {kind.object.value} kind, not a "
                f"{object_type} kind"
            )

        if object_type == "entity":
            raw.id = self._prepare_id(raw, "entity")
        elif object_type == "stock":
            raw.id = self._prepare_id(raw, "stock")
        elif object_type == "box":
            raw.id = self._prepare_box_id(raw)
        self._prepare_inline_aliases(raw, object_type, raw.id)

    def _prepare_id(self, raw: _RawRow, object_type: str) -> str:
        registry = (
            self._entity_kinds if object_type == "entity" else self._stock_kinds
        )
        explicit = _text(raw.data, "id")
        if explicit is not None:
            # Import is the migration path (ticket 12): an old ledger's ids are
            # accepted even when the configured strategy would generate
            # something else (e.g. ``0826FP02`` with a uuid stock strategy).
            if len(explicit) > 64:
                raise UsageError("id must be at most 64 characters")
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]*", explicit) is None:
                raise UsageError(
                    f"malformed id {explicit!r}: use letters, digits, '_', '.', "
                    "':' or '-' (starting with a letter or digit)"
                )
            if explicit in registry:
                raise UsageError(f"{object_type} id {explicit!r} already exists")
        else:
            strategy = self.ledger.id_strategy(object_type)
            existing = list(registry)
            explicit = strategy.next_id(
                lambda prefix: [value for value in existing if value.startswith(prefix)],
                _dt.date.today(),
            )
        registry[explicit] = raw.kind or ""
        return explicit

    def _prepare_box_id(self, raw: _RawRow) -> str:
        name = _text(raw.data, "name")
        if name is None:
            raise UsageError("a box row needs a non-empty 'name'")
        if name in self._box_names:
            raise UsageError(f"box name {name!r} is already in use")

        explicit = _text(raw.data, "id")
        if explicit is not None:
            if len(explicit) > 64:
                raise UsageError("id must be at most 64 characters")
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]*", explicit) is None:
                raise UsageError(
                    f"malformed id {explicit!r}: use letters, digits, '_', '.', "
                    "':' or '-' (starting with a letter or digit)"
                )
            if explicit in self._box_kinds:
                raise UsageError(f"box id {explicit!r} already exists")
        else:
            explicit = str(uuid.uuid4())
            while explicit in self._box_kinds:  # pragma: no cover - improbable
                explicit = str(uuid.uuid4())

        self._box_kinds[explicit] = raw.kind or ""
        self._box_names[name] = explicit
        return explicit

    def _prepare_aliquot_identity(
        self, raw: _RawRow, *, kind_name: str | None
    ) -> None:
        explicit_id = _text(raw.data, "id")
        stock_text = _text(raw.data, "stock")
        seq_text = _text(raw.data, "seq")

        stock_id: str | None = None
        seq: int | None = None
        if explicit_id is not None:
            try:
                stock_id, seq = parse_aliquot_id(explicit_id)
            except LedgerError as error:
                raise UsageError(str(error)) from None

        if stock_text is not None:
            resolved = self._resolve_any("stock", stock_text)
            if resolved is None:
                raise UsageError(f"unknown stock {stock_text!r}")
            resolved_id, resolved_kind = resolved
            if stock_id is not None and stock_id != resolved_id:
                raise UsageError(
                    f"aliquot id {explicit_id!r} belongs to stock {stock_id!r}, "
                    f"not {resolved_id!r}"
                )
            stock_id = resolved_id
            if kind_name is not None and resolved_kind != kind_name:
                raise UsageError(
                    f"stock {stock_id!r} is a {resolved_kind!r} stock, "
                    f"not {kind_name!r}"
                )
            if raw.kind is not None and raw.kind != resolved_kind:
                raise UsageError(
                    f"kind {raw.kind!r} does not match stock {stock_id!r} "
                    f"of kind {resolved_kind!r}"
                )
            raw.kind = resolved_kind

        if stock_id is None:
            raise UsageError("an aliquot row needs 'stock' or an '<stock>.<seq>' id")

        resolved_kind = self._stock_kinds.get(stock_id)
        if resolved_kind is None:
            rows = self.driver.query(
                "SELECT `kind` FROM `stock` WHERE `id` = %s", (stock_id,)
            )
            if rows:
                resolved_kind = str(rows[0]["kind"])
        if resolved_kind is None:
            raise UsageError(f"unknown stock {stock_id!r}")
        if kind_name is not None and resolved_kind != kind_name:
            raise UsageError(
                f"stock {stock_id!r} is a {resolved_kind!r} stock, "
                f"not {kind_name!r}"
            )
        if raw.kind is not None and raw.kind != resolved_kind:
            raise UsageError(
                f"kind {raw.kind!r} does not match stock {stock_id!r} "
                f"of kind {resolved_kind!r}"
            )
        raw.kind = resolved_kind

        if seq_text is not None:
            parsed = _parse_positive_int(seq_text, label="seq", allow_zero=False)
            assert parsed is not None
            if seq is not None and seq != parsed:
                raise UsageError(
                    f"seq {parsed} does not match the aliquot id {explicit_id!r}"
                )
            seq = parsed
        if seq is None:
            existing = set(self._aliquot_seqs.get(stock_id, set()))
            next_id = next_aliquot_id(
                stock_id, [f"{stock_id}.{number}" for number in existing]
            )
            seq = parse_aliquot_id(next_id)[1]

        id_text = f"{stock_id}.{seq}"
        if id_text in self._aliquot_ids:
            raise UsageError(f"aliquot {id_text!r} already exists")
        self._aliquot_ids.add(id_text)
        self._aliquot_seqs.setdefault(stock_id, set()).add(seq)
        raw.id = id_text
        raw.stock_id = stock_id
        raw.seq = seq

    def _prepare_inline_aliases(
        self, raw: _RawRow, object_type: str, object_id: str | None
    ) -> None:
        if object_id is None:
            return
        for alias in _split_aliases(_raw(raw.data, "alias")):
            raw.aliases.append(self._register_alias(object_type, object_id, alias))

    def _register_alias(
        self, object_type: str, object_id: str, alias: str
    ) -> tuple[str, bool]:
        if len(alias) > 255:
            raise UsageError("alias must be at most 255 characters")
        key = (object_type, alias)
        owner = self._aliases.get(key)
        if owner is not None:
            if owner == object_id:
                return alias, False
            raise UsageError(
                f"alias {alias!r} is already used by {object_type} {owner!r}"
            )
        self._aliases[key] = object_id
        return alias, True

    # -- resolution (live DB first, then rows planned by this import) ------
    def _resolve_any(
        self, object_type: str, ref: str
    ) -> tuple[str, str] | None:
        """Resolve an id or alias; return ``(id, kind)`` (kind may be "")."""
        if object_type == "aliquot":
            rows = self.driver.query(
                "SELECT `id`, `stock_id` FROM `aliquot` WHERE `id` = %s", (ref,)
            )
            if rows:
                stock_id = str(rows[0]["stock_id"])
                return str(rows[0]["id"]), self._stock_kinds.get(stock_id, "")
            owner = self._aliases.get(("aliquot", ref))
            if owner is not None:
                rows = self.driver.query(
                    "SELECT `stock_id` FROM `aliquot` WHERE `id` = %s", (owner,)
                )
                if rows:
                    stock_id = str(rows[0]["stock_id"])
                    return owner, self._stock_kinds.get(stock_id, "")
                if owner in self._aliquot_ids:
                    stock_id = parse_aliquot_id(owner)[0]
                    return owner, self._stock_kinds.get(stock_id, "")
            if ref in self._aliquot_ids:
                stock_id = parse_aliquot_id(ref)[0]
                return ref, self._stock_kinds.get(stock_id, "")
            return None

        table = object_type
        rows = self.driver.query(
            f"SELECT `id`, `kind` FROM {_quote(table)} WHERE `id` = %s", (ref,)
        )
        if rows:
            return str(rows[0]["id"]), str(rows[0].get("kind") or "")
        if object_type == "box":
            rows = self.driver.query(
                f"SELECT `id`, `kind` FROM `box` WHERE `name` = %s", (ref,)
            )
            if rows:
                return str(rows[0]["id"]), str(rows[0].get("kind") or "")
        owner = self._aliases.get((object_type, ref))
        if owner is not None:
            if object_type == "entity":
                return owner, self._entity_kinds.get(owner, "")
            if object_type == "stock":
                return owner, self._stock_kinds.get(owner, "")
            if object_type == "box":
                return owner, self._box_kinds.get(owner, "")

        if object_type == "entity" and ref in self._entity_kinds:
            return ref, self._entity_kinds[ref]
        if object_type == "stock" and ref in self._stock_kinds:
            return ref, self._stock_kinds[ref]
        if object_type == "box":
            if ref in self._box_kinds:
                return ref, self._box_kinds[ref]
            if ref in self._box_names:
                box_id = self._box_names[ref]
                return box_id, self._box_kinds.get(box_id, "")
        if object_type == "aliquot" and ref in self._aliquot_ids:
            stock_id = parse_aliquot_id(ref)[0]
            return ref, self._stock_kinds.get(stock_id, "")
        return None

    def _resolve_virtual_ref(
        self, target: str, ref: str
    ) -> tuple[str, str] | None:
        """Resolve a ``ref`` field to a row this import will insert."""
        if target in _OBJECT_TYPES:
            resolved = self._resolve_any(target, ref)
            if resolved is None:
                return None
            # Prefer a planned row (the live one was already tried by
            # ``_coerce_field``).
            object_type = target
        else:
            kind = self.schema.kinds.get(target)
            if kind is None:
                return None
            object_type = kind.object.value
            resolved = self._resolve_any(object_type, ref)
            if resolved is None:
                return None
            if resolved[1] != target:
                return None
        if object_type == "entity" and resolved[0] not in self._entity_kinds:
            return None
        if object_type == "stock" and resolved[0] not in self._stock_kinds:
            return None
        if object_type == "box" and resolved[0] not in self._box_kinds:
            return None
        if object_type == "aliquot" and resolved[0] not in self._aliquot_ids:
            return None
        return object_type, resolved[0]

    def _planned_key(
        self, object_type: str, object_id: str
    ) -> tuple[str, str] | None:
        registry = {
            "entity": self._entity_kinds,
            "stock": self._stock_kinds,
            "box": self._box_kinds,
        }.get(object_type)
        if registry is not None and object_id in registry:
            return object_type, object_id
        if object_type == "aliquot" and object_id in self._aliquot_ids:
            return object_type, object_id
        return None

    def _coerce_ref(
        self, field_schema: Field, value: str, *, label: str
    ) -> tuple[str, tuple[str, str] | None]:
        try:
            coerced = _coerce_field(
                field_schema,
                value,
                ledger=self.ledger,
                session=self.driver,
                label=label,
            )
            return coerced, None
        except UsageError:
            assert field_schema.to is not None
            resolved = self._resolve_virtual_ref(field_schema.to, value)
            if resolved is None:
                raise
            object_type, object_id = resolved
            return object_id, self._planned_key(object_type, object_id)

    def _split_kind_values(
        self,
        kind: Kind,
        kind_name: str,
        object_type: str,
        values: Mapping[str, str],
    ) -> tuple[dict[str, Any], dict[str, Any], set[tuple[str, str]]]:
        """Split values into core columns and kind fields (with virtual refs)."""
        valid = set(kind.fields) | set(_CORE_EDITABLE[object_type])
        unknown = sorted(set(values) - valid)
        if unknown:
            choices = ", ".join(sorted(valid)) or "(none)"
            raise UsageError(
                f"unknown field(s) {', '.join(repr(name) for name in unknown)} "
                f"for {object_type} kind {kind_name!r}; valid fields: {choices}"
            )
        core: dict[str, Any] = {}
        fields: dict[str, Any] = {}
        dependencies: set[tuple[str, str]] = set()
        for name, value in values.items():
            field_schema = kind.fields.get(name)
            if field_schema is None:
                core[name] = value
            elif field_schema.type is FieldType.ref:
                coerced, dependency = self._coerce_ref(
                    field_schema, value, label=name
                )
                fields[name] = coerced
                if dependency is not None:
                    dependencies.add(dependency)
            else:
                fields[name] = _coerce_field(
                    field_schema,
                    value,
                    ledger=self.ledger,
                    session=self.driver,
                    label=name,
                )
        return core, fields, dependencies

    # -- header ------------------------------------------------------------
    def _allowed_columns(
        self, object_type: str, kind_name: str | None
    ) -> set[str]:
        if object_type == "alias":
            return set(_CORE_COLUMNS["alias"]) | {"object"}
        allowed = set(_CORE_COLUMNS[object_type]) | {"alias"}
        if object_type == "aliquot":
            allowed |= {"kind"}
        if object_type == "stock":
            allowed |= _STOCK_PLACEMENT
        if kind_name is None:
            kinds = (
                list(self.schema.stock_kinds)
                if object_type == "aliquot"
                else list(self.schema.kinds_for(object_type))
            )
        else:
            kind = self.schema.kinds.get(kind_name)
            if kind is None:
                raise UsageError(f"unknown kind {kind_name!r}")
            kinds = [kind_name]
        for name in kinds:
            kind = self.schema.kind(name)
            if object_type == "aliquot":
                fields = kind.aliquot_fields
            else:
                fields = kind.fields
                if object_type == "stock":
                    fields = {**fields, **kind.aliquot_fields}
            allowed |= {
                field_name
                for field_name, schema_field in fields.items()
                if schema_field.type is not FieldType.computed
            }
        return allowed

    def _check_header(
        self,
        object_type: str,
        kind_name: str | None,
        header: Sequence[str],
        path: Path,
    ) -> None:
        allowed = self._allowed_columns(object_type, kind_name)
        unknown = [column for column in header if column not in allowed]
        if unknown:
            raise UsageError(
                f"{path.name}: row 1: unknown column(s) "
                f"{', '.join(repr(name) for name in unknown)}; "
                f"allowed: {', '.join(sorted(allowed))}"
            )
        required: set[str] = set()
        if object_type == "stock":
            required.add("entity")
        elif object_type == "box":
            required.add("name")
        elif object_type == "aliquot":
            if "stock" not in header and "id" not in header:
                raise UsageError(
                    f"{path.name}: row 1: aliquots need a 'stock' (or '<stock>.<seq>' "
                    "'id') column"
                )
        elif object_type == "alias":
            if "object_type" not in header and "object" not in header:
                required.add("object_type")
            required |= {"object_id", "alias"}
        missing = sorted(required - set(header))
        if missing:
            raise UsageError(
                f"{path.name}: row 1: missing column(s) "
                f"{', '.join(repr(name) for name in missing)}"
            )
        if object_type in ("entity", "stock", "box") and kind_name is None:
            if "kind" not in header:
                raise UsageError(
                    f"{path.name}: row 1: the 'kind' column is required when "
                    f"importing {object_type} without a single KIND"
                )

    # -- per-row planning --------------------------------------------------
    def _plan_row(
        self,
        raw: _RawRow,
        object_type: str,
        kind_name: str | None,
        path: Path,
    ) -> _RowPlan | None:
        if raw.kind is None and object_type != "alias":
            raise UsageError("cannot determine the kind of this row")
        if object_type == "entity":
            return self._plan_entity(raw, raw.kind or "")
        if object_type == "stock":
            return self._plan_stock(raw, raw.kind or "")
        if object_type == "box":
            return self._plan_box(raw, raw.kind or "")
        if object_type == "aliquot":
            return self._plan_aliquot(raw, raw.kind or "")
        return self._plan_alias(raw)

    def _plan_entity(self, raw: _RawRow, kind_name: str) -> _RowPlan:
        kind = self.schema.kind(kind_name)
        if raw.id is None:
            raise UsageError("this row has no id")
        values = self._field_values(raw, kind)
        name = _raw(raw.data, "name")
        if name is not None:
            values["name"] = name
        notes = _raw(raw.data, "notes")
        if notes is not None:
            values["notes"] = notes
        core, fields, dependencies = self._split_kind_values(
            kind, kind_name, "entity", values
        )
        _mirror_name(kind, core, fields)
        _require_fields(kind, kind_name, fields)
        if core.get("name") is not None and len(str(core["name"])) > 255:
            raise UsageError("name must be at most 255 characters")
        self._check_unique(kind_name, kind.fields, fields)

        payload: dict[str, Any] = {"id": raw.id, "kind": kind_name}
        payload.update(core)
        created = _parse_created_at(_text(raw.data, "created_at"))
        if created is not None:
            payload["created_at"] = created

        plan = _RowPlan(
            number=raw.number,
            object_type="entity",
            kind=kind_name,
            key=("entity", raw.id),
            depends_on=dependencies,
        )
        plan.inserts.append(("entity", payload))
        plan.inserts.append((table_name("entity", kind_name), {"id": raw.id, **fields}))
        self._add_alias_inserts(plan, "entity", raw.id, raw.aliases)
        self._remember_unique(kind_name, kind.fields, fields)
        return plan

    def _plan_stock(self, raw: _RawRow, kind_name: str) -> _RowPlan:
        kind = self.schema.kind(kind_name)
        if raw.id is None:
            raise UsageError("this row has no id")
        data = raw.data
        values = self._field_values(raw, kind)
        notes = _raw(data, "notes")
        if notes is not None:
            values["notes"] = notes
        core, fields, field_dependencies = self._split_kind_values(
            kind, kind_name, "stock", values
        )
        _require_fields(kind, kind_name, fields)
        self._check_unique(kind_name, kind.fields, fields)

        entity_text = _text(data, "entity")
        if entity_text is None:
            raise UsageError("a stock row needs an 'entity'")
        entity_resolved = self._resolve_any("entity", entity_text)
        if entity_resolved is None:
            raise UsageError(f"unknown entity {entity_text!r}")
        entity_id, entity_kind = entity_resolved
        if kind.entity_kinds is not None and entity_kind not in kind.entity_kinds:
            raise UsageError(
                f"stock kind {kind_name!r} only accepts entity kinds "
                f"[{', '.join(kind.entity_kinds)}], not {entity_kind!r}"
            )

        dependencies: set[tuple[str, str]] = set(field_dependencies)
        entity_dep = self._planned_key("entity", entity_id)
        if entity_dep is not None:
            dependencies.add(entity_dep)

        parent_id: str | None = None
        parent_text = _text(data, "parent_stock")
        if parent_text is not None:
            resolved = self._resolve_any("stock", parent_text)
            if resolved is None:
                raise UsageError(f"unknown parent stock {parent_text!r}")
            parent_id = resolved[0]
            dep = self._planned_key("stock", parent_id)
            if dep is not None:
                dependencies.add(dep)

        source_id: str | None = None
        # ``source_aliquot`` is filled by a deferred UPDATE: the source may be
        # created later in this import, and the FK must hold at insert time.
        deferred_source = _text(data, "source_aliquot") is not None
        source_text = _text(data, "source_aliquot")
        if source_text is not None:
            resolved = self._resolve_any("aliquot", source_text)
            if resolved is not None:
                source_id = resolved[0]
            else:
                # The source may be an aliquot created later in the same
                # import (aliquots.csv is imported after stocks).  Accept it
                # when its stock exists and satisfy the FK with an update once
                # every row is inserted.
                try:
                    source_stock, _ = parse_aliquot_id(source_text)
                except LedgerError:
                    raise UsageError(
                        f"unknown source aliquot {source_text!r}"
                    ) from None
                if self._resolve_any("stock", source_stock) is None:
                    raise UsageError(
                        f"unknown source aliquot {source_text!r}"
                    )
                source_id = source_text
                deferred_source = True

        count = _parse_positive_int(
            _text(data, "aliquots"), label="aliquots", allow_zero=True
        )
        if count is None:
            count = 1
        box_text = _text(data, "box")
        slots = _split_list(_text(data, "slot"))
        if count > 0 and len([slot for slot in slots if slot]) > count:
            raise UsageError(
                f"'slot' lists {len([slot for slot in slots if slot])} slot(s) "
                f"for only {count} aliquot(s)"
            )
        quantity, unit = _parse_quantity_unit(
            _text(data, "quantity"), _text(data, "unit")
        )
        status = _text(data, "status")
        if status is not None:
            status = status.lower()
        if status is not None and status not in self.schema.statuses:
            raise UsageError(
                f"unknown status {status!r}; choose from: "
                + ", ".join(self.schema.statuses)
            )
        aliquot_values = (
            self._aliquot_field_values(raw, kind, kind_name) if count > 0 else {}
        )
        if count == 0:
            provided_aliquot_fields = [
                name
                for name in kind.aliquot_fields
                if _raw(data, name) is not None
            ]
            if (
                box_text is not None
                or any(slots)
                or quantity is not None
                or status is not None
                or aliquot_values
                or provided_aliquot_fields
            ):
                raise UsageError(
                    "cannot place or describe aliquots when 'aliquots' is 0"
                )

        box_id: str | None = None
        box_slot_labels: set[str] = set()
        if box_text is not None:
            resolved = self._resolve_any("box", box_text)
            if resolved is None:
                raise UsageError(f"unknown box {box_text!r}")
            box_id, box_kind_name = resolved
            geometry = _box_geometry(self.schema.kind(box_kind_name))
            if geometry.is_slotted:
                box_slot_labels = {
                    label for bid, label in self._slot_labels if bid == box_id
                }
                if box_id in self._box_kinds and box_id not in self._db_box_ids():
                    box_slot_labels |= set(geometry.labels)
                if count > len([slot for slot in slots if slot]):
                    raise UsageError(
                        f"box {box_text!r} has slots: give one slot per aliquot"
                    )
            elif any(slots):
                raise UsageError(f"box {box_text!r} has no slots; omit 'slot'")
            dep = self._planned_key("box", box_id)
            if dep is not None:
                dependencies.add(dep)

        payload: dict[str, Any] = {
            "id": raw.id,
            "kind": kind_name,
            "entity_id": entity_id,
            "parent_stock_id": parent_id,
            "source_aliquot_id": None if deferred_source else source_id,
        }
        payload.update(core)
        created = _parse_created_at(_text(data, "created_at"))
        if created is not None:
            payload["created_at"] = created

        plan = _RowPlan(
            number=raw.number,
            object_type="stock",
            kind=kind_name,
            key=("stock", raw.id),
            depends_on=dependencies,
        )
        plan.inserts.append(("stock", payload))
        plan.inserts.append((table_name("stock", kind_name), {"id": raw.id, **fields}))

        local_slots: dict[tuple[str, str], int] = {}
        local_box: dict[str, int] = {}
        for index in range(1, count + 1):
            slot = slots[index - 1] if index - 1 < len(slots) else None
            slot = slot or None
            if box_id is not None:
                self._check_aliquot_placement(
                    box_id,
                    slot,
                    box_slot_labels=box_slot_labels,
                    local_slots=local_slots,
                    local_box=local_box,
                )
            elif slot is not None:
                raise UsageError("'slot' requires a 'box'")
            aliquot_status = status or ("stored" if box_id is not None else "pending")
            alq_id = f"{raw.id}.{index}"
            self._aliquot_ids.add(alq_id)
            self._aliquot_seqs.setdefault(raw.id, set()).add(index)
            plan.inserts.append(
                (
                    "aliquot",
                    {
                        "id": alq_id,
                        "stock_id": raw.id,
                        "seq": index,
                        "box_id": box_id,
                        "slot": slot,
                        "status": aliquot_status,
                        "quantity": quantity,
                        "unit": unit,
                    },
                )
            )
            if kind.aliquot_fields:
                plan.inserts.append(
                    (table_name("aliquot", kind_name), {"id": alq_id, **aliquot_values})
                )
            plan.events.append(
                {
                    "aliquot_id": alq_id,
                    "event": "create",
                    "amount": quantity,
                    "unit": unit,
                    "to_box_id": box_id,
                    "to_slot": slot,
                    "new_status": aliquot_status,
                    "actor": self.ledger.actor,
                }
            )
        self._apply_slot_deltas(local_slots, local_box)
        self._add_alias_inserts(plan, "stock", raw.id, raw.aliases)
        self._remember_unique(kind_name, kind.fields, fields)
        self._remember_unique(
            f"aliquot:{kind_name}", kind.aliquot_fields, aliquot_values
        )
        if deferred_source and source_id is not None:
            plan.deferred_updates.append(
                ("stock", {"id": raw.id}, {"source_aliquot_id": source_id})
            )
            self._deferred_sources.append(
                (raw.number, source_id, getattr(self, "_current_file_name", ""))
            )
        return plan

    def _plan_box(self, raw: _RawRow, kind_name: str) -> _RowPlan:
        kind = self.schema.kind(kind_name)
        if raw.id is None:
            raise UsageError("this row has no id")
        data = raw.data
        name = _text(data, "name")
        assert name is not None
        values = self._field_values(raw, kind)
        values.pop("name", None)
        notes = _raw(data, "notes")
        if notes is not None:
            values["notes"] = notes
        core, fields, dependencies = self._split_kind_values(
            kind, kind_name, "box", values
        )
        if "name" in kind.fields:
            name = _coerce_field(
                kind.fields["name"],
                name,
                ledger=self.ledger,
                session=self.driver,
                label="name",
            )
            fields["name"] = name
        core["name"] = name
        if len(str(name)) > 255:
            raise UsageError("name must be at most 255 characters")
        _require_fields(kind, kind_name, fields)
        self._check_unique(kind_name, kind.fields, fields)

        parent_text = _text(data, "parent")
        parent_slot = _text(data, "parent_slot")
        if parent_text is None and parent_slot is not None:
            raise UsageError("'parent_slot' requires a 'parent'")
        parent_id: str | None = None
        if parent_text is not None:
            resolved = self._resolve_any("box", parent_text)
            if resolved is None:
                raise UsageError(f"unknown parent box {parent_text!r}")
            parent_id, parent_kind_name = resolved
            self._check_child_placement(parent_id, parent_slot, parent_kind_name)
            dep = self._planned_key("box", parent_id)
            if dep is not None:
                dependencies.add(dep)

        payload: dict[str, Any] = {
            "id": raw.id,
            "kind": kind_name,
            "name": core.get("name", name),
            "parent_box_id": parent_id,
            "parent_slot": parent_slot,
        }
        payload.update(core)
        payload["id"] = raw.id
        payload["kind"] = kind_name
        created = _parse_created_at(_text(data, "created_at"))
        if created is not None:
            payload["created_at"] = created

        plan = _RowPlan(
            number=raw.number,
            object_type="box",
            kind=kind_name,
            key=("box", raw.id),
            depends_on=dependencies,
        )
        plan.inserts.append(("box", payload))
        plan.inserts.append((table_name("box", kind_name), {"id": raw.id, **fields}))
        geometry = _box_geometry(kind)
        for slot_row in slot_rows(raw.id, geometry):
            plan.inserts.append(("slot", slot_row))
            self._slot_labels.add((raw.id, str(slot_row["label"])))
        self._box_parents[raw.id] = parent_id
        if parent_id is not None:
            if parent_slot is not None:
                self._child_slots.add((parent_id, parent_slot))
            self._box_occupancy[parent_id] = (
                self._box_occupancy.get(parent_id, 0) + 1
            )
        self._add_alias_inserts(plan, "box", raw.id, raw.aliases)
        self._remember_unique(kind_name, kind.fields, fields)
        return plan

    def _plan_aliquot(self, raw: _RawRow, kind_name: str) -> _RowPlan:
        if raw.id is None or raw.stock_id is None or raw.seq is None:
            raise UsageError("this aliquot row has no resolved id")
        kind = self.schema.kind(kind_name)
        data = raw.data
        dependencies: set[tuple[str, str]] = set()

        box_id: str | None = None
        slot: str | None = _text(data, "slot")
        box_text = _text(data, "box")
        box_slot_labels: set[str] = set()
        if box_text is not None:
            resolved = self._resolve_any("box", box_text)
            if resolved is None:
                raise UsageError(f"unknown box {box_text!r}")
            box_id, box_kind_name = resolved
            geometry = _box_geometry(self.schema.kind(box_kind_name))
            if geometry.is_slotted:
                box_slot_labels = {
                    label for bid, label in self._slot_labels if bid == box_id
                }
                if box_id in self._box_kinds and box_id not in self._db_box_ids():
                    box_slot_labels |= set(geometry.labels)
            elif slot is not None:
                raise UsageError(f"box {box_text!r} has no slots; omit 'slot'")
            self._check_aliquot_placement(
                box_id,
                slot,
                box_slot_labels=box_slot_labels,
                local_slots={},
                local_box={},
            )
            dep = self._planned_key("box", box_id)
            if dep is not None:
                dependencies.add(dep)
        elif slot is not None:
            raise UsageError("'slot' requires a 'box'")

        status = _text(data, "status")
        if status is not None:
            status = status.lower()
        if status is not None and status not in self.schema.statuses:
            raise UsageError(
                f"unknown status {status!r}; choose from: "
                + ", ".join(self.schema.statuses)
            )
        if status is None:
            status = "stored" if box_id is not None else "pending"
        quantity, unit = _parse_quantity_unit(
            _text(data, "quantity"), _text(data, "unit")
        )
        aliquot_values = self._aliquot_field_values(raw, kind, kind_name)

        payload: dict[str, Any] = {
            "id": raw.id,
            "stock_id": raw.stock_id,
            "seq": raw.seq,
            "box_id": box_id,
            "slot": slot,
            "status": status,
            "quantity": quantity,
            "unit": unit,
        }
        notes = _raw(data, "notes")
        if notes is not None:
            payload["notes"] = notes
        created = _parse_created_at(_text(data, "created_at"))
        if created is not None:
            payload["created_at"] = created

        plan = _RowPlan(
            number=raw.number,
            object_type="aliquot",
            kind=kind_name,
            key=("aliquot", raw.id),
            depends_on=dependencies,
        )
        stock_dep = self._planned_key("stock", raw.stock_id)
        if stock_dep is not None:
            plan.depends_on.add(stock_dep)
        plan.inserts.append(("aliquot", payload))
        if kind.aliquot_fields:
            plan.inserts.append(
                (table_name("aliquot", kind_name), {"id": raw.id, **aliquot_values})
            )
        plan.events.append(
            {
                "aliquot_id": raw.id,
                "event": "create",
                "amount": quantity,
                "unit": unit,
                "to_box_id": box_id,
                "to_slot": slot,
                "new_status": status,
                "actor": self.ledger.actor,
            }
        )
        self._add_alias_inserts(plan, "aliquot", raw.id, raw.aliases)
        self._remember_unique(
            f"aliquot:{kind_name}", kind.aliquot_fields, aliquot_values
        )
        if box_id is not None:
            self._apply_slot_deltas(
                {(box_id, slot): 1} if slot else {}, {box_id: 1} if not slot else {}
            )
        return plan

    def _plan_alias(self, raw: _RawRow) -> _RowPlan | None:
        data = raw.data
        object_type = _text(data, "object_type") or _text(data, "object")
        if object_type is None:
            raise UsageError("an alias row needs 'object_type'")
        object_type = object_type.lower()
        if object_type not in _OBJECT_TYPES[:4]:
            raise UsageError(
                f"unknown object type {object_type!r}; choose from: "
                + ", ".join(_OBJECT_TYPES[:4])
            )
        object_id = _text(data, "object_id")
        alias = _text(data, "alias")
        if object_id is None:
            raise UsageError("an alias row needs 'object_id'")
        if alias is None:
            raise UsageError("an alias row needs 'alias'")
        resolved = self._resolve_any(object_type, object_id)
        if resolved is None:
            raise UsageError(f"unknown {object_type} {object_id!r}")
        object_id = resolved[0]
        _, insert = self._register_alias(object_type, object_id, alias)
        if not insert:
            return None
        plan = _RowPlan(
            number=raw.number,
            object_type="alias",
            kind=None,
            key=("alias", f"{object_type}:{alias}"),
        )
        plan.inserts.append(
            (
                "alias",
                {
                    "object_type": object_type,
                    "object_id": object_id,
                    "alias": alias,
                },
            )
        )
        return plan

    # -- per-row helpers ---------------------------------------------------
    def _field_values(self, raw: _RawRow, kind: Kind) -> dict[str, str]:
        values: dict[str, str] = {}
        for name, field_schema in kind.fields.items():
            if field_schema.type is FieldType.computed:
                continue
            text = _raw(raw.data, name)
            if text is not None:
                if field_schema.type in (FieldType.enum, FieldType.ref):
                    text = text.strip()
                values[name] = text
        return values

    def _aliquot_field_values(
        self, raw: _RawRow, kind: Kind, kind_name: str
    ) -> dict[str, Any]:
        if not kind.aliquot_fields:
            return {}
        data: dict[str, Any] = {}
        for name, field_schema in kind.aliquot_fields.items():
            if field_schema.type is FieldType.computed:
                continue
            text = _raw(raw.data, name)
            if text is None:
                continue
            if field_schema.type in (FieldType.enum, FieldType.ref):
                text = text.strip()
            data[name] = _coerce_field(
                field_schema,
                text,
                ledger=self.ledger,
                session=self.driver,
                label=name,
            )
        missing = [
            name
            for name, field_schema in kind.aliquot_fields.items()
            if field_schema.required
            and not field_schema.has_default
            and name not in data
        ]
        if missing:
            raise UsageError(
                f"missing required aliquot field(s) for kind {kind_name!r}: "
                + ", ".join(sorted(missing))
            )
        self._check_unique(f"aliquot:{kind_name}", kind.aliquot_fields, data)
        return data

    def _check_unique(
        self, kind_name: str, fields: Mapping[str, Field], values: Mapping[str, Any]
    ) -> None:
        for name, field_schema in fields.items():
            if not field_schema.unique or name not in values:
                continue
            seen = self._unique_seen.setdefault((kind_name, name), set())
            key = _unique_value(field_schema, values[name])
            if key in seen:
                raise UsageError(f"duplicate value for unique field {name!r}: {values[name]!r}")

    def _remember_unique(
        self, kind_name: str, fields: Mapping[str, Field], values: Mapping[str, Any]
    ) -> None:
        for name, field_schema in fields.items():
            if not field_schema.unique or name not in values:
                continue
            self._unique_seen.setdefault((kind_name, name), set()).add(
                _unique_value(field_schema, values[name])
            )

    def _db_box_ids(self) -> set[str]:
        if not hasattr(self, "_db_boxes_cache"):
            self._db_boxes_cache = {
                str(row["id"])
                for row in self.driver.query("SELECT `id` FROM `box`")
            }
        return self._db_boxes_cache

    def _box_label(self, box_id: str) -> str:
        for name, identifier in self._box_names.items():
            if identifier == box_id:
                return name
        return box_id

    def _check_aliquot_placement(
        self,
        box_id: str,
        slot: str | None,
        *,
        box_slot_labels: set[str] | None = None,
        local_slots: dict[tuple[str, str], int] | None = None,
        local_box: dict[str, int] | None = None,
    ) -> None:
        box_kind = self.schema.kind(self._box_kinds[box_id])
        geometry = _box_geometry(box_kind)
        label = self._box_label(box_id)
        local_slots = local_slots if local_slots is not None else {}
        local_box = local_box if local_box is not None else {}
        if geometry.is_slotted:
            if not slot:
                raise UsageError(
                    f"box {label!r} has slots: every stored aliquot needs a slot"
                )
            labels = box_slot_labels
            if labels is None:
                labels = {value for bid, value in self._slot_labels if bid == box_id}
            if not labels and box_id not in self._db_box_ids():
                labels = set(geometry.labels)
            if slot not in labels:
                raise UsageError(f"box {label!r} has no slot {slot!r}")
            if (box_id, slot) in self._child_slots:
                raise UsageError(f"slot {slot!r} in box {label!r} holds a child box")
            used = self._slot_counts.get((box_id, slot), 0) + local_slots.get(
                (box_id, slot), 0
            )
            if used >= box_kind.slot_capacity:
                raise UsageError(
                    f"slot {slot!r} in box {label!r} is full "
                    f"(capacity {box_kind.slot_capacity})"
                )
            local_slots[(box_id, slot)] = local_slots.get((box_id, slot), 0) + 1
            return
        if slot:
            raise UsageError(f"box {label!r} has no slots; omit 'slot'")
        capacity = geometry.capacity
        if capacity is not None:
            used = self._box_occupancy.get(box_id, 0) + local_box.get(box_id, 0)
            if used >= capacity:
                raise UsageError(
                    f"box {label!r} is full (capacity {capacity} aliquots + child boxes)"
                )
        local_box[box_id] = local_box.get(box_id, 0) + 1

    def _check_child_placement(
        self, parent_id: str, slot: str | None, parent_kind_name: str
    ) -> None:
        parent_kind = self.schema.kind(parent_kind_name)
        geometry = _box_geometry(parent_kind)
        label = self._box_label(parent_id)
        if geometry.is_slotted:
            if not slot:
                raise UsageError(f"box {label!r} has slots: 'parent_slot' is required")
            labels = {value for bid, value in self._slot_labels if bid == parent_id}
            if parent_id not in self._db_box_ids():
                labels |= set(geometry.labels)
            if slot not in labels:
                raise UsageError(f"box {label!r} has no slot {slot!r}")
            if (parent_id, slot) in self._child_slots:
                raise UsageError(
                    f"slot {slot!r} in box {label!r} already holds a child box"
                )
            if self._slot_counts.get((parent_id, slot), 0) > 0:
                raise UsageError(
                    f"slot {slot!r} in box {label!r} is occupied by an aliquot"
                )
            return
        if slot:
            raise UsageError(f"box {label!r} has no slots; omit 'parent_slot'")
        capacity = geometry.capacity
        if capacity is not None and self._box_occupancy.get(parent_id, 0) >= capacity:
            raise UsageError(
                f"box {label!r} is full (capacity {capacity} aliquots + child boxes)"
            )

    def _apply_slot_deltas(
        self, slots: Mapping[tuple[str, str], int], boxes: Mapping[str, int]
    ) -> None:
        for key, count in slots.items():
            self._slot_counts[key] = self._slot_counts.get(key, 0) + count
        for key, count in boxes.items():
            self._box_occupancy[key] = self._box_occupancy.get(key, 0) + count

    def _add_alias_inserts(
        self,
        plan: _RowPlan,
        object_type: str,
        object_id: str,
        aliases: Iterable[tuple[str, bool]],
    ) -> None:
        for alias, should_insert in aliases:
            if not should_insert:
                continue
            plan.inserts.append(
                (
                    "alias",
                    {
                        "object_type": object_type,
                        "object_id": object_id,
                        "alias": alias,
                    },
                )
            )

    def _check_box_cycles(self) -> None:
        planned_boxes = {
            plan.key[1]: plan.number
            for plan in self.plans
            if plan.object_type == "box"
        }
        for box_id, row in planned_boxes.items():
            seen: set[str] = set()
            current: str | None = box_id
            while current is not None:
                if current in seen:
                    self.errors.append(
                        RowError(
                            row,
                            f"box nesting cycle involving {box_id!r}",
                        )
                    )
                    break
                seen.add(current)
                current = self._box_parents.get(current)


def _topological_order(
    plans: list[_RowPlan], errors: list[RowError]
) -> list[_RowPlan]:
    """Order plans so every referenced in-file row is inserted first."""
    by_key = {plan.key: plan for plan in plans}
    order = {plan.key: index for index, plan in enumerate(plans)}
    dependencies = {
        plan.key: {dep for dep in plan.depends_on if dep in by_key}
        for plan in plans
    }
    remaining = {key: set(deps) for key, deps in dependencies.items()}
    ready = sorted(
        (key for key, deps in remaining.items() if not deps), key=order.__getitem__
    )
    result: list[_RowPlan] = []
    while ready:
        key = ready.pop(0)
        result.append(by_key[key])
        remaining.pop(key)
        newly_ready: list[tuple[str, str]] = []
        for other, deps in remaining.items():
            if key in deps:
                deps.remove(key)
                if not deps:
                    newly_ready.append(other)
        if newly_ready:
            ready.extend(newly_ready)
            ready.sort(key=order.__getitem__)
    if remaining:
        stuck = ", ".join(
            f"{by_key[key].object_type} row {by_key[key].number}"
            for key in remaining
        )
        errors.append(
            RowError(
                min(by_key[key].number for key in remaining),
                f"cyclic reference(s) inside the file: {stuck}",
            )
        )
        raise CsvImportError(errors)
    return result


# ---------------------------------------------------------------------------
# CSV reading / writing
# ---------------------------------------------------------------------------


def _read_csv(
    path: Path, object_type: str
) -> tuple[list[str], list[tuple[int, dict[str, str]]]]:
    if not path.is_file():
        raise UsageError(f"CSV file not found: {path}")
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle, restkey="__extra__", restval="")
            raw_header = reader.fieldnames
            if not raw_header:
                raise UsageError(f"{path.name}: row 1: the header is empty")
            header = [str(name).strip() for name in raw_header]
            if any(not name for name in header):
                raise UsageError(f"{path.name}: row 1: empty column name")
            if len(set(header)) != len(header):
                raise UsageError(f"{path.name}: row 1: duplicate column name")
            rows: list[tuple[int, dict[str, str]]] = []
            for record in reader:
                number = len(rows) + 1
                extra = record.pop("__extra__", None)
                data: dict[str, str] = {}
                for name, raw_name in zip(header, raw_header):
                    value = record.get(raw_name)
                    data[name] = "" if value is None else str(value)
                if extra:
                    raise CsvImportError(
                        [
                            RowError(
                                number,
                                f"too many columns (header has {len(header)})",
                                path.name,
                            )
                        ]
                    )
                if all(not value.strip() for value in data.values()):
                    continue
                rows.append((number, data))
    except UnicodeDecodeError as error:
        raise UsageError(f"{path.name}: not valid UTF-8: {error}") from error
    except csv.Error as error:
        raise UsageError(f"{path.name}: invalid CSV: {error}") from error
    return header, rows


def _write_csv(
    path: Path, header: Sequence[str], rows: Iterable[Mapping[str, Any]]
) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=list(header),
            extrasaction="ignore",
            lineterminator="\n",
        )
        writer.writeheader()
        for row in rows:
            writer.writerow({name: _format_cell(row.get(name)) for name in header})
            count += 1
    return count


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


def _normalise_object_type(value: str) -> str:
    text = value.strip().lower()
    aliases = {"aliases": "alias", "boxes": "box", "aliquots": "aliquot"}
    text = aliases.get(text, text)
    if text not in _OBJECT_TYPES:
        raise UsageError(
            f"unknown object type {value!r}; choose from: "
            + ", ".join(_OBJECT_TYPES)
        )
    return text


def _resolve_import_target(
    ledger: Ledger, kind: str, object_type: str | None
) -> tuple[str, str | None]:
    """Return ``(object_type, kind_name_or_None)`` for a command."""
    kind = str(kind).strip()
    if not kind:
        raise UsageError("KIND must not be empty")

    if object_type is not None:
        wanted = _normalise_object_type(object_type)
        if kind == "all":
            raise UsageError("'all' imports a directory; do not combine it with --object")
        if kind.lower() in _OBJECT_TYPES or kind.lower() in {
            "aliases",
            "boxes",
            "aliquots",
        }:
            if _normalise_object_type(kind) != wanted:
                raise UsageError(
                    f"KIND {kind!r} and --object {object_type!r} disagree"
                )
            return wanted, None
        schema_kind = ledger.schema.kinds.get(kind)
        if schema_kind is None:
            valid = ", ".join(sorted(ledger.schema.kinds))
            raise UsageError(f"unknown kind {kind!r}; choose from: {valid}")
        if wanted == "aliquot":
            if schema_kind.object is not ObjectType.stock:
                raise UsageError(
                    f"aliquot imports need a stock kind, not {kind!r}"
                )
            return "aliquot", kind
        if schema_kind.object.value != wanted:
            raise UsageError(
                f"kind {kind!r} is a {schema_kind.object.value} kind, "
                f"not {wanted}"
            )
        return wanted, kind

    if kind.lower() in ("alias", "aliases"):
        return "alias", None
    if kind.lower() in ("box", "boxes"):
        return "box", None
    if kind.lower() in ("aliquot", "aliquots"):
        return "aliquot", None
    schema_kind = ledger.schema.kinds.get(kind)
    if schema_kind is not None:
        return schema_kind.object.value, kind
    if kind.lower() in _OBJECT_TYPES:
        return _normalise_object_type(kind), None
    valid = ", ".join(sorted(ledger.schema.kinds))
    raise UsageError(
        f"unknown kind {kind!r}; choose from: {valid}, box, boxes, aliquots, "
        "aliases, all"
    )


def import_csv(
    ledger: Ledger,
    kind: str,
    path: str | Path,
    *,
    object_type: str | None = None,
    dry_run: bool | None = None,
) -> ImportResult:
    """Import one CSV (or, with ``KIND=all``, a directory of exported CSVs).

    Rows are fully validated first; every rejected row is reported with its row
    number and nothing is written.  A clean import is one transaction and one
    Dolt commit.
    """
    source = Path(path).expanduser()
    planner = _Planner(ledger)
    dry = ledger.dry_run if dry_run is None else dry_run

    if str(kind).strip().lower() == "all":
        if object_type is not None:
            raise UsageError("'import all' imports every kind; omit --object")
        if not source.is_dir():
            raise UsageError(
                f"'import all' needs a directory of exported CSVs (got {source})"
            )
        files = _all_import_files(ledger, source)
        if not files:
            raise UsageError(
                f"no importable CSV files found in {source}"
            )
        # Reserve every file's ids/names/aliases before planning any row, so a
        # reference to a kind exported in a later file still resolves.
        prepared = [
            planner.prepare_file(expected_object, expected_kind, file_path)
            for expected_object, expected_kind, file_path in files
        ]
        for prepared_file in prepared:
            planner.plan_file(prepared_file)
        total = sum(len(item.prepared) for item in prepared)
        plans = planner.finish()
        commit_object = "ledger"
        commit_summary = f"{total} rows"
        result_object = "ledger"
        result_kind = None
    else:
        target_object, target_kind = _resolve_import_target(ledger, kind, object_type)
        total = planner.add_file(target_object, target_kind, source)
        plans = planner.finish()
        commit_object = target_object
        commit_summary = f"{total} rows"
        result_object = target_object
        result_kind = target_kind

    counts = _plan_counts(plans)
    if not plans:
        return ImportResult(
            object=result_object,
            kind=result_kind,
            files=tuple(planner._files),
            rows=total,
            counts=counts,
            dry_run=dry,
        )

    with ledger.write(
        commit_summary,
        verb="import",
        object=commit_object,
        dry_run=dry,
    ) as session:
        for plan in plans:
            for table, payload in plan.inserts:
                _insert(session, table, payload)
            for event in plan.events:
                record_event(session, **event)
        for plan in plans:
            for table, key, payload in plan.deferred_updates:
                _update(session, table, key, payload)

    return ImportResult(
        object=result_object,
        kind=result_kind,
        files=tuple(planner._files),
        rows=total,
        counts=counts,
        dry_run=dry,
        sql=session.sql,
        commit_hash=None if dry else session.commit_hash,
    )


def _all_import_files(
    ledger: Ledger, directory: Path
) -> list[tuple[str, str | None, Path]]:
    """The files ``export all`` writes, in dependency order."""
    files: list[tuple[str, str | None, Path]] = []
    for kind_name in sorted(ledger.schema.entity_kinds):
        candidate = directory / f"{kind_name}.csv"
        if candidate.is_file():
            files.append(("entity", kind_name, candidate))
    boxes = directory / "boxes.csv"
    if boxes.is_file():
        files.append(("box", None, boxes))
    for kind_name in sorted(ledger.schema.stock_kinds):
        candidate = directory / f"{kind_name}.csv"
        if candidate.is_file():
            files.append(("stock", kind_name, candidate))
    aliquots = directory / "aliquots.csv"
    if aliquots.is_file():
        files.append(("aliquot", None, aliquots))
    aliases = directory / "aliases.csv"
    if aliases.is_file():
        files.append(("alias", None, aliases))
    return files


def _plan_counts(plans: Iterable[_RowPlan]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for plan in plans:
        counts[plan.object_type] = counts.get(plan.object_type, 0) + 1
        if plan.object_type == "stock":
            created = sum(1 for table, _ in plan.inserts if table == "aliquot")
            if created:
                counts["aliquot"] = counts.get("aliquot", 0) + created
    return counts


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def _export_header(base: Sequence[str], fields: Iterable[str]) -> list[str]:
    return _dedupe([*base, *fields, "alias"])


def _alias_map(ledger: Ledger) -> dict[tuple[str, str], list[str]]:
    result: dict[tuple[str, str], list[str]] = {}
    for row in ledger.driver.query(
        "SELECT `object_type`, `object_id`, `alias` FROM `alias` "
        "ORDER BY `object_type`, `object_id`, `alias`"
    ):
        key = (str(row["object_type"]), str(row["object_id"]))
        result.setdefault(key, []).append(str(row["alias"]))
    return result


def _fields_for_export(kind: Kind) -> list[str]:
    return [
        name
        for name, field_schema in kind.fields.items()
        if field_schema.type is not FieldType.computed
    ]


def _aliquot_fields_for_export(kind: Kind) -> list[str]:
    return [
        name
        for name, field_schema in kind.aliquot_fields.items()
        if field_schema.type is not FieldType.computed
    ]


def _export_entity_kind(
    ledger: Ledger,
    kind_name: str,
    destination: Path,
    aliases: Mapping[tuple[str, str], list[str]],
) -> int:
    kind = ledger.schema.kind(kind_name)
    header = _export_header(_CORE_COLUMNS["entity"], _fields_for_export(kind))
    rows = ledger.driver.query(f"SELECT * FROM {_quote(f'v_entity_{kind_name}')} ORDER BY `id`")
    data = []
    for row in rows:
        record = dict(row)
        record["alias"] = _ALIAS_SEPARATOR.join(
            aliases.get(("entity", str(row["id"])), [])
        )
        data.append(record)
    return _write_csv(destination, header, data)


def _export_stock_kind(
    ledger: Ledger,
    kind_name: str,
    destination: Path,
    aliases: Mapping[tuple[str, str], list[str]],
) -> int:
    kind = ledger.schema.kind(kind_name)
    header = _export_header(_CORE_COLUMNS["stock"], _fields_for_export(kind))
    rows = ledger.driver.query(f"SELECT * FROM {_quote(f'v_stock_{kind_name}')} ORDER BY `id`")
    data = []
    for row in rows:
        record = dict(row)
        record["entity"] = row.get("entity_id")
        record["parent_stock"] = row.get("parent_stock_id")
        record["source_aliquot"] = row.get("source_aliquot_id")
        # Aliquots are exported separately; ``aliquots=0`` keeps an import from
        # creating a second copy of them.
        record["aliquots"] = 0
        record["alias"] = _ALIAS_SEPARATOR.join(
            aliases.get(("stock", str(row["id"])), [])
        )
        data.append(record)
    return _write_csv(destination, header, data)


def _export_boxes(
    ledger: Ledger,
    destination: Path,
    aliases: Mapping[tuple[str, str], list[str]],
    *,
    only_kind: str | None = None,
) -> int:
    kinds = (
        {only_kind: ledger.schema.kind(only_kind)}
        if only_kind is not None
        else ledger.schema.box_kinds
    )
    field_names: list[str] = []
    for kind in kinds.values():
        field_names.extend(_fields_for_export(kind))
    header = _export_header(_CORE_COLUMNS["box"], _dedupe(field_names))

    data: list[dict[str, Any]] = []
    for kind_name in sorted(kinds):
        rows = ledger.driver.query(
            f"SELECT * FROM {_quote(f'v_box_{kind_name}')} ORDER BY `id`"
        )
        for row in rows:
            record = dict(row)
            record["parent"] = row.get("parent_box_id")
            record["parent_slot"] = row.get("parent_slot")
            record["alias"] = _ALIAS_SEPARATOR.join(
                aliases.get(("box", str(row["id"])), [])
            )
            data.append(record)
    data.sort(key=lambda item: str(item.get("id")))
    return _write_csv(destination, header, data)


def _export_aliases(ledger: Ledger, destination: Path) -> int:
    header = list(_CORE_COLUMNS["alias"])
    rows = ledger.driver.query(
        "SELECT `object_type`, `object_id`, `alias` FROM `alias` "
        "ORDER BY `object_type`, `object_id`, `alias`"
    )
    return _write_csv(destination, header, [dict(row) for row in rows])


def _export_aliquots(
    ledger: Ledger,
    destination: Path,
    aliases: Mapping[tuple[str, str], list[str]],
) -> int:
    field_names: list[str] = []
    for kind_name in sorted(ledger.schema.stock_kinds):
        field_names.extend(
            _aliquot_fields_for_export(ledger.schema.kind(kind_name))
        )
    header = _export_header(_CORE_COLUMNS["aliquot"], _dedupe(field_names))

    stock_kinds = {
        str(row["id"]): str(row["kind"])
        for row in ledger.driver.query("SELECT `id`, `kind` FROM `stock`")
    }
    box_names = {
        str(row["id"]): str(row["name"])
        for row in ledger.driver.query("SELECT `id`, `name` FROM `box`")
    }
    extensions: dict[str, dict[str, dict[str, Any]]] = {}
    for kind_name, kind in ledger.schema.stock_kinds.items():
        if not kind.aliquot_fields:
            continue
        try:
            rows = ledger.driver.query(
                f"SELECT * FROM {_quote(table_name('aliquot', kind_name))}"
            )
        except LedgerError:  # pragma: no cover - schema/DB mismatch
            continue
        extensions[kind_name] = {str(row["id"]): dict(row) for row in rows}

    data: list[dict[str, Any]] = []
    for row in ledger.driver.query("SELECT * FROM `aliquot` ORDER BY `id`"):
        record = dict(row)
        kind_name = stock_kinds.get(str(row["stock_id"]), "")
        record["kind"] = kind_name
        record["stock"] = row.get("stock_id")
        record["seq"] = row.get("seq")
        record["box"] = box_names.get(str(row.get("box_id")))
        record["alias"] = _ALIAS_SEPARATOR.join(
            aliases.get(("aliquot", str(row["id"])), [])
        )
        for field_name, value in extensions.get(kind_name, {}).get(
            str(row["id"]), {}
        ).items():
            if field_name != "id":
                record[field_name] = value
        data.append(record)
    return _write_csv(destination, header, data)


def export_csv(
    ledger: Ledger,
    kind: str = "all",
    out_dir: str | Path | None = None,
) -> ExportResult:
    """Export one kind (or ``all``) as CSVs importable by :func:`import_csv`."""
    directory = Path(out_dir).expanduser() if out_dir is not None else Path.cwd()
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise UsageError(f"cannot create export directory {directory}: {error}") from error
    aliases = _alias_map(ledger)
    files: list[Path] = []
    counts: dict[str, int] = {}

    def emit(name: str, writer: Any) -> None:
        path = directory / name
        counts[path.name] = writer(path)
        files.append(path)

    text = str(kind).strip()
    lowered = text.lower()

    if lowered == "all":
        reserved = {"boxes", "aliquots", "aliases"}
        clashes = sorted(name for name in ledger.schema.kinds if name in reserved)
        if clashes:
            raise UsageError(
                "cannot export all: kind(s) "
                + ", ".join(repr(name) for name in clashes)
                + " would collide with the fixed CSV names"
            )
        for kind_name in sorted(ledger.schema.entity_kinds):
            emit(
                f"{kind_name}.csv",
                lambda path, kind_name=kind_name: _export_entity_kind(
                    ledger, kind_name, path, aliases
                ),
            )
        emit(
            "boxes.csv",
            lambda path: _export_boxes(ledger, path, aliases),
        )
        for kind_name in sorted(ledger.schema.stock_kinds):
            emit(
                f"{kind_name}.csv",
                lambda path, kind_name=kind_name: _export_stock_kind(
                    ledger, kind_name, path, aliases
                ),
            )
        emit(
            "aliquots.csv",
            lambda path: _export_aliquots(ledger, path, aliases),
        )
        emit("aliases.csv", lambda path: _export_aliases(ledger, path))
        return ExportResult(directory=directory, files=tuple(files), counts=counts)

    if lowered in ("box", "boxes"):
        emit("boxes.csv", lambda path: _export_boxes(ledger, path, aliases))
        return ExportResult(directory=directory, files=tuple(files), counts=counts)
    if lowered in ("aliquot", "aliquots"):
        emit(
            "aliquots.csv",
            lambda path: _export_aliquots(ledger, path, aliases),
        )
        return ExportResult(directory=directory, files=tuple(files), counts=counts)
    if lowered in ("alias", "aliases"):
        emit("aliases.csv", lambda path: _export_aliases(ledger, path))
        return ExportResult(directory=directory, files=tuple(files), counts=counts)

    schema_kind = ledger.schema.kinds.get(text)
    if schema_kind is None:
        valid = ", ".join(sorted(ledger.schema.kinds))
        raise UsageError(
            f"unknown kind {kind!r}; choose from: {valid}, all, boxes, aliquots, aliases"
        )
    if schema_kind.object is ObjectType.entity:
        emit(
            f"{text}.csv",
            lambda path: _export_entity_kind(ledger, text, path, aliases),
        )
    elif schema_kind.object is ObjectType.stock:
        emit(
            f"{text}.csv",
            lambda path: _export_stock_kind(ledger, text, path, aliases),
        )
    else:
        emit(
            f"{text}.csv",
            lambda path: _export_boxes(ledger, path, aliases, only_kind=text),
        )
    return ExportResult(directory=directory, files=tuple(files), counts=counts)
