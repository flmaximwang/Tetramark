"""Unit tests for the ID strategies (spec §6.1, ticket 04).

Pure unit tests: no Dolt involved, nothing is mocked. ``date_code`` is checked
against the same table as ``~/.local/bin/date_code.py`` and the strategies are
driven through their ``existing(prefix)`` callback.
"""

from __future__ import annotations

import doctest
import re
import uuid
from datetime import date

import pytest

from doltledger.core import ids as ids_module
from doltledger.core.ids import (
    DAY_LETTER_MARKERS,
    MONTH_MARKERS,
    DatecodeIds,
    IdError,
    SequenceIds,
    UuidIds,
    aliquot_id,
    date_code,
    is_aliquot_id,
    next_aliquot_id,
    parse_aliquot_id,
    strategy_from_config,
)
from doltledger.errors import LedgerError, UsageError

# 2026-06-24 is the example date of the spec: prefix 08 → 0826FP01.
TODAY = date(2026, 6, 24)


def _existing(*values: str):
    """An ``existing`` callback that records the prefix it was asked about."""
    calls: list[str] = []

    def callback(prefix: str) -> list[str]:
        calls.append(prefix)
        return list(values)

    return callback, calls


# --------------------------------------------------------------------------- date_code


def test_doctests_pass() -> None:
    results = doctest.testmod(ids_module)
    assert results.failed == 0
    assert results.attempted >= 4


@pytest.mark.parametrize(
    ("month", "day", "expected"),
    [
        (6, 24, "FP"),
        (1, 1, "A1"),
        (12, 31, "MW"),
        (9, 10, "JA"),
        (1, 9, "A9"),
        (1, 10, "AA"),
        (1, 18, "AJ"),  # day 18 skips I (A..H, J)
        (1, 31, "AW"),
        (8, 31, "HW"),
        (2, 14, "BE"),
        (10, 1, "K1"),
        (11, 30, "LV"),
    ],
)
def test_date_code_examples(month: int, day: int, expected: str) -> None:
    assert date_code(month, day) == expected


def test_date_code_month_markers() -> None:
    assert [date_code(month, 1)[0] for month in range(1, 13)] == list(MONTH_MARKERS)
    assert "I" not in MONTH_MARKERS


def test_date_code_day_markers() -> None:
    assert [date_code(1, day)[1] for day in range(1, 10)] == list("123456789")
    assert [date_code(1, day)[1] for day in range(10, 32)] == list(DAY_LETTER_MARKERS)
    assert "I" not in DAY_LETTER_MARKERS
    assert len(set(DAY_LETTER_MARKERS)) == 22


@pytest.mark.parametrize(("month", "day"), [(0, 1), (13, 1), (-1, 1), (1, 0), (1, 32), (1, -1)])
def test_date_code_rejects_out_of_range(month: int, day: int) -> None:
    with pytest.raises(ValueError):
        date_code(month, day)


# --------------------------------------------------------------------------- uuid


def test_uuid_next_id_is_a_unique_v4() -> None:
    callback, calls = _existing()
    value = UuidIds().next_id(callback, TODAY)

    assert uuid.UUID(value).version == 4
    assert str(uuid.UUID(value)) == value
    assert calls == [""], "UUIDs ask for the whole ID space (empty prefix)"
    assert UuidIds().validate(value)


def test_uuid_next_id_skips_known_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    taken = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
    fresh = "11111111-1111-4111-8111-111111111111"
    generated = iter([taken, fresh])

    class FakeUUID:
        def __init__(self, text: str) -> None:
            self.text = text

        def __str__(self) -> str:
            return self.text

    monkeypatch.setattr(uuid, "uuid4", lambda: FakeUUID(next(generated)))
    assert UuidIds().next_id(lambda prefix: [taken], TODAY) == fresh


