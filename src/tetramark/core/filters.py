"""Shared ``--where`` filters: ``=``, ``~``, ``!=``, ``!~``, ``<``, ``<=``, ``>``, ``>=``.

One filter backend serves the entity/stock/box listings (spec §6): the
``--where`` option of ``tm entity list``, ``tm stock list`` and ``tm box list``
and the ``where=`` argument of their Python API facades.  A filter is written
as

* ``field=value`` -- the column equals the value: a kind field is coerced by
  its schema (an enum is checked, a ref is resolved to the id it stores), a
  core column takes the text as it is;
* ``field~pattern`` -- the column's *text form* matches the regular expression
  ``pattern``;
* ``field!=value`` -- the column holds another value (or is ``NULL``);
* ``field!~pattern`` -- the column's text form does not match the pattern (or
  is ``NULL``);
* ``field<value``, ``field<=value``, ``field>value``, ``field>=value`` -- the
  column compares to the value in the order of its type.

The parser splits a filter at the operator that follows its field name, so a
value may contain ``=``, ``~``, ``!``, ``<`` and ``>``; the field name must be
an identifier.  A pattern is a *search*: it is unanchored (users anchor it
with ``^``/``$``) and case-sensitive (``(?i)`` makes it case-insensitive).
The database compiles the pattern (Dolt's ICU dialect), so the same pattern
means the same thing in the query and in the error message.

Rules:

* a field is filtered by ``=`` at most once; a second ``=`` on the same field
  is an error, and so is mixing ``=`` with any other operator on one field;
* any number of the other operators may address one field; they are ANDed;
* an empty pattern (``name~``) and an empty comparison value (``count<``) are
  errors;
* an invalid pattern is reported as a usage error naming the filter;
* ``field=`` (an empty value) means "the field is empty": ``NULL`` or ``''``
  on a text-like column (the kind types ``string``/``text``/``enum``/
  ``sequence``/``ref``, and every core column but ``created_at``), ``NULL``
  only on a non-text one (the kind types ``int``/``decimal``/``bool``/
  ``date``/``datetime``, and core ``created_at``); it skips the coercion and
  the ``ref``/alias resolution;
* ``NULL`` never matches a ``~`` pattern (not even ``field~^$``), and a
  ``ref`` field matches the id it stores.

Three-valued logic makes the negated operators the complements of their
positive ones, but not in the same way:

* ``field!=value`` is the complement of ``field=value``: it matches the rows
  whose value differs *and* the ``NULL`` rows;
* ``field!~pattern`` is the complement of ``field~pattern``: it matches the
  rows that do not match *and* the ``NULL`` rows;
* ``field!=`` (an empty value) is the complement of ``field=``: it matches the
  rows that have a value and excludes the ``NULL`` and the empty rows.

An ordered comparison is typed by its field: a number compares numerically, a
date or a datetime compares chronologically and a string compares in the
database's collation order (the order ``=`` uses).  A bool, an enum, a ref, a
sequence and a computed field have no order, so an ordered comparison on one
of them is a usage error.

A mapping (``where={"name": "pUC19"}``) means equality only, and a mapping
value ``None`` or ``""`` means "is empty"; a list of strings
(``where=["name~^pUC"]``, ``where=["name="]`` or ``where=["count>=10"]``)
accepts every operator.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from tetramark.errors import LedgerError, UsageError
from tetramark.schema.model import FieldType

if TYPE_CHECKING:  # pragma: no cover - import only for type checkers
    from tetramark.core.ledger import Ledger
    from tetramark.schema.model import Kind

__all__ = [
    "EQUALS",
    "REGEXP",
    "NOT_EQUALS",
    "NOT_REGEXP",
    "LESS",
    "LESS_EQUAL",
    "GREATER",
    "GREATER_EQUAL",
    "Filter",
    "build_conditions",
    "check_fields",
    "coerce_value",
    "comparison_type",
    "empty_condition",
    "filter_fields",
    "parse_filters",
    "pattern_is_invalid",
    "regex_usage_error",
    "validate_patterns",
]

#: The operator of an exact filter (``field=value``).
EQUALS = "="

#: The operator of a regular-expression filter (``field~pattern``).
REGEXP = "~"

#: The operator of an inequality filter (``field!=value``).
NOT_EQUALS = "!="

#: The operator of a negated regular-expression filter (``field!~pattern``).
NOT_REGEXP = "!~"

#: The operator of the ordered comparison ``field<value``.
LESS = "<"

#: The operator of the ordered comparison ``field<=value``.
LESS_EQUAL = "<="

#: The operator of the ordered comparison ``field>value``.
GREATER = ">"

#: The operator of the ordered comparison ``field>=value``.
GREATER_EQUAL = ">="

#: Every operator, the longest first: the parser takes the longest match at
#: the position right after the field name.
_OPERATORS = (
    NOT_EQUALS,
    NOT_REGEXP,
    LESS_EQUAL,
    GREATER_EQUAL,
    EQUALS,
    REGEXP,
    LESS,
    GREATER,
)

#: The operators that compare an ordered value.
_ORDERED_OPERATORS = frozenset({LESS, LESS_EQUAL, GREATER, GREATER_EQUAL})

#: The operators that carry a pattern.
_PATTERN_OPERATORS = frozenset({REGEXP, NOT_REGEXP})

#: A field name is an identifier: a letter or an underscore, then letters,
#: digits or underscores.  Core columns and kind fields are identifiers by
#: construction, so anything else is a typo (and never reaches the SQL).
_FIELD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")

#: The leading identifier of a filter text (the field name).
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

#: A statement that compiles a pattern without reading a row.
_PROBE_SQL = "SELECT %s REGEXP %s"

#: The subject the probe compiles the pattern against.
_PROBE_SUBJECT = "x"

#: The part of a driver error that says the pattern is the problem.
_REGEX_MARKER = "regular expression"

#: Kind field types whose column is text-like: an empty ``=`` filter means
#: ``NULL`` or ``''``.  The other types (``int``, ``decimal``, ``bool``,
#: ``date``, ``datetime``) mean ``NULL`` only, so a stored ``0`` never matches.
_TEXT_FIELD_TYPES = frozenset(
    {
        FieldType.string,
        FieldType.text,
        FieldType.enum,
        FieldType.sequence,
        FieldType.ref,
    }
)

#: The kinds of order an ordered comparison can use.
TEXT_ORDER = "text"
NUMBER_ORDER = "number"
DATETIME_ORDER = "datetime"


@dataclass(frozen=True)
class Filter:
    """One parsed filter: a field, an operator and a value."""

    field: str
    operator: str
    value: str

    @property
    def text(self) -> str:
        """The filter as it was written (``field=value`` / ``field~pattern``)."""
        return f"{self.field}{self.operator}{self.value}"


def parse_filters(
    values: Iterable[str] | Mapping[str, Any] | None,
    *,
    option: str = "--where",
) -> tuple[Filter, ...]:
    """Parse the ``--where`` filters of one listing.

    A mapping is equality only (``{"name": "pUC19"}``); a list of strings
    accepts every operator (``["name~^pUC"]``, ``["count>=10"]``).  The
    operator is the one that follows the field name, so a value may contain
    any operator character.

    Raises :class:`UsageError` for a malformed filter, a field name that is
    not an identifier, a repeated ``=`` on one field, a field filtered with
    ``=`` and another operator, an empty pattern and an empty comparison
    value.
    """
    filters: list[Filter] = []
    equals: set[str] = set()
    others: dict[str, str] = {}
    for field, operator, value, text in _split_filters(values, option=option):
        if not field:
            raise UsageError(f"invalid {option} {text!r}: the field name is empty")
        if not _FIELD_RE.match(field):
            raise UsageError(
                f"invalid {option} {text!r}: {field!r} is not a valid field name"
            )
        previous = others.get(field)
        if field in equals:
            if operator == EQUALS:
                raise UsageError(f"field {field!r} appears twice in {option}")
            raise UsageError(
                f"field {field!r} cannot be filtered with both {EQUALS!r} and "
                f"{operator!r} in {option}"
            )
        if operator == EQUALS:
            if previous is not None:
                raise UsageError(
                    f"field {field!r} cannot be filtered with both {EQUALS!r} and "
                    f"{previous!r} in {option}"
                )
            equals.add(field)
        else:
            # Every operator but ``=`` may be repeated on one field; they are
            # ANDed.  The first one is kept, so the error message names the
            # operator the field was already filtered with.
            others.setdefault(field, operator)
        if operator in _PATTERN_OPERATORS and not value:
            raise UsageError(
                f"the pattern of the {option} filter {field + operator!r} must not "
                "be empty"
            )
        if operator in _ORDERED_OPERATORS and not value:
            raise UsageError(
                f"the value of the {option} filter {field + operator!r} must not "
                "be empty"
            )
        filters.append(Filter(field, operator, value))
    return tuple(filters)


def filter_fields(filters: Iterable[Filter]) -> set[str]:
    """The fields the filters address."""
    return {item.field for item in filters}


def check_fields(
    filters: Iterable[Filter], valid: Iterable[str], *, option: str = "--where"
) -> None:
    """Refuse a filter on a field the object type does not have.

    The error names the offending fields and lists the valid ones, the way
    ``--where`` has always reported an unknown field.
    """
    fields = filter_fields(filters)
    unknown = sorted(fields - set(valid))
    if unknown:
        raise UsageError(
            f"unknown field(s) {', '.join(repr(name) for name in unknown)} in "
            f"{option}; valid fields: {', '.join(sorted(valid))}"
        )


def build_conditions(
    filters: Iterable[Filter],
    *,
    expressions: Mapping[str, str],
    coerce: Callable[[str, str], Any] | None = None,
    empty: Callable[[str, str], str] | None = None,
    kind: Kind | None = None,
    option: str = "--where",
) -> tuple[list[str], list[Any]]:
    """The SQL conditions and parameters of one kind's filters.

    ``expressions`` maps every filtered field to the SQL expression that holds
    its value: a quoted column of the kind's view, or the joined expression of
    a box kind's core row and kind row.  ``coerce`` turns the text of an ``=``
    (or ``!=``) filter into the value the column stores (the schema's coercion
    for a kind field, the raw text for a core column); it is not called for a
    pattern filter, whose pattern is matched against the column's text form.

    An ``=`` filter whose value is exactly the empty string means "the field
    is empty": it emits ``empty(field, expression)`` with no parameter and
    never calls ``coerce``.  ``empty`` lets the caller choose the SQL by field
    type (:func:`empty_condition`); without it the column is matched as text
    (``IS NULL OR = ''``).

    The negated and the ordered operators:

    * ``field!=value`` matches the rows whose value differs and the ``NULL``
      rows, so it is the complement of ``field=value``; an empty value means
      "the field has a value", the complement of ``field=``;
    * ``field!~pattern`` matches the rows whose text form does not match and
      the ``NULL`` rows, so it is the complement of ``field~pattern``;
    * ``field<value`` (and ``<=``, ``>``, ``>=``) compares the column with the
      value in the order of the field's type; a ``NULL`` never matches.

    ``kind`` tells an ordered comparison how the field compares; without it
    every field compares as text.
    """
    conditions: list[str] = []
    params: list[Any] = []
    for item in filters:
        expression = expressions[item.field]
        operator = item.operator
        if operator == EQUALS:
            if item.value == "":
                conditions.append(_empty_sql(item.field, expression, empty=empty))
                continue
            conditions.append(f"{expression} = %s")
            params.append(coerce(item.field, item.value) if coerce else item.value)
        elif operator == NOT_EQUALS:
            if item.value == "":
                conditions.append(
                    f"NOT ({_empty_sql(item.field, expression, empty=empty)})"
                )
                continue
            conditions.append(f"({expression} IS NULL OR {expression} <> %s)")
            params.append(coerce(item.field, item.value) if coerce else item.value)
        elif operator == REGEXP:
            conditions.append(f"{expression} REGEXP %s")
            params.append(item.value)
        elif operator == NOT_REGEXP:
            conditions.append(
                f"({expression} IS NULL OR NOT ({expression} REGEXP %s))"
            )
            params.append(item.value)
        else:
            order = comparison_type(
                kind, item.field, text=item.text, option=option
            )
            conditions.append(f"{expression} {operator} %s")
            params.append(
                _ordered_value(
                    kind,
                    item,
                    order=order,
                    coerce=coerce,
                    option=option,
                )
            )
    return conditions, params


def comparison_type(
    kind: Kind | None,
    field: str,
    *,
    text: str = "",
    option: str = "--where",
) -> str:
    """How an ordered comparison on ``field`` compares: text, number, datetime.

    A kind field is compared by its declared type: ``string``/``text`` compare
    as text, ``int``/``decimal`` compare numerically and ``date``/``datetime``
    compare chronologically.  A core column compares as text, except
    ``created_at`` (a datetime).  A bool, an enum, a ref, a sequence and a
    computed field have no order: an ordered comparison on one of them is a
    usage error naming the filter and the field type.
    """
    schema_field = kind.fields.get(field) if kind is not None else None
    if schema_field is not None:
        field_type = schema_field.type
        if field_type is FieldType.computed:
            raise UsageError(
                f"invalid {option} {text!r}: the field {field!r} is computed and "
                "cannot be compared"
            )
        if field_type in (FieldType.string, FieldType.text):
            return TEXT_ORDER
        if field_type in (FieldType.int, FieldType.decimal):
            return NUMBER_ORDER
        if field_type in (FieldType.date, FieldType.datetime):
            return DATETIME_ORDER
        raise UsageError(
            f"invalid {option} {text!r}: the field {field!r} is of type "
            f"{field_type.value}, which has no order"
        )
    if field == "created_at":
        return DATETIME_ORDER
    return TEXT_ORDER


def empty_condition(kind: Kind, field: str, expression: str) -> str:
    """The SQL condition for an empty ``=`` filter on one kind.

    A kind field is matched by its declared type: text-like fields (``string``,
    ``text``, ``enum``, ``sequence``, ``ref``) match ``NULL`` or ``''``, and
    the remaining types (``int``, ``decimal``, ``bool``, ``date``,
    ``datetime``) match ``NULL`` only, so a stored ``0`` never looks empty.
    A core column works the same way, except ``created_at``: every core column
    is text-like but that one is a datetime.  A computed field keeps the
    ``--set`` usage error: it cannot be written, so it cannot be filtered by an
    empty value either.
    """
    schema_field = kind.fields.get(field)
    if schema_field is not None:
        if schema_field.type is FieldType.computed:
            raise UsageError(f"field {field!r} is computed and cannot be set")
        if schema_field.type in _TEXT_FIELD_TYPES:
            return f"({expression} IS NULL OR {expression} = '')"
        return f"{expression} IS NULL"
    # Not a kind field: a core column.  Every core column is text-like except
    # the creation timestamp (a DATETIME).
    if field == "created_at":
        return f"{expression} IS NULL"
    return f"({expression} IS NULL OR {expression} = '')"


def coerce_value(
    kind: Kind,
    field: str,
    raw: str,
    *,
    ledger: Ledger,
    session: Any,
) -> Any:
    """The value an ``=`` filter stores, coerced by the field's schema.

    A core column has no schema of its own, so its value is the text the user
    wrote; a kind field is coerced like a ``--set`` value (an enum is checked,
    a ref is resolved to the id it stores, ...).
    """
    # Local import: objects.py imports this module, so a module-level import
    # here would be a cycle.
    from tetramark.core.objects import _coerce_field

    schema_field = kind.fields.get(field)
    if schema_field is None:
        return raw
    return _coerce_field(
        schema_field, raw, ledger=ledger, session=session, label=field
    )


def regex_usage_error(
    driver: Any,
    filters: Iterable[Filter],
    error: BaseException,
    *,
    option: str = "--where",
) -> UsageError | None:
    """The usage error for an invalid pattern, or ``None``.

    The database compiles the pattern, so an invalid one is only known when
    the query runs.  When the failure is a pattern error, the patterns are
    re-checked one by one (the error path may cost a few queries) to name the
    filter the user has to fix; a failure that is not about a pattern is none
    of this module's business and is reported as it is.
    """
    if not _is_regex_error(error):
        return None
    item = _offending_pattern(driver, filters)
    if item is None:
        return None
    return UsageError(_invalid_pattern_message(item, option=option))


def pattern_is_invalid(driver: Any, pattern: str) -> bool:
    """Whether the database refuses ``pattern`` (it compiles it, not we).

    ``tm find -E`` uses this on its error path: its query is a regex, not a
    ``--where`` filter, so :func:`regex_usage_error` does not apply.  Each call
    costs one probe query; a successful search never pays for it.
    """
    try:
        driver.query(_PROBE_SQL, (_PROBE_SUBJECT, pattern))
    except LedgerError as probe:
        return _is_regex_error(probe)
    return False


def validate_patterns(
    driver: Any, filters: Iterable[Filter], *, option: str = "--where"
) -> None:
    """Compile every pattern, raising the usage error for an invalid one.

    The database compiles a pattern, so this asks it.  A caller needs it only
    when no query carries the filters (a filter combination no kind can
    express): nothing else would compile the patterns there, and an invalid
    one must still be reported instead of silently returning no rows.
    """
    item = _offending_pattern(driver, filters)
    if item is not None:
        raise UsageError(_invalid_pattern_message(item, option=option))


def _split_filters(
    values: Iterable[str] | Mapping[str, Any] | None, *, option: str
) -> list[tuple[str, str, str, str]]:
    """The ``(field, operator, value, text)`` of every filter."""
    if values is None:
        return []
    if isinstance(values, Mapping):
        # A mapping is equality only: the key is the field, the value is the
        # value (a ``~`` in either is part of the text, never an operator).
        # A ``None`` value means the empty value (``field=``), not the text
        # ``"None"`` (the bug this spells out).
        split_mapping: list[tuple[str, str, str, str]] = []
        for key, value in values.items():
            text = "" if value is None else str(value)
            split_mapping.append(
                (str(key).strip(), EQUALS, text, f"{key}={text}")
            )
        return split_mapping
    split: list[tuple[str, str, str, str]] = []
    for item in values:
        text = str(item)
        parsed = _split_filter_text(text)
        if parsed is None:
            raise UsageError(
                f"invalid {option} {text!r}: expected field=value, "
                "field~pattern, field!=value, field!~pattern, or "
                "field<value / <= / > / >="
            )
        field, operator, value = parsed
        split.append((field, operator, value, text))
    return split


def _split_filter_text(text: str) -> tuple[str, str, str] | None:
    """The ``(field, operator, value)`` of one filter text, or ``None``.

    The field name is the identifier the text starts with (after the
    whitespace); the operator is the longest one that follows it (after the
    whitespace between the name and the operator); everything after the
    operator is the value, verbatim.
    """
    length = len(text)
    index = 0
    while index < length and text[index].isspace():
        index += 1
    match = _IDENT_RE.match(text, index)
    if match is None:
        return None
    field = match.group(0)
    index = match.end()
    while index < length and text[index].isspace():
        index += 1
    for operator in _OPERATORS:
        if text.startswith(operator, index):
            return field, operator, text[index + len(operator) :]
    return None


def _empty_sql(
    field: str, expression: str, *, empty: Callable[[str, str], str] | None
) -> str:
    """The SQL of the empty filter on one field."""
    if empty is not None:
        return empty(field, expression)
    return f"({expression} IS NULL OR {expression} = '')"


def _ordered_value(
    kind: Kind | None,
    item: Filter,
    *,
    order: str,
    coerce: Callable[[str, str], Any] | None,
    option: str,
) -> Any:
    """The value an ordered comparison compares with.

    A kind field is coerced by its schema (the same coercion an ``=`` filter
    uses), so an int compares numerically and a date compares chronologically.
    A core column takes the text as it is; ``created_at`` is a datetime, so
    its value must be an ISO date or datetime and is compared as a datetime
    (a date means its midnight), never as an arbitrary string.
    """
    if order == DATETIME_ORDER and (kind is None or item.field not in kind.fields):
        return _iso_datetime(item, option=option)
    if coerce is None:
        return item.value
    return coerce(item.field, item.value)


def _iso_datetime(item: Filter, *, option: str) -> datetime:
    """The datetime an ordered comparison on a core datetime column compares with."""
    text = item.value.strip()
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass
    try:
        return datetime.combine(date.fromisoformat(text), datetime.min.time())
    except ValueError:
        raise UsageError(
            f"invalid {option} {item.text!r}: the value {item.value!r} is not an "
            "ISO date or datetime"
        ) from None


def _is_regex_error(error: BaseException) -> bool:
    """Whether the driver refused the statement because of its pattern."""
    return _REGEX_MARKER in str(error).lower()


def _offending_pattern(driver: Any, filters: Iterable[Filter]) -> Filter | None:
    """The first pattern filter the database refuses, or ``None``."""
    for item in filters:
        if item.operator not in _PATTERN_OPERATORS:
            continue
        try:
            driver.query(_PROBE_SQL, (_PROBE_SUBJECT, item.value))
        except LedgerError as probe:
            # A failure that is not about the pattern is none of this
            # module's business: keep looking (and report it elsewhere).
            if _is_regex_error(probe):
                return item
    return None


def _invalid_pattern_message(item: Filter, *, option: str) -> str:
    """The usage error text: it names the filter the user has to fix."""
    return f"invalid regular expression in the {option} filter {item.text!r}"
