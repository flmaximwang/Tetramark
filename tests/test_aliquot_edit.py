"""``tm aliquot edit``: correct a tube's recorded fields, keep the audit trail.

Tetramark-72s.  The editable fields are ``quantity``, ``unit`` and
``created_at`` (spec 8 dropped the tube's own ``notes``: an aliquot borrows its
stock's note).  Identity (``id``/``stock_id``/``seq``), ``label``, ``status``
and placement (``box_id``/``slot``) belong to other commands and are refused
with a pointer to the command that does own them.

A successful edit corrects the *current* record, never rewrites the earlier
``create``/``use``/``move`` events: it is one ``UPDATE``, one ``aliquot_event``
row with ``event = 'edit'`` and one Dolt commit.  The whole ``--set`` batch is
validated before anything is written, so one bad value leaves no trace.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal

import pytest
from typer.testing import CliRunner

from tetramark.cli.app import app
from tetramark.errors import LedgerError, UsageError

from conftest import PRESETS

BASE = PRESETS / "base"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, dolt_env: dict[str, str]) -> None:
    """Run the CLI with the same isolated Dolt/Git environment as the ledger."""
    for key, value in dolt_env.items():
        monkeypatch.setenv(key, value)


def commit_count(ledger) -> int:
    return int(ledger.driver.query("SELECT COUNT(*) AS n FROM dolt_log")[0]["n"])


def dirty(ledger) -> list[dict[str, object]]:
    return ledger.driver.query("SELECT * FROM dolt_status")


def aliquot_row(ledger, ref: str) -> dict[str, object]:
    rows = ledger.driver.query("SELECT * FROM `aliquot` WHERE `id` = %s", (ref,))
    assert len(rows) == 1
    return rows[0]


def aliquot_events(ledger, ref: str) -> list[dict[str, object]]:
    return ledger.driver.query(
        "SELECT * FROM `aliquot_event` WHERE `aliquot_id` = %s ORDER BY `id`",
        (ref,),
    )


def invoke(ledger, *args: str):
    return CliRunner().invoke(app, ["--ledger", str(ledger.path), *args])


def write(ledger, *args: str):
    before = commit_count(ledger)
    result = invoke(ledger, *args)
    assert result.exit_code == 0, result.output
    assert commit_count(ledger) == before + 1
    return result


def read(ledger, *args: str):
    result = invoke(ledger, *args)
    assert result.exit_code == 0, result.output
    return result


def make_stock(
    ledger,
    *,
    quantity: str | None = "100",
    unit: str | None = "ul",
    count: int = 1,
    name: str = "alpha",
) -> dict:
    """Create an item and one stock of ``count`` tubes via the CLI.

    ``quantity``/``unit`` default to 100 ul; pass both as ``None`` for a tube
    whose quantity is unknown.
    """
    entity = json.loads(
        write(ledger, "entity", "add", "item", "--name", name, "--json").output
    )
    args = [
        "stock",
        "add",
        "lot",
        "--entity",
        entity["id"],
        "--aliquots",
        str(count),
        "--json",
    ]
    if quantity is not None:
        args += ["--quantity", quantity]
    if unit is not None:
        args += ["--unit", unit]
    stock = json.loads(write(ledger, *args).output)
    return {"entity": entity, "stock": stock, "aliquots": stock["aliquots"]}


def assert_untouched(ledger, alq: str, before_row, before_events, before_commits):
    """A refused edit leaves the row, its events and the commit log alone."""
    assert aliquot_row(ledger, alq) == before_row
    assert aliquot_events(ledger, alq) == before_events
    assert commit_count(ledger) == before_commits
    assert dirty(ledger) == []


def as_datetime(value) -> datetime:
    """The record's ``created_at`` as a datetime (the driver reads it as text)."""
    return datetime.fromisoformat(str(value))


# ---------------------------------------------------------------------------
# The three editable fields
# ---------------------------------------------------------------------------


