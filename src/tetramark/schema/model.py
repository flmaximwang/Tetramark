"""Pydantic models for the declarative Tetramark schema.

The schema language is described in ``docs/spec/02-schema-language.md``.  This module only
models *shape*: loading, includes, mixins and cross references are handled by
:mod:`tetramark.schema.loader`.  Everything here is pure (no database access)
and safe to import from anywhere.

Notes
-----
* ``Field.default`` uses the :data:`UNSET` sentinel so that "no default given"
  can be distinguished from an explicit ``default: null``.  The sentinel is
  never serialised; :meth:`Field.normalised` omits it.
* :meth:`EffectiveSchema.fingerprint` hashes a canonical, order-independent
  rendering of the resolved schema.  Descriptions, mixin names and preset
  versions are deliberately excluded: they do not change the physical DDL.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re
import string
from enum import StrEnum
from typing import Any, Iterable

from pydantic import BaseModel, ConfigDict, RootModel, field_validator, model_validator
from pydantic import Field as PydanticField

from tetramark.errors import LedgerError

__all__ = [
    "BOX_NAME_MAX_LENGTH",
    "BOX_VIEW_PREFIX",
    "CORE_VIEW_NAMES",
    "SPEC_VERSION",
    "UNSET",
    "EffectiveSchema",
    "Field",
    "FieldType",
    "Geometry",
    "GeometryType",
    "IdStrategyConfig",
    "Kind",
    "LedgerConfig",
    "ObjectType",
    "Preset",
    "Rule",
    "RuleExpect",
    "RuleSeverity",
    "SchemaError",
    "SqlType",
    "Status",
    "StatusSet",
    "box_name_problem",
    "parse_labels",
]

#: Ledger format version (spec §3).  Bumped only when the generated shape
#: changes: v2 added the ``alias_list``/``occupant_alias_list`` columns to the
#: generated views so the ``alias`` table is visible there; v3 removed the
#: ``aliquot_<stockkind>`` extension tables (an aliquot is a generic tube now)
#: and added the entity/aliquot/quantity/status columns to every
#: ``v_stock_<kind>``; v4 moved the status vocabulary out of ``ledger.yaml``
#: into per-stock-kind ``status_sets``, dropped the global ``aliquot.status``
#: CHECK, added ``aliquot_event.forced`` and replaced the stock views'
#: ``aliquots_stored``/``quantity_stored``/five-value ``status`` with
#: ``aliquots_in_stock``/``aliquots_total``/``quantity_in_stock`` + ``unit`` and
#: ``status`` ∈ {active, depleted, empty}; v5 replaced the per-box-kind
#: ``v_box_<kind>`` views with one view per **existing box**
#: (``v_box_<box name>``), so a box's contents are read by box rather than by
#: kind.
SPEC_VERSION = 5

#: Views that exist for every ledger, independent of the selected kinds.
#: ``box_name_problem`` reserves the ``v_box_*`` ones: a per-box view must never
#: shadow a core view.  This tuple lives here (not in ``schema.ddl``, which
#: generates the views) because :mod:`tetramark.schema.model` must not import
#: the DDL module; ``schema.ddl.CORE_VIEWS`` re-exports it.
CORE_VIEW_NAMES: tuple[str, ...] = (
    "v_box_slots",
    "v_aliquot_location",
    "v_stock_status",
)

#: Prefix of the one generated view every existing box gets (spec 5).
BOX_VIEW_PREFIX = "v_box_"

#: Longest box name that still yields a view name Dolt accepts (64 characters
#: is the identifier limit, minus the ``v_box_`` prefix).
BOX_NAME_MAX_LENGTH = 64 - len(BOX_VIEW_PREFIX)

#: Order-independent schema rendering version.  Bump when the normalised
#: representation changes (invalidates stored fingerprints).
FINGERPRINT_VERSION = 1

IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_]*$")
RULE_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]*$")

#: Words that cannot be used as kind or field names.  This is a conservative
#: subset of the MySQL/Dolt reserved words: enough to keep generated DDL
#: unambiguous without banning ordinary names such as ``name`` or ``status``.
RESERVED_SQL_WORDS: frozenset[str] = frozenset(
    {
        "add",
        "all",
        "alter",
        "and",
        "as",
        "asc",
        "between",
        "by",
        "case",
        "check",
        "collate",
        "column",
        "constraint",
        "create",
        "cross",
        "database",
        "default",
        "delete",
        "desc",
        "distinct",
        "drop",
        "else",
        "end",
        "exists",
        "false",
        "foreign",
        "from",
        "full",
        "group",
        "having",
        "in",
        "index",
        "inner",
        "insert",
        "interval",
        "into",
        "is",
        "join",
        "key",
        "left",
        "like",
        "limit",
        "natural",
        "not",
        "null",
        "on",
        "or",
        "order",
        "outer",
        "primary",
        "references",
        "right",
        "select",
        "set",
        "table",
        "then",
        "to",
        "true",
        "union",
        "unique",
        "update",
        "using",
        "values",
        "when",
        "where",
        "with",
    }
)

#: Columns generated in every kind table; repurposing one would shadow the
#: core object's identity and discriminator.  The loader checks these (and the
#: per-object aliquot variant) while resolving documents.


# ---------------------------------------------------------------------------
# Box names and generated view names (spec 5)
# ---------------------------------------------------------------------------


def box_name_problem(
    box_name: str, existing_names: Iterable[str] = ()
) -> str | None:
    """Why ``box_name`` cannot name a box (spec 5), or ``None`` when it can.

    Every existing box gets a view named ``v_box_<box name>``, so a box name has
    to be a usable view name.  Three conditions make it unusable:

    a. the view would collide with a core view (today ``slots`` —
       ``v_box_slots`` — and any future core ``v_box_*`` view); the comparison
       is case-insensitive because Dolt stores view names in lowercase;
    b. it is longer than :data:`BOX_NAME_MAX_LENGTH` (58) characters, the Dolt
       identifier limit minus the ``v_box_`` prefix;
    c. it differs only in case from an existing box name: the two names would
       share one view (Dolt view names are case-insensitive), so one box would
       silently read the other's contents.

    An exact duplicate of an existing name is **not** a problem here: ``tm box
    add`` has its own duplicate check, and CSV import uses an existing name to
    update that box.  The caller decides what to do with the returned message
    (``tm box add`` and import refuse with it, the generated
    ``core_box_name_safe`` rule lists the rows).
    """
    name = str(box_name)
    view_name = f"{BOX_VIEW_PREFIX}{name}"
    reserved = {
        core.lower() for core in CORE_VIEW_NAMES if core.startswith(BOX_VIEW_PREFIX)
    }
    if view_name.lower() in reserved:
        return (
            f"box name {name!r} would make the view {view_name!r}, which is a "
            "core view; choose another name"
        )
    if len(name) > BOX_NAME_MAX_LENGTH:
        return (
            f"box name {name!r} is {len(name)} characters long; the limit is "
            f"{BOX_NAME_MAX_LENGTH} because every box gets a view named "
            f"'{BOX_VIEW_PREFIX}<name>'"
        )
    lowered = name.lower()
    for existing in existing_names:
        other = str(existing)
        if other != name and other.lower() == lowered:
            return (
                f"box name {name!r} differs only in case from the existing box "
                f"{other!r}; box names must be unique ignoring case because "
                "each one names a view"
            )
    return None


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SchemaError(LedgerError):
    """A schema document, include, mixin or cross-reference is invalid.

    Carries the YAML document (``file``) and the dotted path inside it
    (``path``) so the message looks like::

        molbio.yaml: kinds.plasmid.fields.construct.to: unknown kind 'x'
    """

    def __init__(self, message: str, *, file: str | None = None, path: str | None = None):
        self.message = message
        self.file = file
        self.path = path
        super().__init__(self._render())

    def _render(self) -> str:
        prefix = self.file or ""
        if self.path:
            prefix = f"{prefix}: {self.path}" if prefix else self.path
        return f"{prefix}: {self.message}" if prefix else self.message

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self._render()


# ---------------------------------------------------------------------------
# Field
# ---------------------------------------------------------------------------


class FieldType(StrEnum):
    """The field types of spec §2.1."""

    string = "string"
    text = "text"
    int = "int"
    decimal = "decimal"
    bool = "bool"
    date = "date"
    datetime = "datetime"
    enum = "enum"
    sequence = "sequence"
    ref = "ref"
    computed = "computed"


class SqlType(StrEnum):
    """Concrete SQL types accepted for a ``computed`` column."""

    string = "string"
    text = "text"
    int = "int"
    decimal = "decimal"
    bool = "bool"
    date = "date"
    datetime = "datetime"


class _UnsetType:
    """Sentinel for an absent ``default`` (as opposed to ``default: null``)."""

    _instance: _UnsetType | None = None

    def __new__(cls) -> _UnsetType:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "UNSET"

    def __bool__(self) -> bool:
        return False

    def __copy__(self) -> _UnsetType:
        return self

    def __deepcopy__(self, memo: dict[str, Any]) -> _UnsetType:
        return self


UNSET = _UnsetType()


class Field(BaseModel):
    """One column of a kind table (or one mixin field)."""

    model_config = ConfigDict(extra="forbid")

    type: FieldType
    required: bool = False
    unique: bool = False
    default: Any = UNSET
    pattern: str | None = None
    description: str | None = None
    length: int = 255
    precision: int = 18
    scale: int = 6
    values: list[str] | None = None
    to: str | None = None
    #: ``ref`` only: this field is a lineage edge (spec §2.1, ``tm lineage``).
    #: Read-only metadata: it changes no DDL, but it is part of the schema
    #: fingerprint and of ``tm schema show``.
    lineage: bool = False
    expr: str | None = None
    sql_type: SqlType | None = None
    renamed_from: str | None = None

    # -- resolved helpers ---------------------------------------------------

    @property
    def has_default(self) -> bool:
        return self.default is not UNSET

    @property
    def string_length(self) -> int:
        """Resolved ``VARCHAR`` length (spec default 255)."""
        return self.length

    @property
    def decimal_precision(self) -> int:
        """Resolved ``DECIMAL`` precision (spec default 18)."""
        return self.precision

    @property
    def decimal_scale(self) -> int:
        """Resolved ``DECIMAL`` scale (spec default 6)."""
        return self.scale

    # -- validation ---------------------------------------------------------

    @model_validator(mode="after")
    def _validate_type_specifics(self) -> Field:
        t = self.type
        explicit = set(self.model_fields_set)

        if "length" in explicit:
            if t is FieldType.string:
                pass
            elif t is FieldType.computed and self.sql_type is SqlType.string:
                pass
            else:
                raise ValueError(
                    "'length' is only valid for type 'string' "
                    "(or a computed string column)"
                )
            if self.length < 1:
                raise ValueError("'length' must be a positive integer")

        if "precision" in explicit or "scale" in explicit:
            if t is FieldType.decimal:
                pass
            elif t is FieldType.computed and self.sql_type is SqlType.decimal:
                pass
            else:
                raise ValueError(
                    "'precision'/'scale' are only valid for type 'decimal' "
                    "(or a computed decimal column)"
                )
            if self.precision < 1:
                raise ValueError("'precision' must be a positive integer")
            if not 0 <= self.scale <= self.precision:
                raise ValueError("'scale' must be between 0 and precision")

        if self.values is not None:
            if t is not FieldType.enum:
                raise ValueError("'values' is only valid for type 'enum'")
            if not self.values:
                raise ValueError("type 'enum' requires a non-empty 'values' list")
            if len(set(self.values)) != len(self.values):
                raise ValueError("'values' must not contain duplicates")
            if any(not isinstance(v, str) or not v for v in self.values):
                raise ValueError("'values' entries must be non-empty strings")
        elif t is FieldType.enum:
            raise ValueError("type 'enum' requires a non-empty 'values' list")

        if self.to is not None:
            if t is not FieldType.ref:
                raise ValueError("'to' is only valid for type 'ref'")
        elif t is FieldType.ref:
            raise ValueError("type 'ref' requires 'to'")

        if "lineage" in explicit:
            # ``lineage`` is a marker on a reference field, so it cannot exist
            # without one (the loader additionally rejects it on box kinds and
            # on refs that point at a box).
            if t is not FieldType.ref:
                raise ValueError("'lineage' is only valid for type 'ref'")

        if self.expr is not None or self.sql_type is not None:
            if t is not FieldType.computed:
                raise ValueError("'expr'/'sql_type' are only valid for type 'computed'")
        if t is FieldType.computed:
            if not self.expr:
                raise ValueError("type 'computed' requires a non-empty 'expr'")
            if self.sql_type is None:
                raise ValueError("type 'computed' requires 'sql_type'")
            if self.required or self.unique:
                raise ValueError("'required'/'unique' are not valid for type 'computed'")
            if self.has_default:
                raise ValueError("'default' is not valid for type 'computed'")
            if self.pattern is not None:
                raise ValueError("'pattern' is not valid for type 'computed'")

        if t is FieldType.sequence and self.has_default:
            # NOT NULL / UNIQUE are fine (they apply to the sha256 column).
            raise ValueError("'default' is not valid for type 'sequence'")

        if self.pattern is not None:
            if t not in (FieldType.string, FieldType.text, FieldType.enum, FieldType.sequence):
                raise ValueError(
                    "'pattern' is only valid for string, text, enum and sequence fields"
                )
            try:
                re.compile(self.pattern)
            except re.error as exc:
                raise ValueError(f"'pattern' is not a valid regular expression: {exc}") from exc

        self._check_default_type()
        return self

    def _check_default_type(self) -> None:
        if not self.has_default or self.default is None:
            return
        value = self.default
        t = self.type
        if t is FieldType.enum and value not in (self.values or []):
            raise ValueError(f"default {value!r} is not one of the enum values")
        if t is FieldType.int and (isinstance(value, bool) or not isinstance(value, int)):
            raise ValueError("default for type 'int' must be an integer")
        if t is FieldType.bool and not isinstance(value, bool):
            raise ValueError("default for type 'bool' must be true or false")
        if t is FieldType.decimal and (
            isinstance(value, bool) or not isinstance(value, (int, float))
        ):
            raise ValueError("default for type 'decimal' must be a number")
        if t in (FieldType.string, FieldType.text) and not isinstance(value, str):
            raise ValueError(f"default for type '{t.value}' must be a string")
        if t in (FieldType.date, FieldType.datetime):
            if t is FieldType.date:
                if isinstance(value, _dt.datetime):
                    raise ValueError(
                        "default for type 'date' must be a date, not a datetime"
                    )
                if isinstance(value, _dt.date):
                    return
            else:
                if isinstance(value, _dt.datetime):
                    return
                if isinstance(value, _dt.date):
                    raise ValueError(
                        "default for type 'datetime' must include a time"
                    )
            if not isinstance(value, str):
                raise ValueError(f"default for type '{t.value}' must be a date string")
            try:
                if t is FieldType.date:
                    _dt.date.fromisoformat(value)
                else:
                    _dt.datetime.fromisoformat(value)
            except ValueError as exc:
                raise ValueError(
                    f"default for type '{t.value}' is not a valid ISO date: {value!r}"
                ) from exc

    # -- canonical form -----------------------------------------------------

    def normalised(self) -> dict[str, Any]:
        """A stable, JSON-able representation used for fingerprinting."""
        out: dict[str, Any] = {"type": self.type.value}
        if self.required:
            out["required"] = True
        if self.unique:
            out["unique"] = True
        if self.has_default:
            value = self.default
            if isinstance(value, (_dt.datetime, _dt.date)):
                value = value.isoformat()
            out["default"] = value
        if self.pattern is not None:
            out["pattern"] = self.pattern
        if self.renamed_from is not None:
            out["renamed_from"] = self.renamed_from

        if self.type is FieldType.string:
            out["length"] = self.string_length
        elif self.type is FieldType.decimal:
            out["precision"] = self.decimal_precision
            out["scale"] = self.decimal_scale
        elif self.type is FieldType.enum:
            out["values"] = sorted(self.values or [])
        elif self.type is FieldType.ref:
            out["to"] = self.to
            if self.lineage:
                # Part of the fingerprint: ``tm lineage`` reads it, so two
                # ledgers with the same DDL but different edges are different
                # schemas.  Only emitted when set, so the fingerprints of
                # schemas without the flag do not change.
                out["lineage"] = True
        elif self.type is FieldType.computed:
            out["expr"] = self.expr
            out["sql_type"] = self.sql_type.value if self.sql_type else None
            if self.sql_type is SqlType.string:
                out["length"] = self.length
            elif self.sql_type is SqlType.decimal:
                out["precision"] = self.precision
                out["scale"] = self.scale
        return out


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

_GRID_RANGE_RE = re.compile(r"^([A-Z]|\d+)-([A-Z]|\d+)$")


class GeometryType(StrEnum):
    grid = "grid"
    named = "named"
    slotless = "slotless"


_DEFAULT_LABEL = "{row}{col}"
_LABEL_FIELDS = frozenset({"row", "col", "row_index", "col_index", "ordinal"})


def _parse_single_range(token: str) -> list[str]:
    match = _GRID_RANGE_RE.match(token)
    if match is None:
        raise ValueError(f"invalid range {token!r} (expected e.g. 'A-H' or '1-12')")
    start, end = match.groups()
    if start.isdigit() and end.isdigit():
        lo, hi = int(start), int(end)
        if lo < 0 or hi < 0:
            raise ValueError(f"invalid range {token!r}")
        if lo > hi:
            raise ValueError(f"range {token!r} is descending")
        return [str(value) for value in range(lo, hi + 1)]
    if start.isalpha() and end.isalpha():
        if start > end:
            raise ValueError(f"range {token!r} is descending")
        return [chr(code) for code in range(ord(start), ord(end) + 1)]
    raise ValueError(f"range {token!r} mixes letters and digits")


def parse_labels(
    spec: str | list[str | int] | tuple[str | int, ...],
) -> list[str]:
    """Expand a geometry axis spec into an ordered list of labels.

    Accepted forms (spec §2.2):

    * ``"A-H"``, ``"1-12"`` — inclusive ranges;
    * ``"A-J!I"`` — an inclusive range minus excluded labels (``!`` suffix,
      comma-separated for several exclusions, e.g. ``"1-12!3,5"``);
    * comma-separated combinations, e.g. ``"A-C,E,G-J"``;
    * an explicit YAML list, e.g. ``[A, B, C]``.

    A list is taken literally (items are labels, never ranges).  Duplicate
    labels are rejected: they would make a box slot ambiguous.
    """

    if isinstance(spec, (list, tuple)):
        labels = [str(item).strip() for item in spec]
        if any(not label for label in labels):
            raise ValueError("labels must be non-empty strings")
        if len(set(labels)) != len(labels):
            raise ValueError(f"duplicate labels in {list(spec)!r}")
        return labels

    if not isinstance(spec, str):
        raise ValueError("labels must be a string or a list of strings")

    text = spec.strip()
    if not text:
        raise ValueError("labels must not be empty")
    if "!" in text:
        base, _, excluded_text = text.partition("!")
        if not excluded_text:
            raise ValueError(f"empty exclusion in {spec!r}")
    else:
        base, excluded_text = text, ""
    excluded: list[str] = []
    if excluded_text:
        for item in excluded_text.split(","):
            item = item.strip()
            if not item:
                raise ValueError(f"empty exclusion in {spec!r}")
            if not (item.isdigit() or (len(item) == 1 and item.isalpha())):
                raise ValueError(f"invalid exclusion {item!r} in {spec!r}")
            excluded.append(item)

    labels: list[str] = []
    for token in base.split(","):
        token = token.strip()
        if not token:
            raise ValueError(f"empty label in {spec!r}")
        labels.extend(_parse_single_range(token) if "-" in token else [token])

    if excluded:
        missing = [item for item in excluded if item not in labels]
        if missing:
            raise ValueError(
                f"exclusion(s) {', '.join(missing)} not present in {spec!r}"
            )
        labels = [label for label in labels if label not in excluded]

    if not labels:
        raise ValueError(f"{spec!r} expands to no labels")
    if len(set(labels)) != len(labels):
        raise ValueError(f"duplicate labels in {spec!r}")
    return labels


class Geometry(BaseModel):
    """Box geometry: ``grid``, ``named`` or ``slotless`` (spec §2.2)."""

    model_config = ConfigDict(extra="forbid")

    type: GeometryType
    rows: str | list[str | int] | None = None
    cols: str | list[str | int] | None = None
    label: str = _DEFAULT_LABEL
    slots: list[str | int] | None = None
    capacity: int | None = None

    @model_validator(mode="after")
    def _validate_shape(self) -> Geometry:
        if self.type is GeometryType.grid:
            if self.rows is None or self.cols is None:
                raise ValueError("grid geometry requires 'rows' and 'cols'")
            if self.slots is not None:
                raise ValueError("'slots' is only valid for named geometry")
            if self.capacity is not None:
                raise ValueError("'capacity' is only valid for slotless geometry")
            self._check_label_template()
            # Parse eagerly so errors point at the geometry, not at first use.
            parse_labels(self.rows)
            parse_labels(self.cols)
            self._grid_labels()
        elif self.type is GeometryType.named:
            if not self.slots:
                raise ValueError("named geometry requires a non-empty 'slots' list")
            if self.rows is not None or self.cols is not None:
                raise ValueError("'rows'/'cols' are only valid for grid geometry")
            if self.capacity is not None:
                raise ValueError("'capacity' is only valid for slotless geometry")
            if self.label != _DEFAULT_LABEL:
                raise ValueError("named geometry labels come from 'slots'; 'label' is not allowed")
            labels = [str(slot).strip() for slot in self.slots]
            if any(not label for label in labels):
                raise ValueError("'slots' entries must be non-empty strings")
            if len(set(labels)) != len(labels):
                raise ValueError("'slots' must not contain duplicates")
        else:  # slotless
            if self.rows is not None or self.cols is not None or self.slots is not None:
                raise ValueError("slotless geometry takes no 'rows', 'cols' or 'slots'")
            if self.capacity is not None and self.capacity < 1:
                raise ValueError("'capacity' must be a positive integer")
            if self.label != _DEFAULT_LABEL:
                raise ValueError("slotless geometry has no labels; 'label' is not allowed")
        return self

    def _check_label_template(self) -> None:
        try:
            pieces = list(string.Formatter().parse(self.label))
        except ValueError as exc:
            raise ValueError(f"invalid label template {self.label!r}: {exc}") from exc
        for _literal, field_name, _format_spec, conversion in pieces:
            if field_name is None:
                continue
            if conversion is not None:
                raise ValueError(
                    f"invalid label template {self.label!r}: conversions are not supported"
                )
            if field_name not in _LABEL_FIELDS:
                allowed = ", ".join(sorted(_LABEL_FIELDS))
                raise ValueError(
                    f"invalid label template {self.label!r}: unknown field "
                    f"{field_name!r} (allowed: {allowed})"
                )

    # -- resolved labels ----------------------------------------------------

    @property
    def row_labels(self) -> list[str]:
        return list(parse_labels(self.rows)) if self.rows is not None else []

    @property
    def col_labels(self) -> list[str]:
        return list(parse_labels(self.cols)) if self.cols is not None else []

    def _grid_labels(self) -> list[str]:
        labels: list[str] = []
        ordinal = 0
        for row_index, row in enumerate(self.row_labels, start=1):
            for col_index, col in enumerate(self.col_labels, start=1):
                ordinal += 1
                labels.append(
                    self.label.format(
                        row=row,
                        col=col,
                        row_index=row_index,
                        col_index=col_index,
                        ordinal=ordinal,
                    )
                )
        if len(set(labels)) != len(labels):
            raise ValueError("geometry label template produces duplicate slot labels")
        return labels

    @property
    def labels(self) -> list[str]:
        """Ordered slot labels; empty for slotless geometry."""
        if self.type is GeometryType.grid:
            return self._grid_labels()
        if self.type is GeometryType.named:
            return [str(slot).strip() for slot in (self.slots or [])]
        return []

    @property
    def is_slotted(self) -> bool:
        return self.type is not GeometryType.slotless

    def normalised(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": self.type.value, "labels": self.labels}
        if self.type is GeometryType.slotless and self.capacity is not None:
            out["capacity"] = self.capacity
        return out


# ---------------------------------------------------------------------------
# Kind / preset / rules
# ---------------------------------------------------------------------------


class ObjectType(StrEnum):
    entity = "entity"
    stock = "stock"
    box = "box"


# ---------------------------------------------------------------------------
# Status sets (spec 4, design §4)
# ---------------------------------------------------------------------------


class Status(BaseModel):
    """One status of a :class:`StatusSet`.

    ``in_stock`` says whether an aliquot in this status counts as physically in
    stock (it drives the generated stock views).  ``to`` lists the statuses it
    may change to; an empty ``to`` means the status is **final** and no
    unforced change away from it is allowed.
    """

    model_config = ConfigDict(extra="forbid")

    in_stock: bool
    to: list[str] = PydanticField(default_factory=list)

    @field_validator("to")
    @classmethod
    def _validate_targets(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("'to' must not contain duplicates")
        for target in value:
            if not isinstance(target, str) or not IDENTIFIER_RE.match(target):
                raise ValueError(
                    f"'to' entry {target!r} must match [a-z][a-z0-9_]*"
                )
        return value

    def normalised(self) -> dict[str, Any]:
        return {"in_stock": self.in_stock, "to": sorted(self.to)}


class StatusSet(RootModel[dict[str, Status]]):
    """A named set of statuses and their transitions (spec 4).

    A preset declares sets under the top-level ``status_sets:`` key::

        status_sets:
          tube:
            pending: {in_stock: false, to: [stored, missing, consumed]}
            stored:  {in_stock: true,  to: [missing, consumed]}

    Every ``to:`` target must exist in the same set (a set is never merged
    across documents), and set names are global, like kind names.  A stock kind
    names its set through ``statuses: tube`` (or gives an inline set); an
    aliquot's allowed statuses are always its stock kind's set.
    """

    @model_validator(mode="after")
    def _validate_statuses(self) -> StatusSet:
        if not self.root:
            raise ValueError("a status set must define at least one status")
        for name in self.root:
            if not isinstance(name, str) or not IDENTIFIER_RE.match(name):
                raise ValueError(
                    f"status name {name!r} must match [a-z][a-z0-9_]*"
                )
            if name in RESERVED_SQL_WORDS:
                raise ValueError(f"status name {name!r} is a reserved SQL word")
        for name, status in self.root.items():
            for target in status.to:
                if target not in self.root:
                    raise ValueError(
                        f"status {name!r}: 'to' target {target!r} is not a "
                        "status of this set"
                    )
        return self

    # -- lookups ------------------------------------------------------------

    @property
    def names(self) -> list[str]:
        """Status names in declaration order."""
        return list(self.root)

    @property
    def in_stock_names(self) -> list[str]:
        """Statuses that count as in stock, in declaration order."""
        return [name for name, status in self.root.items() if status.in_stock]

    @property
    def final_names(self) -> list[str]:
        """Statuses with no ``to:`` targets (final states)."""
        return [name for name, status in self.root.items() if not status.to]

    def has(self, name: str) -> bool:
        return name in self.root

    def allows(self, old: str, new: str) -> bool:
        """True when an unforced change ``old`` → ``new`` is allowed."""
        current = self.root.get(old)
        if current is None:
            return False
        return new in current.to

    def normalised(self) -> dict[str, dict[str, Any]]:
        return {
            name: self.root[name].normalised() for name in sorted(self.root)
        }



class Kind(BaseModel):
    """One kind (a table extension of entity, stock or box).

    There is no per-kind aliquot table: an aliquot is a **generic tube**
    (location, quantity, unit, status, label, notes) and every property of the
    material belongs on the stock kind (spec §3).

    A **stock** kind must name the status set that applies to its aliquots
    (``statuses: tube``) or give an inline set; entity and box kinds must not.
    """

    model_config = ConfigDict(extra="forbid")

    object: ObjectType
    description: str | None = None
    fields: dict[str, Field] = PydanticField(default_factory=dict)
    mixins: list[str] = PydanticField(default_factory=list)
    entity_kinds: list[str] | None = None
    statuses: str | StatusSet | None = None
    geometry: Geometry | None = None
    slot_capacity: int = 1
    renamed_from: str | None = None

    @model_validator(mode="after")
    def _validate_object_shape(self) -> Kind:
        explicit = set(self.model_fields_set)

        if self.object is ObjectType.box:
            if self.geometry is None:
                raise ValueError("box kinds require a 'geometry'")
            if self.entity_kinds is not None:
                raise ValueError("'entity_kinds' is only valid on stock kinds")
            if self.slot_capacity < 1:
                raise ValueError("'slot_capacity' must be a positive integer")
        else:
            if self.geometry is not None:
                raise ValueError("'geometry' is only valid on box kinds")
            if "slot_capacity" in explicit:
                raise ValueError("'slot_capacity' is only valid on box kinds")
            if self.object is ObjectType.entity:
                if self.entity_kinds is not None:
                    raise ValueError("'entity_kinds' is only valid on stock kinds")
        if self.object is not ObjectType.stock:
            if self.statuses is not None:
                raise ValueError("'statuses' is only valid on stock kinds")
        elif self.statuses is None:
            raise ValueError(
                "stock kinds require 'statuses': name a set from 'status_sets' "
                "(e.g. 'statuses: tube') or give an inline set; a spec-3 preset "
                "that predates status sets is upgraded with "
                "migration/spec/3-to-4 and the wave-10 preset steps"
            )
        return self

    def status_set(self) -> StatusSet | None:
        """The kind's resolved status set (``None`` for entity/box kinds)."""
        return self.statuses if isinstance(self.statuses, StatusSet) else None

    def normalised(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "object": self.object.value,
            "fields": {
                name: self.fields[name].normalised() for name in sorted(self.fields)
            },
        }
        if self.entity_kinds is not None:
            out["entity_kinds"] = sorted(self.entity_kinds)
        if self.geometry is not None:
            out["geometry"] = self.geometry.normalised()
        if self.object is ObjectType.stock:
            # A named set and an inline set with the same content describe the
            # same ledger, so the fingerprint uses the resolved content.
            assert isinstance(self.statuses, StatusSet)
            out["statuses"] = self.statuses.normalised()
        if self.object is ObjectType.box:
            out["slot_capacity"] = self.slot_capacity
        if self.renamed_from is not None:
            out["renamed_from"] = self.renamed_from
        return out


