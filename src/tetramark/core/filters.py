"""Shared ``--where`` filters: ``field=value`` and ``field~pattern``.

One filter backend serves the entity/stock/box listings (spec §6): the
``--where`` option of ``tm entity list``, ``tm stock list`` and ``tm box list``
and the ``where=`` argument of their Python API facades.  A filter is written
as

* ``field=value`` -- the column equals the value: a kind field is coerced by
  its schema (an enum is checked, a ref is resolved to the id it stores), a
  core column takes the text as it is; and
* ``field~pattern`` -- the column's *text form* matches the regular expression
  ``pattern``.

The parser splits a filter at the first ``=`` or ``~``, so a value may contain
either character; the field name must be an identifier.  A pattern is a
*search*: it is unanchored (users anchor it with ``^``/``$``) and
case-sensitive (``(?i)`` makes it case-insensitive).  The database compiles the
pattern (Dolt's ICU dialect), so the same pattern means the same thing in the
query and in the error message.

Rules:

* a field is filtered by ``=`` at most once; a second ``=`` on the same field
  is an error, and so is mixing ``=`` and ``~`` on one field;
* any number of ``~`` filters may address one field; they are ANDed;
* an empty pattern (``name~``) is an error;
* an invalid pattern is reported as a usage error naming the filter;
* ``NULL`` never matches, and a ``ref`` field matches the id it stores.

A mapping (``where={"name": "pUC19"}``) means equality only; a list of strings
(``where=["name~^pUC"]``) accepts both operators.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from tetramark.errors import LedgerError, UsageError

if TYPE_CHECKING:  # pragma: no cover - import only for type checkers
    from tetramark.core.ledger import Ledger
    from tetramark.schema.model import Kind

__all__ = [
    "EQUALS",
    "REGEXP",
    "Filter",
    "build_conditions",
    "check_fields",
    "coerce_value",
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

#: A field name is an identifier: a letter or an underscore, then letters,
#: digits or underscores.  Core columns and kind fields are identifiers by
#: construction, so anything else is a typo (and never reaches the SQL).
_FIELD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")

#: A statement that compiles a pattern without reading a row.
_PROBE_SQL = "SELECT %s REGEXP %s"

#: The subject the probe compiles the pattern against.
_PROBE_SUBJECT = "x"

#: The part of a driver error that says the pattern is the problem.
_REGEX_MARKER = "regular expression"


@dataclass(frozen=True)
class Filter:
    """One parsed filter: ``field=value`` (exact) or ``field~pattern``."""

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
    """Parse ``field=value`` / ``field~pattern`` filters.

    A mapping is equality only (``{"name": "pUC19"}``); a list of strings
    accepts both operators (``["name~^pUC"]``).  The operator is the first
    ``=`` or ``~`` of the text, so a value may contain either character.

    Raises :class:`UsageError` for a malformed filter, a field name that is
    not an identifier, a repeated ``=`` on one field, a field filtered with
    both operators and an empty pattern.
    """
    filters: list[Filter] = []
    operators: dict[str, str] = {}
    for field, operator, value, text in _split_filters(values, option=option):
        if not field:
            raise UsageError(f"invalid {option} {text!r}: the field name is empty")
        if not _FIELD_RE.match(field):
            raise UsageError(
                f"invalid {option} {text!r}: {field!r} is not a valid field name"
            )
        previous = operators.get(field)
        if previous is not None:
            if previous == operator == EQUALS:
                raise UsageError(f"field {field!r} appears twice in {option}")
            if previous != operator:
                raise UsageError(
                    f"field {field!r} cannot be filtered with both {EQUALS!r} and "
                    f"{REGEXP!r} in {option}"
                )
        if operator == REGEXP and not value:
            raise UsageError(
                f"the pattern of the {option} filter {field + REGEXP!r} must not "
                "be empty"
            )
        operators[field] = operator
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
) -> tuple[list[str], list[Any]]:
    """The SQL conditions and parameters of one kind's filters.

    ``expressions`` maps every filtered field to the SQL expression that holds
    its value: a quoted column of the kind's view, or the joined expression of
    a box kind's core row and kind row.  ``coerce`` turns the text of an ``=``
    filter into the value the column stores (the schema's coercion for a kind
    field, the raw text for a core column); it is not called for a ``~``
    filter, whose pattern is matched against the column's text form.
    """
    conditions: list[str] = []
    params: list[Any] = []
    for item in filters:
        expression = expressions[item.field]
        if item.operator == EQUALS:
            conditions.append(f"{expression} = %s")
            params.append(coerce(item.field, item.value) if coerce else item.value)
        else:
            conditions.append(f"{expression} REGEXP %s")
            params.append(item.value)
    return conditions, params


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
        return [
            (str(key).strip(), EQUALS, str(value), f"{key}={value}")
            for key, value in values.items()
        ]
    split: list[tuple[str, str, str, str]] = []
    for item in values:
        text = str(item)
        index = _operator_index(text)
        if index is None:
            raise UsageError(
                f"invalid {option} {text!r}: expected field=value or field~pattern"
            )
        split.append((text[:index].strip(), text[index], text[index + 1 :], text))
    return split


def _operator_index(text: str) -> int | None:
    """The index of the first ``=`` or ``~`` in ``text``, or ``None``."""
    equals = text.find(EQUALS)
    tilde = text.find(REGEXP)
    if equals < 0:
        return tilde if tilde >= 0 else None
    if tilde < 0:
        return equals
    return min(equals, tilde)


def _is_regex_error(error: BaseException) -> bool:
    """Whether the driver refused the statement because of its pattern."""
    return _REGEX_MARKER in str(error).lower()


def _offending_pattern(driver: Any, filters: Iterable[Filter]) -> Filter | None:
    """The first ``~`` filter whose pattern the database refuses, or ``None``."""
    for item in filters:
        if item.operator != REGEXP:
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