def test_cli_aliquot_edit_updates_the_three_editable_fields(
    cli_env: None, ledger_factory
) -> None:
    """quantity, unit and created_at are corrected in one edit."""
    ledger = ledger_factory([BASE], name="cli-aliquot-edit-fields", id_prefix="77")
    made = make_stock(ledger, quantity="100", unit="ul", count=2)
    first, second = made["aliquots"]
    first_row = aliquot_row(ledger, first)
    first_events = aliquot_events(ledger, first)
    old_events = aliquot_events(ledger, second)
    old_row = aliquot_row(ledger, second)
    old_created_at = old_row["created_at"]
    before = commit_count(ledger)

    edited = json.loads(
        write(
            ledger,
            "aliquot",
            "edit",
            second,
            "--set",
            "quantity=250",
            "--set",
            "unit=ml",
            "--set",
            "created_at=2026-06-24 12:34:56",
            "--json",
        ).output
    )

    assert edited["object"] == "aliquot"
    assert edited["id"] == second
    assert edited["stock_id"] == made["stock"]["id"]
    assert edited["seq"] == 2
    assert edited["status"] == old_row["status"]  # edit never changes status
    assert edited["label"] is None
    assert edited["box_id"] is None and edited["slot"] is None
    assert Decimal(str(edited["quantity"])) == Decimal("250")
    assert edited["unit"] == "ml"
    assert as_datetime(edited["created_at"]) == datetime(2026, 6, 24, 12, 34, 56)

    stored = aliquot_row(ledger, second)
    assert Decimal(str(stored["quantity"])) == Decimal("250")
    assert stored["unit"] == "ml"
    assert as_datetime(stored["created_at"]) == datetime(2026, 6, 24, 12, 34, 56)
    assert commit_count(ledger) == before + 1
    assert dirty(ledger) == []

    # One new event, and every earlier event is byte-for-byte what it was --
    # including the old created_at on the create event (events keep their at).
    after_events = aliquot_events(ledger, second)
    assert len(after_events) == len(old_events) + 1
    assert after_events[:-1] == old_events
    event = after_events[-1]
    assert event["event"] == "edit"
    assert event["amount"] is None
    assert event["unit"] is None
    assert event["from_box_id"] is None
    assert event["from_slot"] is None
    assert event["to_box_id"] is None
    assert event["to_slot"] is None
    assert event["old_status"] is None
    assert event["new_status"] is None
    assert not event["forced"]
    assert old_created_at != stored["created_at"]

    # The event's summary names what changed, even without -m/--message.
    message = str(event["message"])
    assert "quantity" in message and "unit" in message

    # The sibling tube is untouched by the edit.
    assert aliquot_row(ledger, first) == first_row
    assert aliquot_events(ledger, first) == first_events


def test_cli_aliquot_edit_clears_quantity_and_unit_together(
    cli_env: None, ledger_factory
) -> None:
    """``--set quantity=`` and ``--set unit=`` empty the pair, keeping history."""
    ledger = ledger_factory([BASE], name="cli-aliquot-edit-clear-pair", id_prefix="77")
    made = make_stock(ledger, quantity="100", unit="ul")
    alq = made["aliquots"][0]
    creation = aliquot_events(ledger, alq)

    cleared = json.loads(
        write(
            ledger,
            "aliquot",
            "edit",
            alq,
            "--set",
            "quantity=",
            "--set",
            "unit=",
            "--json",
        ).output
    )
    assert cleared["quantity"] is None
    assert cleared["unit"] is None
    stored = aliquot_row(ledger, alq)
    assert stored["quantity"] is None
    assert stored["unit"] is None
    # The create event keeps the quantity it was recorded with.
    assert Decimal(str(creation[0]["amount"])) == Decimal("100")
    assert creation[0]["unit"] == "ul"


def test_cli_aliquot_edit_refuses_the_removed_notes_field(
    cli_env: None, ledger_factory
) -> None:
    """Spec 8 dropped ``aliquot.notes``: it is an unknown editable field."""
    ledger = ledger_factory([BASE], name="cli-aliquot-edit-notes", id_prefix="77")
    made = make_stock(ledger, quantity="100", unit="ul")
    alq = made["aliquots"][0]
    before = (
        aliquot_row(ledger, alq),
        aliquot_events(ledger, alq),
        commit_count(ledger),
    )
    assert "notes" not in before[0]

    for args in (("--set", "notes=left of the rack"), ("--set", "notes=")):
        refused = invoke(ledger, "aliquot", "edit", alq, *args)
        assert isinstance(refused.exception, UsageError)
        assert refused.exception.exit_code == 2
        message = str(refused.exception)
        assert "unknown field(s) 'notes'" in message
        assert "editable fields: created_at, quantity, unit" in message
    assert_untouched(ledger, alq, *before)


