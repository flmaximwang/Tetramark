"""Fast contract tests for the shared event-line renderer (spec §6.6).

The renderer is pure Python, so these tests run in the Dolt-free lane.  They
pin the settled grammar: the message token last and JSON-escaped onto exactly
one physical line, the resolved/unresolved place forms, the pre-0.8.32 legacy
fallback, the unchanged actor token and the 16-column history row.
"""

from __future__ import annotations

import csv
import io
import json
import uuid

import pytest

from tetramark.ops.history import HISTORY_COLUMNS, event_line, history_rows

pytestmark = pytest.mark.fast


def _event(**overrides: object) -> dict[str, object]:
    """One complete event dict with stable values; overrides replace fields."""
    event: dict[str, object] = {
        "at": "2026-10-26 11:11:44",
        "event": "create",
        "amount": "50.000000",
        "unit": "ml",
        "from_box_id": None,
        "from_box": None,
        "from_box_resolved": None,
        "from_slot": None,
        "to_box_id": "930b7cc2-6f06-4dee-aac5-161f329bfe6e",
        "to_box": "Rack-3",
        "to_box_resolved": True,
        "to_slot": "B4",
        "old_status": None,
        "new_status": "stored",
        "forced": False,
        "actor": "alice",
        "message": None,
    }
    event.update(overrides)
    return event


def test_message_is_the_last_token_and_empty_or_null_adds_none() -> None:
    """F1: the message is last; NULL and the empty string add no token."""
    line = event_line(_event(message="re-rack tubes"))
    assert line.endswith(' message="re-rack tubes"')
    assert line.count("message=") == 1

    assert "message=" not in event_line(_event(message=None))
    assert "message=" not in event_line(_event(message=""))
    # The stored text is not trimmed: spaces stay inside the quotes.
    assert event_line(_event(message="  spaced  ")).endswith(
        'message="  spaced  "'
    )


def test_message_escaping_keeps_one_physical_line_and_round_trips() -> None:
    """F2: JSON quoting, line separators and CJK survive a round trip."""
    message = 'line1\nline2\r\t\b\f"\\\x00\x1c\x85\u2028\u2029 中文'
    line = event_line(_event(message=message))

    assert len(line.splitlines()) == 1
    assert "\n" not in line and "\r" not in line
    assert "\u0085" not in line
    assert "\u2028" not in line and "\u2029" not in line
    assert "\\u0085" in line and "\\u2028" in line and "\\u2029" in line
    assert "中文" in line

    encoded = line.split("message=", 1)[1]
    assert json.loads(encoded) == message


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        (
            _event(),
            "2026-10-26 11:11:44 create amount=50.000000 ml "
            "to=Rack-3:B4 status=stored by=alice",
        ),
        (
            _event(
                at="2026-11-04 09:20:44",
                event="move",
                aliquot_id="1026KR08.1",
                amount=None,
                unit=None,
                from_box_id="930b7cc2-6f06-4dee-aac5-161f329bfe6e",
                from_box="Rack-3",
                from_box_resolved=True,
                from_slot="B4",
                to_box_id="b0b9c39a-0f9f-4f4f-9d48-2c60c9c9e0f1",
                to_box="Freezer-B",
                to_box_resolved=True,
                to_slot="A2",
                old_status="stored",
                new_status="stored",
            ),
            "2026-11-04 09:20:44 move 1026KR08.1 from=Rack-3:B4 "
            "to=Freezer-B:A2 status=stored by=alice",
        ),
        (
            _event(
                at="2026-11-05 00:08:28",
                event="use",
                aliquot_id="1026KR08.1",
                amount="50.000000",
                from_box_id="930b7cc2-6f06-4dee-aac5-161f329bfe6e",
                from_box="Rack-3",
                from_box_resolved=True,
                from_slot="A2",
                to_box_id=None,
                to_box=None,
                to_box_resolved=None,
                to_slot=None,
                old_status="stored",
                new_status="consumed",
                forced=True,
            ),
            "2026-11-05 00:08:28 use 1026KR08.1 amount=50.000000 ml "
            "from=Rack-3:A2 (forced) status=stored->consumed by=alice",
        ),
        (
            _event(
                at="2026-11-05 00:09:00",
                event="status",
                aliquot_id="1026KR08.1",
                amount=None,
                unit=None,
                from_box_id="930b7cc2-6f06-4dee-aac5-161f329bfe6e",
                from_box="Rack-3",
                from_box_resolved=True,
                from_slot="A2",
                to_box_id=None,
                to_box=None,
                to_box_resolved=None,
                to_slot=None,
                old_status="stored",
                new_status="missing",
            ),
            "2026-11-05 00:09:00 status 1026KR08.1 from=Rack-3:A2 "
            "status=stored->missing by=alice",
        ),
    ],
)
def test_line_is_byte_identical_to_0_8_29_without_a_message(
    event: dict[str, object], expected: str
) -> None:
    """F3: the pre-message tokens keep their exact 0.8.29 rendering."""
    assert event_line(event) == expected