@pytest.mark.parametrize(
    "value",
    [
        "3f2504e0-4f89-41d3-9a0c-0305e82c3301",
        "3F2504E0-4F89-41D3-9A0C-0305E82C3301",
        "00000000-0000-0000-0000-000000000000",
    ],
)
def test_uuid_validate_accepts_canonical(value: str) -> None:
    assert UuidIds().validate(value)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "not-a-uuid",
        "3f2504e04f8941d39a0c0305e82c3301",  # no hyphens
        "{3f2504e0-4f89-41d3-9a0c-0305e82c3301}",  # braces
        "3f2504e0-4f89-41d3-9a0c-0305e82c330",  # one digit short
        "3f2504e0-4f89-41d3-9a0c-0305e82c33011",  # one digit long
        "0826FP01",
    ],
)
def test_uuid_validate_rejects_other_shapes(value: str) -> None:
    assert not UuidIds().validate(value)


def test_validate_rejects_non_strings() -> None:
    assert not UuidIds().validate(42)  # type: ignore[arg-type]
    assert not DatecodeIds("08").validate(None)  # type: ignore[arg-type]
    assert not SequenceIds("S").validate(1)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- datecode


def test_datecode_first_id_of_the_day() -> None:
    strategy = DatecodeIds("08")
    callback, calls = _existing()

    assert strategy.next_id(callback, TODAY) == "0826FP01"
    assert calls == ["0826FP"], "the callback is asked for the NN-less day prefix"


def test_datecode_reuses_the_smallest_gap() -> None:
    strategy = DatecodeIds("08")
    callback, calls = _existing("0826FP01", "0826FP02", "0826FP07")

    assert strategy.next_id(callback, TODAY) == "0826FP03"
    assert calls == ["0826FP"]


def test_datecode_reuse_false_takes_max_plus_one() -> None:
    strategy = DatecodeIds("08", reuse=False)
    callback, _ = _existing("0826FP01", "0826FP02", "0826FP07")

    assert strategy.next_id(callback, TODAY) == "0826FP08"


def test_datecode_reuse_false_with_sequence_holes_only_uses_max() -> None:
    strategy = DatecodeIds("08", reuse=False)
    assert strategy.next_id(_existing("0826FP05")[0], TODAY) == "0826FP06"


def test_datecode_ignores_other_days_and_malformed_ids() -> None:
    strategy = DatecodeIds("08")
    callback, _ = _existing(
        "0826FP01",  # same day: counts
        "0825FP01",  # yesterday
        "0826FQ05",  # another day marker
        "0926FP01",  # another prefix
        "0826FP00",  # NN never starts at 00
        "0826FP99X",  # trailing junk
        "0826FP1",  # one-digit NN
        "0826FP123",  # three-digit NN
    )
    assert strategy.next_id(callback, TODAY) == "0826FP02"


@pytest.mark.parametrize(
    ("today", "expected"),
    [
        (date(2026, 1, 1), "0826A101"),
        (date(2026, 1, 9), "0826A901"),
        (date(2026, 1, 10), "0826AA01"),
        (date(2026, 6, 24), "0826FP01"),
        (date(2026, 9, 10), "0826JA01"),
        (date(2026, 12, 31), "0826MW01"),
        (date(2025, 12, 31), "0825MW01"),
        (date(2000, 2, 29), "0800BU01"),
        (date(2100, 1, 1), "0800A101"),
    ],
)
def test_datecode_day_month_and_year_edges(today: date, expected: str) -> None:
    assert DatecodeIds("08").next_id(_existing()[0], today) == expected


def test_datecode_day_prefix_is_the_nn_less_head() -> None:
    assert DatecodeIds("12").day_prefix(date(2026, 6, 24)) == "1226FP"
    assert DatecodeIds("12").day_prefix(date(2025, 1, 1)) == "1225A1"


def test_datecode_reuse_exhaustion() -> None:
    strategy = DatecodeIds("08")
    all_ids = [f"0826FP{number:02d}" for number in range(1, 100)]

    with pytest.raises(IdError, match="exhausted"):
        strategy.next_id(_existing(*all_ids)[0], TODAY)


def test_datecode_reuse_false_exhaustion() -> None:
    strategy = DatecodeIds("08", reuse=False)
    with pytest.raises(IdError, match="exhausted"):
        strategy.next_id(_existing("0826FP99")[0], TODAY)


def test_datecode_sequential_allocation_stays_unique_for_a_full_day() -> None:
    strategy = DatecodeIds("08")
    used: set[str] = set()
    for _ in range(99):
        value = strategy.next_id(lambda prefix: used, TODAY)
        assert re.fullmatch(strategy.pattern, value)
        assert value not in used
        used.add(value)
    assert used == {f"0826FP{number:02d}" for number in range(1, 100)}

    with pytest.raises(IdError):
        strategy.next_id(lambda prefix: used, TODAY)