def test_cli_aliquot_edit_accepts_a_t_separated_created_at(
    cli_env: None, ledger_factory
) -> None:
    """``created_at`` accepts the ISO form with a ``T`` separator as well."""
    ledger = ledger_factory([BASE], name="cli-aliquot-edit-t", id_prefix="77")
    made = make_stock(ledger, quantity="100", unit="ul")
    alq = made["aliquots"][0]

    edited = json.loads(
        write(
            ledger,
            "aliquot",
            "edit",
            alq,
            "--set",
            "created_at=2026-06-24T08:05:03",
            "--json",
        ).output
    )
    assert as_datetime(edited["created_at"]) == datetime(2026, 6, 24, 8, 5, 3)


# ---------------------------------------------------------------------------
# Type and boundary validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["0", "-1", "-0.5", "abc", "Infinity", "NaN", "   "])
def test_cli_aliquot_edit_refuses_an_invalid_quantity(
    cli_env: None, ledger_factory, value: str
) -> None:
    """quantity must be a finite number greater than zero; 0 points at use."""
    ledger = ledger_factory([BASE], name="cli-aliquot-edit-bad-quantity", id_prefix="77")
    made = make_stock(ledger, quantity="100", unit="ul")
    alq = made["aliquots"][0]
    before = (
        aliquot_row(ledger, alq),
        aliquot_events(ledger, alq),
        commit_count(ledger),
    )

    refused = invoke(ledger, "aliquot", "edit", alq, "--set", f"quantity={value}")
    assert isinstance(refused.exception, UsageError)
    assert refused.exception.exit_code == 2
    if value == "0":
        assert "tm aliquot use" in str(refused.exception)
    assert_untouched(ledger, alq, *before)


@pytest.mark.parametrize(
    "values",
    [
        ["quantity="],  # clearing only the quantity breaks the pair
        ["unit="],  # clearing only the unit breaks the pair
        ["unit=   "],  # whitespace-only is not an empty value
        ["unit=" + "x" * 33],  # unit is at most 32 characters
    ],
)
def test_cli_aliquot_edit_refuses_an_invalid_unit_or_pair(
    cli_env: None, ledger_factory, values: list[str]
) -> None:
    """unit stays non-empty/<=32 and quantity+unit must stay paired."""
    ledger = ledger_factory([BASE], name="cli-aliquot-edit-bad-unit", id_prefix="77")
    made = make_stock(ledger, quantity="100", unit="ul")
    alq = made["aliquots"][0]
    before = (
        aliquot_row(ledger, alq),
        aliquot_events(ledger, alq),
        commit_count(ledger),
    )

    args = [part for value in values for part in ("--set", value)]
    refused = invoke(ledger, "aliquot", "edit", alq, *args)
    assert isinstance(refused.exception, UsageError)
    assert refused.exception.exit_code == 2
    if values[0].startswith("unit=") and "x" * 33 in values[0]:
        assert "32" in str(refused.exception)
    assert_untouched(ledger, alq, *before)


@pytest.mark.parametrize(
    "value",
    [
        "not-a-date",
        "2026-02-30 00:00:00",
        "24/06/2026",
        "2026-06-24T12:34:56+08:00",  # timezone-aware values are refused
        "",  # created_at cannot be cleared
        "   ",
    ],
)
def test_cli_aliquot_edit_refuses_an_invalid_created_at(
    cli_env: None, ledger_factory, value: str
) -> None:
    """created_at takes a naive ISO datetime and cannot be cleared."""
    ledger = ledger_factory([BASE], name="cli-aliquot-edit-bad-created", id_prefix="77")
    made = make_stock(ledger, quantity="100", unit="ul")
    alq = made["aliquots"][0]
    before = (
        aliquot_row(ledger, alq),
        aliquot_events(ledger, alq),
        commit_count(ledger),
    )

    refused = invoke(ledger, "aliquot", "edit", alq, "--set", f"created_at={value}")
    assert isinstance(refused.exception, UsageError)
    assert refused.exception.exit_code == 2
    assert_untouched(ledger, alq, *before)