def test_place_resolution_keeps_the_full_stored_id_and_legacy_fallback() -> None:
    """F4: true -> live name, false -> full-id unresolved, absent -> legacy."""
    stored = "930b7cc2-6f06-4dee-aac5-161f329bfe6e"

    resolved = event_line(
        _event(
            from_box_id=stored,
            from_box="Rack-3",
            from_box_resolved=True,
            from_slot="A1",
        )
    )
    assert "from=Rack-3:A1" in resolved

    unresolved = event_line(
        _event(
            from_box_id=stored,
            from_box=stored,
            from_box_resolved=False,
            from_slot="A1",
        )
    )
    assert f"from=[unresolved box {stored}]:A1" in unresolved
    assert "deleted" not in unresolved

    slot_only = event_line(
        _event(
            from_box_id=None,
            from_box=None,
            from_box_resolved=None,
            from_slot="A1",
        )
    )
    assert "from=A1" in slot_only
    assert "from=:A1" not in slot_only

    both_null = event_line(
        _event(
            from_box_id=None,
            from_box=None,
            from_box_resolved=None,
            from_slot=None,
        )
    )
    assert "from=" not in both_null

    legacy_name = _event(
        from_box_id=stored,
        from_box="Old-Name",
        from_slot="A1",
    )
    del legacy_name["from_box_resolved"]
    assert "from=Old-Name:A1" in event_line(legacy_name)

    legacy_id = _event(
        from_box_id=stored,
        from_box=None,
        from_slot="A1",
    )
    del legacy_id["from_box_resolved"]
    assert f"from={stored}:A1" in event_line(legacy_id)


def test_actor_rendering_is_raw_and_unchanged() -> None:
    """F5: the actor token is the stored string, with no quoting or expansion."""
    line = event_line(_event(actor="Ledger Factory 中文"))
    assert "by=Ledger Factory 中文" in line
    assert 'by="' not in line


def test_history_rows_keep_sixteen_columns_and_the_raw_message() -> None:
    """F6: the CSV row shape is unchanged and the message is raw text."""
    message = 'line1\n"quoted", 中文'
    result = {"events": [_event(message=message)], "commits": []}

    rows = history_rows(result)

    assert len(HISTORY_COLUMNS) == 16
    assert list(rows[0]) == list(HISTORY_COLUMNS)
    assert rows[0]["message"] == message

    # The writer performs the only quoting; reading the CSV back restores the
    # stored message byte for byte, newline included.
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(HISTORY_COLUMNS))
    writer.writeheader()
    writer.writerow(rows[0])
    parsed = list(csv.DictReader(io.StringIO(buffer.getvalue())))
    assert parsed[0]["message"] == message


def test_unresolved_place_uses_the_whole_id_not_a_prefix() -> None:
    """F4: two ids sharing a prefix must not collapse to the same text."""
    first = str(uuid.uuid4())
    second = str(uuid.uuid4())
    # Only the first 8 characters are shared by construction in the report;
    # the renderer always prints the complete stored id.
    assert len(first) == 36 and len(second) == 36
    line = event_line(
        _event(
            from_box_id=first,
            from_box=first,
            from_box_resolved=False,
            from_slot="A1",
        )
    )
    assert first in line
    assert first[:8] + "]" not in line