class RuleSeverity(StrEnum):
    fail = "fail"
    info = "info"


class RuleExpect(BaseModel):
    """What a rule's SELECT must return (spec §2.3)."""

    model_config = ConfigDict(extra="forbid")

    single_value: int | None = None
    rows: int | None = None
    rows_at_most: int | None = None

    @model_validator(mode="after")
    def _validate(self) -> RuleExpect:
        set_fields = [
            name
            for name in ("single_value", "rows", "rows_at_most")
            if getattr(self, name) is not None
        ]
        if len(set_fields) != 1:
            raise ValueError(
                "'expect' must set exactly one of single_value, rows, rows_at_most"
            )
        value = getattr(self, set_fields[0])
        if value < 0:
            raise ValueError("'expect' values must be non-negative integers")
        return self

    @property
    def kind(self) -> str:
        if self.single_value is not None:
            return "single_value"
        if self.rows is not None:
            return "rows"
        return "rows_at_most"

    @property
    def value(self) -> int:
        return int(getattr(self, self.kind))

    def normalised(self) -> dict[str, int]:
        return {self.kind: self.value}


class Rule(BaseModel):
    """A custom validation rule compiled into ``dolt_tests`` (spec §2.3)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    description: str
    severity: RuleSeverity = RuleSeverity.fail
    sql: str
    expect: RuleExpect

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        if not RULE_ID_RE.match(value):
            raise ValueError("rule id must match [A-Za-z][A-Za-z0-9_.-]*")
        return value

    @field_validator("description")
    @classmethod
    def _validate_description(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("rule description must not be empty")
        return value

    @field_validator("sql")
    @classmethod
    def _validate_sql(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("rule sql must not be empty")
        statement = value.lstrip()
        # Skip leading SQL comments so "-- note\nSELECT ..." is accepted.
        while statement.startswith("--") or statement.startswith("/*"):
            if statement.startswith("--"):
                newline = statement.find("\n")
                if newline == -1:
                    raise ValueError("rule sql must be a SELECT statement")
                statement = statement[newline + 1 :].lstrip()
            else:
                end = statement.find("*/")
                if end == -1:
                    raise ValueError("rule sql has an unterminated comment")
                statement = statement[end + 2 :].lstrip()
        keyword = statement.split(None, 1)[0].upper() if statement else ""
        if keyword not in ("SELECT", "WITH"):
            raise ValueError("rule sql must be a read-only SELECT (or WITH ... SELECT)")
        if ";" in statement.rstrip().rstrip(";"):
            raise ValueError("rule sql must be a single statement")
        return value

    def normalised(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "severity": self.severity.value,
            "sql": self.sql,
            "expect": self.expect.normalised(),
        }


class Preset(BaseModel):
    """A parsed preset document (one YAML file)."""

    model_config = ConfigDict(extra="forbid")

    preset: str | None = None
    version: int = 1
    description: str | None = None
    include: list[str] = PydanticField(default_factory=list)
    status_sets: dict[str, StatusSet] = PydanticField(default_factory=dict)
    mixins: dict[str, dict[str, Field]] = PydanticField(default_factory=dict)
    kinds: dict[str, Kind] = PydanticField(default_factory=dict)
    rules: list[Rule] = PydanticField(default_factory=list)

    @field_validator("version")
    @classmethod
    def _validate_version(cls, value: int) -> int:
        if value < 1:
            raise ValueError("preset version must be a positive integer")
        return value


class IdStrategyConfig(BaseModel):
    """``ids:`` entry from ``ledger.yaml`` (spec §1.1, §6.1)."""

    model_config = ConfigDict(extra="forbid")

    strategy: str
    prefix: str = ""
    reuse: bool = True
    width: int = 5

    @field_validator("strategy")
    @classmethod
    def _validate_strategy(cls, value: str) -> str:
        if value not in ("uuid", "datecode", "sequence"):
            raise ValueError("strategy must be one of uuid, datecode, sequence")
        return value

    @field_validator("width")
    @classmethod
    def _validate_width(cls, value: int) -> int:
        if value < 1:
            raise ValueError("width must be a positive integer")
        return value


class LedgerConfig(BaseModel):
    """``ledger.yaml`` (spec §1.1)."""

    model_config = ConfigDict(extra="forbid")

    name: str
    presets: list[str] = PydanticField(default_factory=list)
    branch: str | None = None
    kinds_dir: str | None = None
    rules_dir: str | None = None
    author: str | None = None
    ids: dict[str, IdStrategyConfig] = PydanticField(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("ledger name must not be empty")
        return value

    @model_validator(mode="after")
    def _validate_ids(self) -> LedgerConfig:
        for name in self.ids:
            if name not in ("entity", "stock"):
                raise ValueError(
                    f"unknown id strategy for {name!r} (expected 'entity' or 'stock')"
                )
        return self

    def id_config(self, object_type: str | ObjectType) -> IdStrategyConfig:
        """Resolved ID strategy for an object type.

        ``entity`` defaults to UUID and ``stock`` to a date code, per §1.1.
        """
        name = object_type.value if isinstance(object_type, ObjectType) else object_type
        if name in self.ids:
            return self.ids[name]
        if name == "stock":
            return IdStrategyConfig(strategy="datecode")
        return IdStrategyConfig(strategy="uuid")


# ---------------------------------------------------------------------------
# Effective schema
# ---------------------------------------------------------------------------


class EffectiveSchema(BaseModel):
    """The resolved schema of one ledger.

    ``kinds`` maps kind name → resolved :class:`Kind` (mixins already inlined).
    """

    model_config = ConfigDict(extra="forbid")

    kinds: dict[str, Kind] = PydanticField(default_factory=dict)
    rules: list[Rule] = PydanticField(default_factory=list)
    #: Rule id → ``"preset"`` or ``"local"`` (``rules_dir``).  Drives the
    #: ``rules:<preset|local>`` group of the generated dolt_tests; excluded
    #: from the fingerprint because it does not change the physical schema.
    rule_sources: dict[str, str] = PydanticField(default_factory=dict)
    #: Every declared status set, by global name (spec 4).  Stock kinds carry
    #: their resolved set; this map is for introspection and drift checks.
    status_sets: dict[str, StatusSet] = PydanticField(default_factory=dict)
    preset_versions: dict[str, int] = PydanticField(default_factory=dict)

    # -- lookups ------------------------------------------------------------

    @property
    def entity_kinds(self) -> dict[str, Kind]:
        return {
            name: kind
            for name, kind in self.kinds.items()
            if kind.object is ObjectType.entity
        }

    @property
    def stock_kinds(self) -> dict[str, Kind]:
        return {
            name: kind
            for name, kind in self.kinds.items()
            if kind.object is ObjectType.stock
        }

    @property
    def box_kinds(self) -> dict[str, Kind]:
        return {
            name: kind
            for name, kind in self.kinds.items()
            if kind.object is ObjectType.box
        }

    def kind(self, name: str) -> Kind:
        try:
            return self.kinds[name]
        except KeyError:
            raise SchemaError(f"unknown kind {name!r}") from None

    def status_set(self, kind_name: str) -> StatusSet | None:
        """The status set of a stock kind (``None`` for entity/box kinds)."""
        kind = self.kind(kind_name)
        return kind.status_set()

    def statuses_for(self, kind_name: str) -> list[str]:
        """The allowed aliquot statuses of a stock kind, in declared order."""
        status_set = self.status_set(kind_name)
        return status_set.names if status_set is not None else []

    def has_kind(self, name: str) -> bool:
        return name in self.kinds

    def kinds_for(self, object_type: str | ObjectType) -> dict[str, Kind]:
        obj = object_type.value if isinstance(object_type, ObjectType) else object_type
        return {
            name: kind
            for name, kind in self.kinds.items()
            if kind.object.value == obj
        }

    @property
    def kinds_by_object(self) -> dict[str, dict[str, Kind]]:
        """All resolved kinds grouped by core object."""
        return {
            "entity": self.entity_kinds,
            "stock": self.stock_kinds,
            "box": self.box_kinds,
        }

    # -- fingerprint --------------------------------------------------------

    def normalised(self) -> dict[str, Any]:
        """Canonical, order-independent rendering of the resolved schema."""
        return {
            "fingerprint_version": FINGERPRINT_VERSION,
            "spec_version": SPEC_VERSION,
            "status_sets": {
                name: self.status_sets[name].normalised()
                for name in sorted(self.status_sets)
            },
            "kinds": {
                name: self.kinds[name].normalised() for name in sorted(self.kinds)
            },
            "rules": sorted(
                (rule.normalised() for rule in self.rules), key=lambda rule: rule["id"]
            ),
        }

    def fingerprint(self) -> str:
        """Stable sha256 of :meth:`normalised` (hex digest)."""
        payload = json.dumps(
            self.normalised(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=_json_default,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _json_default(value: Any) -> str:
    if isinstance(value, (_dt.datetime, _dt.date)):
        return value.isoformat()
    return str(value)