def test_datecode_pattern_and_validate() -> None:
    strategy = DatecodeIds("08")

    for value in ("0826FP01", "0826A101", "0826MW99", "0825JA10"):
        assert strategy.validate(value), value
        assert re.fullmatch(strategy.pattern, value)

    for value in (
        "0826FP1",  # short NN
        "0826FP001",  # long NN
        "0826FP00",  # NN starts at 01
        "0926FP01",  # other prefix
        "0826IP01",  # month marker I does not exist
        "0826FI01",  # day marker I does not exist
        "0826F001",  # digit day marker out of range only allows 1-9 (0 is invalid)
        "0826fp01",  # lowercase
        " 0826FP01",
        "0826FP01 ",
        "0826FP01.1",  # that is an aliquot id, not a stock id
        "",
    ):
        assert not strategy.validate(value), value


def test_datecode_explicit_backfilled_id_is_accepted() -> None:
    """Explicit IDs are accepted for migration when they fit the pattern."""
    strategy = DatecodeIds("10")
    assert strategy.require_valid("1026FP42") == "1026FP42"


def test_require_valid_raises_id_error() -> None:
    with pytest.raises(IdError, match="malformed"):
        DatecodeIds("08").require_valid("0826FP1")


# --------------------------------------------------------------------------- sequence


def test_sequence_first_id() -> None:
    strategy = SequenceIds("S")
    callback, calls = _existing()

    assert strategy.next_id(callback, TODAY) == "S00001"
    assert calls == ["S"]


def test_sequence_is_monotonic_and_does_not_reuse_gaps() -> None:
    strategy = SequenceIds("S")
    assert strategy.next_id(_existing("S00001", "S00002", "S00005")[0], TODAY) == "S00006"


def test_sequence_ignores_other_prefixes_and_widths() -> None:
    strategy = SequenceIds("S", width=5)
    callback, _ = _existing("S00001", "X00009", "S0005", "S000012", "S0000A")
    assert strategy.next_id(callback, TODAY) == "S00002"


def test_sequence_custom_width() -> None:
    strategy = SequenceIds("PL", width=3)
    assert strategy.next_id(_existing("PL001", "PL009")[0], TODAY) == "PL010"
    assert strategy.validate("PL010")
    assert not strategy.validate("PL10")
    assert not strategy.validate("PL0010")


def test_sequence_exhaustion() -> None:
    with pytest.raises(IdError, match="exhausted"):
        SequenceIds("S").next_id(_existing("S99999")[0], TODAY)

    with pytest.raises(IdError, match="exhausted"):
        SequenceIds("S", width=2).next_id(_existing("S99")[0], TODAY)


def test_sequence_pattern_and_validate() -> None:
    strategy = SequenceIds("12", width=4)
    assert strategy.validate("120001")
    for value in ("1200011", "12001", "0001", "12", "12000A", ""):
        assert not strategy.validate(value), value


# --------------------------------------------------------------------------- config


def test_strategy_from_config_uuid() -> None:
    strategy = strategy_from_config({"strategy": "uuid"})
    assert isinstance(strategy, UuidIds)


def test_strategy_from_config_datecode() -> None:
    strategy = strategy_from_config({"strategy": "datecode", "prefix": "12"})
    assert isinstance(strategy, DatecodeIds)
    assert strategy.prefix == "12"
    assert strategy.reuse is True

    no_reuse = strategy_from_config({"strategy": "datecode", "prefix": 12, "reuse": False})
    assert isinstance(no_reuse, DatecodeIds)
    assert (no_reuse.prefix, no_reuse.reuse) == ("12", False)


def test_strategy_from_config_sequence() -> None:
    strategy = strategy_from_config({"strategy": "sequence", "prefix": "P", "width": 4})
    assert isinstance(strategy, SequenceIds)
    assert (strategy.prefix, strategy.width) == ("P", 4)

    default_width = strategy_from_config({"strategy": "sequence", "prefix": "P"})
    assert isinstance(default_width, SequenceIds)
    assert default_width.width == 5