def test_cli_aliquot_edit_enforces_the_quantity_unit_pairing(
    cli_env: None, ledger_factory
) -> None:
    """Final values must be both set or both empty; the valid directions work."""
    ledger = ledger_factory([BASE], name="cli-aliquot-edit-pairing", id_prefix="77")
    made = make_stock(ledger, quantity="100", unit="ul")
    alq = made["aliquots"][0]
    empty = make_stock(ledger, quantity=None, unit=None, name="beta")
    empty_alq = empty["aliquots"][0]

    # One side may change alone as long as the final record stays paired.
    kept_unit = json.loads(
        write(ledger, "aliquot", "edit", alq, "--set", "quantity=50", "--json").output
    )
    assert Decimal(str(kept_unit["quantity"])) == Decimal("50")
    assert kept_unit["unit"] == "ul"

    before = (
        aliquot_row(ledger, empty_alq),
        aliquot_events(ledger, empty_alq),
        commit_count(ledger),
    )
    for value in ("quantity=5", "unit=ml"):
        refused = invoke(ledger, "aliquot", "edit", empty_alq, "--set", value)
        assert isinstance(refused.exception, UsageError)
        assert refused.exception.exit_code == 2
        assert_untouched(ledger, empty_alq, *before)

    # Setting both at once on an empty tube is the legal way to fill them in.
    filled = json.loads(
        write(
            ledger,
            "aliquot",
            "edit",
            empty_alq,
            "--set",
            "quantity=5",
            "--set",
            "unit=ml",
            "--json",
        ).output
    )
    assert Decimal(str(filled["quantity"])) == Decimal("5")
    assert filled["unit"] == "ml"


# ---------------------------------------------------------------------------
# Resolution: full id or alias, never an id prefix
# ---------------------------------------------------------------------------


def test_cli_aliquot_edit_resolves_a_full_id_and_an_alias(
    cli_env: None, ledger_factory
) -> None:
    """ALQ is a full ``<stock_id>.<seq>`` id or an alias."""
    ledger = ledger_factory([BASE], name="cli-aliquot-edit-refs", id_prefix="77")
    made = make_stock(ledger, quantity="100", unit="ul")
    alq = made["aliquots"][0]
    stock_id, seq = alq.rsplit(".", 1)
    assert stock_id == made["stock"]["id"]
    assert seq == "1"
    write(ledger, "alias", "add", "aliquot", alq, "tube-1")

    by_alias = json.loads(
        write(ledger, "aliquot", "edit", "tube-1", "--set", "unit=ml", "--json").output
    )
    assert by_alias["id"] == alq
    assert by_alias["unit"] == "ml"

    by_id = json.loads(
        write(ledger, "aliquot", "edit", alq, "--set", "quantity=250", "--json").output
    )
    assert by_id["id"] == alq
    assert Decimal(str(by_id["quantity"])) == Decimal("250")

    # The human line names the tube it edited.
    human = write(ledger, "aliquot", "edit", "tube-1", "--set", "created_at=2026-06-24 12:34:56")
    assert alq in human.output


def test_cli_aliquot_edit_refuses_an_id_prefix(
    cli_env: None, ledger_factory
) -> None:
    """An aliquot id is never abbreviated: a prefix is an unknown tube."""
    ledger = ledger_factory([BASE], name="cli-aliquot-edit-prefix", id_prefix="77")
    made = make_stock(ledger, quantity="100", unit="ul")
    alq = made["aliquots"][0]
    before = (
        aliquot_row(ledger, alq),
        aliquot_events(ledger, alq),
        commit_count(ledger),
    )

    for ref in (alq.rsplit(".", 1)[0], alq[:-1]):
        refused = invoke(ledger, "aliquot", "edit", ref, "--set", "unit=ml")
        assert isinstance(refused.exception, LedgerError)
        assert not isinstance(refused.exception, UsageError)
        assert refused.exit_code == 1
        assert "unknown aliquot" in str(refused.exception)
    assert_untouched(ledger, alq, *before)


