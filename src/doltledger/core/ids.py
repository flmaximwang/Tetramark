"""ID strategies for entities and stocks (spec §6.1).

Three strategies are available, selected per object in ``ledger.yaml``::

    ids:
      stock: {strategy: datecode, prefix: "12", reuse: true}
      entity: {strategy: uuid}

* ``uuid``     — a UUID v4 string.
* ``datecode`` — ``<prefix><YY><M><D><NN>`` where ``M`` is the month marker and
  ``D`` the day marker of :func:`date_code`, and ``NN`` is a two-digit
  sequence within that day (smallest unused by default).
* ``sequence`` — ``<prefix><NNNNN>``, monotonic.

Strategies are DB-agnostic: :meth:`IdStrategy.next_id` receives an ``existing``
callable. It is called once with a bare prefix string (``"0826FP"`` for a
datecode day, ``"S"`` for a sequence, ``""`` for UUIDs) and must return the IDs
already used whose text starts with that prefix. Strategies filter the returned
IDs themselves, so returning a superset is harmless.

Example::

    >>> DatecodeIds("08").next_id(lambda prefix: [], date(2026, 6, 24))
    '0826FP01'
"""

from __future__ import annotations

import re
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any, ClassVar

from doltledger.errors import LedgerError, UsageError

__all__ = [
    "DAY_LETTER_MARKERS",
    "MONTH_MARKERS",
    "DatecodeIds",
    "IdError",
    "IdStrategy",
    "SequenceIds",
    "UuidIds",
    "aliquot_id",
    "date_code",
    "is_aliquot_id",
    "next_aliquot_id",
    "parse_aliquot_id",
    "strategy_from_config",
]

#: Month 1–12 → letter markers (skipping ``I``), as ``~/.local/bin/date_code.py``.
MONTH_MARKERS = ("A", "B", "C", "D", "E", "F", "G", "H", "J", "K", "L", "M")

#: Day 10–31 → letter markers (skipping ``I``).
DAY_LETTER_MARKERS = (
    "A", "B", "C", "D", "E", "F", "G", "H",
    "J", "K", "L", "M", "N", "O", "P", "Q",
    "R", "S", "T", "U", "V", "W",
)


class IdError(LedgerError, ValueError):
    """An ID is malformed, or a strategy has exhausted its ID space."""


def date_code(month: int, day: int) -> str:
    """Return the two-character date code for ``month`` (1-12) and ``day`` (1-31).

    The first character is the month marker (``A``-``M`` skipping ``I``); the
    second is the day marker (``1``-``9``, then ``A``-``W`` skipping ``I``).
    Ported verbatim from ``~/.local/bin/date_code.py``.

    >>> date_code(6, 24)
    'FP'
    >>> date_code(1, 1)
    'A1'
    >>> date_code(12, 31)
    'MW'
    >>> date_code(9, 10)
    'JA'
    """
    if not 1 <= month <= 12:
        raise ValueError(f"Month must be 1-12, got {month}")
    if not 1 <= day <= 31:
        raise ValueError(f"Day must be 1-31, got {day}")
    month_marker = MONTH_MARKERS[month - 1]
    day_marker = str(day) if day <= 9 else DAY_LETTER_MARKERS[day - 10]
    return month_marker + day_marker


def _normalise_prefix(prefix: Any) -> str:
    """Return ``prefix`` as a non-empty, LIKE-safe string."""
    if isinstance(prefix, bool) or prefix is None:
        raise UsageError("id strategy 'prefix' must be a non-empty string")
    text = str(prefix).strip()
    if not text:
        raise UsageError("id strategy 'prefix' must be a non-empty string")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", text):
        raise UsageError(
            f"id strategy 'prefix' may only contain letters, digits, '_' and '-' (got {prefix!r})"
        )
    return text


def _numbers(ids: Iterable[str], head: str, width: int) -> set[int]:
    """Numeric tails of the IDs that start with ``head`` and have ``width`` digits."""
    pattern = re.compile(rf"{re.escape(head)}([0-9]{{{width}}})")
    numbers: set[int] = set()
    for raw in ids:
        match = pattern.fullmatch(str(raw))
        if match is not None:
            numbers.add(int(match.group(1)))
    return numbers