@pytest.mark.parametrize("name", ["DATEcode", "date_code", "Date-Code", " datecode "])
def test_strategy_from_config_normalises_names(name: str) -> None:
    strategy = strategy_from_config({"strategy": name, "prefix": "08"})
    assert isinstance(strategy, DatecodeIds)


@pytest.mark.parametrize(
    ("config", "match"),
    [
        ({}, "strategy"),
        ({"strategy": ""}, "strategy"),
        ({"strategy": 3}, "strategy"),
        ({"strategy": "random"}, "unknown"),
        ({"strategy": "uuid", "prefix": "08"}, "does not accept"),
        ({"strategy": "datecode"}, "requires"),
        ({"strategy": "datecode", "prefix": "08", "width": 5}, "does not accept"),
        ({"strategy": "datecode", "prefix": ""}, "prefix"),
        ({"strategy": "datecode", "prefix": "a b"}, "prefix"),
        ({"strategy": "datecode", "prefix": "08", "reuse": "yes"}, "reuse"),
        ({"strategy": "sequence"}, "requires"),
        ({"strategy": "sequence", "prefix": "S", "width": 0}, "width"),
        ({"strategy": "sequence", "prefix": "S", "width": -1}, "width"),
        ({"strategy": "sequence", "prefix": "S", "width": True}, "width"),
        ("uuid", "mapping"),
    ],
)
def test_strategy_from_config_rejects_bad_config(config: object, match: str) -> None:
    with pytest.raises(UsageError, match=match):
        strategy_from_config(config)  # type: ignore[arg-type]


def test_strategy_from_config_does_not_mutate_the_input() -> None:
    config = {"strategy": "datecode", "prefix": "08"}
    strategy_from_config(config)
    assert config == {"strategy": "datecode", "prefix": "08"}


# --------------------------------------------------------------------------- aliquots


def test_aliquot_id_round_trip() -> None:
    assert aliquot_id("0826FP01", 1) == "0826FP01.1"
    assert aliquot_id("0826FP01", 12) == "0826FP01.12"
    assert parse_aliquot_id("0826FP01.1") == ("0826FP01", 1)
    assert parse_aliquot_id("0826FP01.12") == ("0826FP01", 12)


def test_aliquot_id_for_stock_ids_containing_dots() -> None:
    stock = "γPFD-0001.02.03.04-1"
    assert aliquot_id(stock, 3) == f"{stock}.3"
    assert parse_aliquot_id(f"{stock}.3") == (stock, 3)


@pytest.mark.parametrize("seq", [0, -1, True, 1.5, "1"])
def test_aliquot_id_rejects_bad_seq(seq: object) -> None:
    with pytest.raises(IdError):
        aliquot_id("0826FP01", seq)  # type: ignore[arg-type]


@pytest.mark.parametrize("stock", ["", "   ", None, 42])
def test_aliquot_id_rejects_bad_stock(stock: object) -> None:
    with pytest.raises(IdError):
        aliquot_id(stock, 1)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "value",
    ["", "0826FP01", ".1", "0826FP01.", "0826FP01.0", "0826FP01.01", "0826FP01.x", "0826FP01.1.2x"],
)
def test_parse_aliquot_id_rejects_malformed(value: str) -> None:
    with pytest.raises(IdError, match="malformed"):
        parse_aliquot_id(value)
    assert not is_aliquot_id(value)


def test_is_aliquot_id_accepts_valid() -> None:
    assert is_aliquot_id("0826FP01.1")
    assert is_aliquot_id("γPFD-0001.02.03.04-1.2")
    assert not is_aliquot_id(42)  # type: ignore[arg-type]


def test_next_aliquot_id() -> None:
    assert next_aliquot_id("0826FP01", []) == "0826FP01.1"
    assert (
        next_aliquot_id("0826FP01", ["0826FP01.1", "0826FP01.3", "0826FP02.9", "junk"])
        == "0826FP01.4"
    )


# --------------------------------------------------------------------------- errors


def test_id_error_is_a_ledger_error_and_value_error() -> None:
    assert issubclass(IdError, LedgerError)
    assert issubclass(IdError, ValueError)
    assert IdError.exit_code == 1