# ---------------------------------------------------------------------------
# Fields owned by other commands
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", "other-stock.1"),
        ("stock_id", "other-stock"),
        ("seq", "2"),
        ("label", "new-label"),
        ("status", "consumed"),
        ("box_id", "some-box"),
        ("slot", "A1"),
    ],
)
def test_cli_aliquot_edit_refuses_fields_owned_by_other_commands(
    cli_env: None, ledger_factory, field: str, value: str
) -> None:
    """Identity, label, status and placement each point at their own command."""
    ledger = ledger_factory([BASE], name="cli-aliquot-edit-refused", id_prefix="77")
    made = make_stock(ledger, quantity="100", unit="ul")
    alq = made["aliquots"][0]
    before = (
        aliquot_row(ledger, alq),
        aliquot_events(ledger, alq),
        commit_count(ledger),
    )

    refused = invoke(ledger, "aliquot", "edit", alq, "--set", f"{field}={value}")
    assert isinstance(refused.exception, UsageError)
    assert refused.exception.exit_code == 2
    message = str(refused.exception)
    if field in {"id", "stock_id", "seq"}:
        assert "identity" in message or "cannot be edited" in message
    elif field == "label":
        assert "tm aliquot label" in message
        assert "--replace" in message
    elif field == "status":
        assert "tm aliquot status" in message
        assert "--force" in message and "-m" in message
    else:
        assert "tm aliquot move" in message
        assert "--box" in message and "--slot" in message and "-m" in message
    assert_untouched(ledger, alq, *before)


def test_cli_aliquot_edit_refuses_unknown_and_derived_fields(
    cli_env: None, ledger_factory
) -> None:
    """Unknown and derived names are usage errors that list the editable fields."""
    ledger = ledger_factory([BASE], name="cli-aliquot-edit-unknown", id_prefix="77")
    made = make_stock(ledger, quantity="100", unit="ul")
    alq = made["aliquots"][0]
    before = (
        aliquot_row(ledger, alq),
        aliquot_events(ledger, alq),
        commit_count(ledger),
    )

    for field in ("colour", "path", "in_stock"):
        refused = invoke(ledger, "aliquot", "edit", alq, "--set", f"{field}=x")
        assert isinstance(refused.exception, UsageError)
        assert refused.exception.exit_code == 2
        message = str(refused.exception)
        assert "unknown" in message.lower()
        assert field in message
        assert "quantity" in message  # the valid fields are listed
    assert_untouched(ledger, alq, *before)


def test_cli_aliquot_edit_refuses_a_no_op(cli_env: None, ledger_factory) -> None:
    """An edit whose final values all equal the current ones changes nothing."""
    ledger = ledger_factory([BASE], name="cli-aliquot-edit-noop", id_prefix="77")
    made = make_stock(ledger, quantity="100", unit="ul")
    alq = made["aliquots"][0]
    before = (
        aliquot_row(ledger, alq),
        aliquot_events(ledger, alq),
        commit_count(ledger),
    )

    for args in (
        ("--set", "quantity=100"),
        ("--set", "quantity=100", "--set", "unit=ul"),
    ):
        refused = invoke(ledger, "aliquot", "edit", alq, *args)
        assert isinstance(refused.exception, UsageError)
        assert refused.exception.exit_code == 2
        assert "nothing to change" in str(refused.exception)
    assert_untouched(ledger, alq, *before)


# ---------------------------------------------------------------------------
# Whole-batch validation: one bad --set means no write at all
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        ("--set", "quantity=5", "--set", "unit=" + "x" * 33),
        ("--set", "unit=ml", "--set", "created_at=tomorrow"),
        ("--set", "quantity=5", "--set", "colour=red"),
        ("--set", "unit=ml", "--set", "quantity=0"),
    ],
)
def test_cli_aliquot_edit_invalid_set_has_no_side_effect(
    cli_env: None, ledger_factory, args: tuple[str, ...]
) -> None:
    """A batch with one invalid value writes neither the valid fields nor an event."""
    ledger = ledger_factory([BASE], name="cli-aliquot-edit-atomic", id_prefix="77")
    made = make_stock(ledger, quantity="100", unit="ul")
    alq = made["aliquots"][0]
    before = (
        aliquot_row(ledger, alq),
        aliquot_events(ledger, alq),
        commit_count(ledger),
    )

    refused = invoke(ledger, "aliquot", "edit", alq, *args)
    assert isinstance(refused.exception, UsageError)
    assert refused.exception.exit_code == 2
    assert_untouched(ledger, alq, *before)


# ---------------------------------------------------------------------------
# Correcting after a use: current value only, history stays put
# ---------------------------------------------------------------------------