class IdStrategy(ABC):
    """Common interface of the ID strategies (spec §6.1)."""

    #: Value of the ``strategy:`` key in ``ledger.yaml``.
    name: ClassVar[str]

    @property
    @abstractmethod
    def pattern(self) -> str:
        """Anchored regular expression every well-formed ID matches."""

    @abstractmethod
    def next_id(
        self,
        existing: Callable[[str], Iterable[str]],
        today: date,
    ) -> str:
        """Return a fresh ID, asking ``existing(prefix)`` which IDs are taken."""

    @abstractmethod
    def validate(self, value: str) -> bool:
        """Return whether ``value`` is a well-formed ID for this strategy."""

    def require_valid(self, value: str) -> str:
        """Return ``value`` unchanged, or raise :class:`IdError` if malformed."""
        if not self.validate(value):
            raise IdError(
                f"malformed {self.name} id {value!r}; expected /{self.pattern}/"
            )
        return value


@dataclass(frozen=True)
class UuidIds(IdStrategy):
    """Entity / stock IDs from :func:`uuid.uuid4`."""

    name: ClassVar[str] = "uuid"

    @property
    def pattern(self) -> str:
        return (
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}"
            r"-[0-9a-f]{4}-[0-9a-f]{12}$"
        )

    def next_id(
        self,
        existing: Callable[[str], Iterable[str]],
        today: date,
    ) -> str:
        del today  # UUIDs do not depend on the date
        used = {str(value) for value in existing("")}
        while True:
            candidate = str(uuid.uuid4())
            if candidate not in used:
                return candidate

    def validate(self, value: str) -> bool:
        if not isinstance(value, str):
            return False
        return re.fullmatch(self.pattern, value.lower()) is not None


@dataclass(frozen=True)
class DatecodeIds(IdStrategy):
    """``<prefix><YY><M><D><NN>`` IDs, e.g. ``0826FP01`` (spec §6.1).

    ``NN`` is the smallest unused 2-digit sequence for the day when ``reuse`` is
    true (the default), otherwise the highest used value plus one. ``NN`` never
    starts at ``00`` and overflows past ``99`` with :class:`IdError`.
    """

    prefix: str = ""
    reuse: bool = True

    name: ClassVar[str] = "datecode"

    def __post_init__(self) -> None:
        object.__setattr__(self, "prefix", _normalise_prefix(self.prefix))
        if not isinstance(self.reuse, bool):
            raise UsageError(
                f"datecode id strategy 'reuse' must be a boolean (got {self.reuse!r})"
            )

    @property
    def pattern(self) -> str:
        # NN runs from 01 to 99: 00 is never a valid sequence.
        return (
            rf"^{re.escape(self.prefix)}[0-9]{{2}}"
            r"[A-HJ-M][1-9A-HJ-W](?:0[1-9]|[1-9][0-9])$"
        )

    def day_prefix(self, today: date) -> str:
        """Return ``<prefix><YY><M><D>`` for ``today`` (the ``NN``-less head)."""
        return f"{self.prefix}{today.year % 100:02d}{date_code(today.month, today.day)}"

    def next_id(
        self,
        existing: Callable[[str], Iterable[str]],
        today: date,
    ) -> str:
        head = self.day_prefix(today)
        used = _numbers(existing(head), head, 2)
        if self.reuse:
            for number in range(1, 100):
                if number not in used:
                    return f"{head}{number:02d}"
            raise IdError(
                f"datecode prefix {head!r} exhausted: all 99 ids for the day are in use"
            )
        number = max(used, default=0) + 1
        if number > 99:
            raise IdError(
                f"datecode prefix {head!r} exhausted: all 99 ids for the day are in use"
            )
        return f"{head}{number:02d}"

    def validate(self, value: str) -> bool:
        if not isinstance(value, str):
            return False
        return re.fullmatch(self.pattern, value) is not None


@dataclass(frozen=True)
class SequenceIds(IdStrategy):
    """``<prefix><NNNNN>`` monotonic IDs (``width`` digits, default 5)."""

    prefix: str = ""
    width: int = 5

    name: ClassVar[str] = "sequence"

    def __post_init__(self) -> None:
        object.__setattr__(self, "prefix", _normalise_prefix(self.prefix))
        width = self.width
        if isinstance(width, str) and width.strip().isdigit():
            width = int(width.strip())
        if isinstance(width, bool) or not isinstance(width, int) or width < 1:
            raise UsageError(
                f"sequence id strategy 'width' must be a positive integer (got {self.width!r})"
            )
        object.__setattr__(self, "width", width)

    @property
    def pattern(self) -> str:
        return rf"^{re.escape(self.prefix)}[0-9]{{{self.width}}}$"

    def next_id(
        self,
        existing: Callable[[str], Iterable[str]],
        today: date,
    ) -> str:
        del today  # sequences do not depend on the date
        used = _numbers(existing(self.prefix), self.prefix, self.width)
        number = max(used, default=0) + 1
        if number >= 10**self.width:
            raise IdError(
                f"sequence prefix {self.prefix!r} exhausted: width {self.width} is full"
            )
        return f"{self.prefix}{number:0{self.width}d}"

    def validate(self, value: str) -> bool:
        if not isinstance(value, str):
            return False
        return re.fullmatch(self.pattern, value) is not None