def test_cli_aliquot_edit_after_use_corrects_the_current_quantity(
    cli_env: None, ledger_factory
) -> None:
    """A post-use correction sets the remaining amount and keeps the old events."""
    ledger = ledger_factory([BASE], name="cli-aliquot-edit-after-use", id_prefix="77")
    made = make_stock(ledger, quantity="100", unit="ul")
    alq = made["aliquots"][0]

    write(ledger, "aliquot", "use", alq, "30", "ul", "-m", "prep")
    used = aliquot_row(ledger, alq)
    assert Decimal(str(used["quantity"])) == Decimal("70")
    old_events = aliquot_events(ledger, alq)
    assert [event["event"] for event in old_events] == ["create", "use"]
    before = commit_count(ledger)

    edited = json.loads(
        write(
            ledger,
            "aliquot",
            "edit",
            alq,
            "--set",
            "quantity=120",
            "-m",
            "scale was misread",
            "--json",
        ).output
    )
    assert Decimal(str(edited["quantity"])) == Decimal("120")
    assert edited["unit"] == "ul"
    assert edited["status"] == used["status"]  # only the amount is corrected
    assert commit_count(ledger) == before + 1

    after_events = aliquot_events(ledger, alq)
    assert after_events[:-1] == old_events
    assert [event["event"] for event in after_events] == ["create", "use", "edit"]
    edit_event = after_events[-1]
    message = str(edit_event["message"])
    assert "quantity" in message
    assert message.endswith("; reason: scale was misread")
    for column in (
        "amount",
        "unit",
        "from_box_id",
        "from_slot",
        "to_box_id",
        "to_slot",
        "old_status",
        "new_status",
    ):
        assert edit_event[column] is None
    assert not edit_event["forced"]


def test_cli_aliquot_edit_after_move_keeps_the_position(cli_env: None, ledger_factory) -> None:
    """Correcting a placed tube leaves its position, status and move event alone."""
    ledger = ledger_factory([BASE], name="cli-aliquot-edit-after-move", id_prefix="77")
    made = make_stock(ledger, quantity="100", unit="ul")
    alq = made["aliquots"][0]
    write(ledger, "box", "add", "grid96", "BOX")
    write(ledger, "aliquot", "move", alq, "--box", "BOX", "--slot", "A1", "-m", "stored it")
    placed = aliquot_row(ledger, alq)
    old_events = aliquot_events(ledger, alq)
    assert [event["event"] for event in old_events] == ["create", "move"]

    edited = json.loads(
        write(
            ledger,
            "aliquot",
            "edit",
            alq,
            "--set",
            "quantity=250",
            "--set",
            "unit=ml",
            "--json",
        ).output
    )
    assert Decimal(str(edited["quantity"])) == Decimal("250")
    assert edited["unit"] == "ml"
    assert edited["box_id"] == placed["box_id"]
    assert edited["slot"] == placed["slot"]
    assert edited["status"] == placed["status"]
    assert aliquot_row(ledger, alq)["box_id"] == placed["box_id"]
    assert aliquot_events(ledger, alq)[:-1] == old_events


# ---------------------------------------------------------------------------
# One Dolt commit, the edit event in history, and a clean check
# ---------------------------------------------------------------------------


def test_cli_aliquot_edit_is_one_commit_and_history_shows_the_edit(
    cli_env: None, ledger_factory
) -> None:
    """The correction is exactly one Dolt commit and one visible edit event."""
    ledger = ledger_factory([BASE], name="cli-aliquot-edit-history", id_prefix="77")
    made = make_stock(ledger, quantity="100", unit="ul")
    alq = made["aliquots"][0]
    before = commit_count(ledger)

    write(
        ledger,
        "aliquot",
        "edit",
        alq,
        "--set",
        "quantity=250",
        "-m",
        "typo",
    )
    assert commit_count(ledger) == before + 1

    history = json.loads(read(ledger, "history", alq, "--json").output)
    events = history["events"]
    assert [event["event"] for event in events] == ["create", "edit"]
    assert str(events[-1]["message"]).endswith("; reason: typo")

    text = read(ledger, "history", alq).output
    assert "edit" in text
    assert "typo" in text