#: ``strategy:`` value → class, after normalising case, ``-`` and ``_``.
_STRATEGIES: dict[str, type[IdStrategy]] = {
    "uuid": UuidIds,
    "datecode": DatecodeIds,
    "sequence": SequenceIds,
}

#: Per-strategy options accepted from ``ledger.yaml`` (``strategy`` excluded).
_OPTIONS: dict[str, set[str]] = {
    "uuid": set(),
    "datecode": {"prefix", "reuse"},
    "sequence": {"prefix", "width"},
}

_REQUIRED: dict[str, set[str]] = {
    "uuid": set(),
    "datecode": {"prefix"},
    "sequence": {"prefix"},
}


def strategy_from_config(config: Mapping[str, Any]) -> IdStrategy:
    """Build a strategy from a ``ledger.yaml`` ``ids.<object>`` mapping.

    ``{"strategy": "datecode", "prefix": "12", "reuse": false}`` →
    ``DatecodeIds(prefix="12", reuse=False)``. Unknown strategies, missing
    required options and unexpected options raise :class:`UsageError` so typos
    surface at ``ledger init``/``open`` rather than producing odd IDs.
    """
    if not isinstance(config, Mapping):
        raise UsageError(f"id strategy config must be a mapping (got {type(config).__name__})")
    options = dict(config)
    raw_name = options.pop("strategy", None)
    if not isinstance(raw_name, str) or not raw_name.strip():
        raise UsageError(
            "id strategy config needs a 'strategy' key (uuid, datecode or sequence)"
        )
    key = re.sub(r"[-_]", "", raw_name.strip().lower())
    try:
        strategy_class = _STRATEGIES[key]
    except KeyError:
        raise UsageError(
            f"unknown id strategy {raw_name!r}; choose 'uuid', 'datecode' or 'sequence'"
        ) from None

    allowed = _OPTIONS[key]
    unexpected = sorted(set(options) - allowed)
    if unexpected:
        raise UsageError(
            f"id strategy {raw_name!r} does not accept option(s): {', '.join(unexpected)}"
        )
    missing = sorted(_REQUIRED[key] - set(options))
    if missing:
        raise UsageError(
            f"id strategy {raw_name!r} requires option(s): {', '.join(missing)}"
        )

    if key == "uuid":
        return UuidIds()
    if key == "datecode":
        return DatecodeIds(prefix=options["prefix"], reuse=options.get("reuse", True))
    return SequenceIds(prefix=options["prefix"], width=options.get("width", 5))


_ALIQUOT_PATTERN = re.compile(r"(?P<stock>.+)\.(?P<seq>[1-9][0-9]*)")


def aliquot_id(stock_id: str, seq: int) -> str:
    """Return the aliquot ID ``<stock_id>.<seq>`` (spec §1).

    >>> aliquot_id("0826FP01", 2)
    '0826FP01.2'
    """
    if not isinstance(stock_id, str) or not stock_id.strip():
        raise IdError("aliquot stock id must be a non-empty string")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
        raise IdError(f"aliquot seq must be a positive integer (got {seq!r})")
    return f"{stock_id}.{seq}"


def parse_aliquot_id(value: str) -> tuple[str, int]:
    """Split ``<stock_id>.<seq>`` into ``(stock_id, seq)``.

    >>> parse_aliquot_id("0826FP01.2")
    ('0826FP01', 2)

    Stock IDs may themselves contain dots (protein UIDs often do), so the split
    happens at the last dot.
    """
    match = _ALIQUOT_PATTERN.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise IdError(f"malformed aliquot id {value!r}; expected '<stock_id>.<seq>'")
    return match.group("stock"), int(match.group("seq"))


def is_aliquot_id(value: str) -> bool:
    """Return whether ``value`` looks like ``<stock_id>.<seq>``."""
    return isinstance(value, str) and _ALIQUOT_PATTERN.fullmatch(value) is not None


def next_aliquot_id(stock_id: str, existing: Iterable[str]) -> str:
    """Return the next aliquot ID for ``stock_id`` (``max(seq) + 1``, starting at 1)."""
    highest = 0
    for raw in existing:
        try:
            candidate_stock, seq = parse_aliquot_id(str(raw))
        except IdError:
            continue
        if candidate_stock == stock_id and seq > highest:
            highest = seq
    return aliquot_id(stock_id, highest + 1)