def test_cli_aliquot_edit_leaves_tm_check_clean(cli_env: None, ledger_factory) -> None:
    """A corrected tube still passes ``tm check`` with no pending changes."""
    ledger = ledger_factory([BASE], name="cli-aliquot-edit-check", id_prefix="77")
    made = make_stock(ledger, quantity="100", unit="ul")
    alq = made["aliquots"][0]

    write(
        ledger,
        "aliquot",
        "edit",
        alq,
        "--set",
        "quantity=250",
        "--set",
        "unit=ml",
        "--set",
        "created_at=2026-06-24 12:34:56",
    )
    write(ledger, "aliquot", "edit", alq, "--set", "created_at=2026-07-01 00:00:00")
    write(ledger, "aliquot", "edit", alq, "--set", "quantity=", "--set", "unit=")

    report = json.loads(read(ledger, "check", "--json").output)
    assert report["ok"] is True
    assert report["failed"] == 0
    assert dirty(ledger) == []


# ---------------------------------------------------------------------------
# JSON and --dry-run
# ---------------------------------------------------------------------------


def test_cli_aliquot_edit_json_is_the_loaded_record(
    cli_env: None, ledger_factory
) -> None:
    """--json prints the loaded tube row with the edit's new values overlaid."""
    ledger = ledger_factory([BASE], name="cli-aliquot-edit-json", id_prefix="77")
    made = make_stock(ledger, quantity="100", unit="ul")
    alq = made["aliquots"][0]
    status_before = aliquot_row(ledger, alq)["status"]

    payload = json.loads(
        read(ledger, "aliquot", "edit", alq, "--set", "unit=ml", "--json").output
    )
    assert isinstance(payload, dict)
    assert set(payload) >= {
        "id",
        "stock_id",
        "seq",
        "status",
        "quantity",
        "unit",
        "label",
        "created_at",
        "box_id",
        "slot",
        "object",
    }
    assert payload["id"] == alq
    assert payload["object"] == "aliquot"
    assert payload["stock_id"] == made["stock"]["id"]
    assert payload["seq"] == 1
    assert payload["status"] == status_before
    assert Decimal(str(payload["quantity"])) == Decimal("100")
    assert payload["unit"] == "ml"


def test_cli_aliquot_edit_dry_run_writes_nothing(cli_env: None, ledger_factory) -> None:
    """--dry-run reports the planned row and SQL, and writes nothing."""
    ledger = ledger_factory([BASE], name="cli-aliquot-edit-dry", id_prefix="77")
    made = make_stock(ledger, quantity="100", unit="ul")
    alq = made["aliquots"][0]
    before = (
        aliquot_row(ledger, alq),
        aliquot_events(ledger, alq),
        commit_count(ledger),
    )

    result = invoke(
        ledger,
        "--dry-run",
        "aliquot",
        "edit",
        alq,
        "--set",
        "quantity=250",
        "--set",
        "unit=ml",
        "--json",
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["dry_run"] is True
    assert "UPDATE `aliquot`" in payload["sql"]
    assert payload["result"]["id"] == alq
    assert Decimal(str(payload["result"]["quantity"])) == Decimal("250")
    assert payload["result"]["unit"] == "ml"

    assert commit_count(ledger) == before[2]
    assert aliquot_row(ledger, alq) == before[0]
    assert aliquot_events(ledger, alq) == before[1]
    assert dirty(ledger) == []


# ---------------------------------------------------------------------------
# Core API
# ---------------------------------------------------------------------------


def test_core_aliquots_edit_records_the_change_and_its_event(
    ledger_factory,
) -> None:
    """``ledger.aliquots.edit(ref, *, values=(), message=None)`` is the core API."""
    ledger = ledger_factory([BASE], name="core-aliquot-edit", id_prefix="77")
    made = make_stock(ledger, quantity="100", unit="ul")
    alq = made["aliquots"][0]
    status_before = aliquot_row(ledger, alq)["status"]
    before = commit_count(ledger)

    edited = ledger.aliquots.edit(
        alq, values=["quantity=250", "unit=ml"], message="recount"
    )
    assert commit_count(ledger) == before + 1
    assert edited["id"] == alq
    assert edited["stock_id"] == made["stock"]["id"]
    assert edited["status"] == status_before
    assert Decimal(str(edited["quantity"])) == Decimal("250")
    assert edited["unit"] == "ml"

    events = aliquot_events(ledger, alq)
    assert [event["event"] for event in events] == ["create", "edit"]
    assert str(events[-1]["message"]).endswith("; reason: recount")
