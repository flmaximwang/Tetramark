"""``tm stock note``: write a note in ``$VISUAL``/``$EDITOR``, append or clear.

Tetramark-8km.2.  A note is the core ``notes`` column of an entity, stock or
box; this ticket ships the generic core writer (:func:`note_object`) and the
stock command.  One call is one write session and one Dolt commit, the summary
is ``edit(stock): <kind> <id>`` (ASCII only -- the note text never enters it),
and an empty result is refused unless ``--clear`` is given.

The editor is injected in these tests: the CLI tests monkeypatch
``tetramark.cli.stock.edit_plain_text`` and the Dolt-free editor tests pass a
``run=`` that rewrites the temp file, exactly like the form editor tests.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import typer.main
from typer.testing import CliRunner

from tetramark.cli import stock as stock_cli
from tetramark.cli.app import app
from tetramark.core.objects import note_object
from tetramark.errors import UsageError
from tetramark.form.editor import (
    SCISSORS,
    edit_plain_text,
    plain_editor_text,
    strip_editor_text,
)

from conftest import PRESETS

BASE = PRESETS / "base"

#: Two stock ids that share the ``7721A10`` prefix (ambiguity is deliberate).
AMBIGUOUS_IDS = ("7721A101", "7721A102")
AMBIGUOUS_PREFIX = "7721A10"


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


def stock_notes(ledger, ref: str):
    rows = ledger.driver.query("SELECT `notes` FROM `stock` WHERE `id` = %s", (ref,))
    assert len(rows) == 1
    return rows[0]["notes"]


def make_stock(ledger, name: str = "alpha") -> dict:
    """Create one item and one empty stock of it via the CLI."""
    entity = json.loads(
        write(ledger, "entity", "add", "item", "--name", name, "--json").output
    )
    return json.loads(
        write(
            ledger,
            "stock",
            "add",
            "lot",
            "--entity",
            entity["id"],
            "--aliquots",
            "0",
            "--json",
        ).output
    )


def fake_editor(monkeypatch: pytest.MonkeyPatch, result: str | None = None, seen=None):
    """Replace the CLI's editor: return ``result`` (identity when ``None``)."""

    def fake(value: str, **kwargs: object) -> str:
        if seen is not None:
            seen.append(value)
        return value if result is None else result

    monkeypatch.setattr(stock_cli, "edit_plain_text", fake)
    return fake


def latest_messages(ledger, count: int = 3) -> list[str]:
    return [
        str(row["message"])
        for row in ledger.driver.query(
            "SELECT `message` FROM `dolt_log` "
            "ORDER BY `date` DESC, `commit_order` DESC LIMIT %s",
            (count,),
        )
    ]


# ---------------------------------------------------------------------------
# The editor helper (Dolt-free)
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_plain_editor_text_puts_metadata_below_the_scissors_line() -> None:
    text = plain_editor_text("old note", description="free text")
    assert SCISSORS in text
    assert "# field: notes" in text
    assert "# description: free text" in text
    assert text.index(SCISSORS) > text.index("old note")
    assert strip_editor_text(text) == "old note"


@pytest.mark.fast
def test_plain_editor_text_starts_an_append_from_an_empty_buffer() -> None:
    """A bare ``--append`` buffer holds nothing above the scissors line."""
    text = plain_editor_text("", description="text to append")
    assert text.splitlines()[0] == ""
    assert strip_editor_text(text) == ""


@pytest.mark.fast
def test_edit_plain_text_runs_the_editor_and_strips_metadata() -> None:
    def fake_run(command: list[str], check: bool = False) -> None:
        Path(command[-1]).write_text(
            "edited\n# kept\n\n" + SCISSORS + "\n# field: notes\n",
            encoding="utf-8",
        )

    assert edit_plain_text("old", env={"EDITOR": "vi"}, run=fake_run) == (
        "edited\n# kept"
    )


@pytest.mark.fast
def test_note_command_surface_is_ref_text_append_clear_json() -> None:
    """TEXT is a positional argument; ``--append`` cannot take an optional value."""
    stock_command = typer.main.get_command(app).commands["stock"]
    note = stock_command.commands["note"]
    assert {parameter.name for parameter in note.params} == {
        "ref",
        "text",
        "append",
        "clear",
        "as_json",
    }
    by_name = {parameter.name: parameter for parameter in note.params}
    assert by_name["text"].param_type_name == "argument"  # positional, not an option
    assert "--append" in by_name["append"].opts
    assert "--clear" in by_name["clear"].opts


# ---------------------------------------------------------------------------
# The generic core writer
# ---------------------------------------------------------------------------


def test_note_object_writes_entity_stock_and_box_notes(
    cli_env: None, ledger_factory
) -> None:
    """One helper serves entity, stock and box; each call is one ``edit`` commit."""
    ledger = ledger_factory([BASE], name="note-object-kinds", id_prefix="77")
    entity = ledger.entities.add("item", name="alpha")
    stock = ledger.stocks.add("lot", entity=entity["id"], aliquots=0)
    box = ledger.boxes.add("rack", "R1")

    before = commit_count(ledger)
    entity_record = note_object(ledger, "entity", entity["id"], text="entity note")
    stock_record = note_object(ledger, "stock", stock["id"], text="stock note")
    # A box resolves by its human name first, exactly like ``box edit``.
    box_record = note_object(ledger, "box", "R1", append="box note")

    assert commit_count(ledger) == before + 3
    assert entity_record["object"] == "entity"
    assert entity_record["notes"] == "entity note"
    assert stock_record["object"] == "stock"
    assert stock_record["notes"] == "stock note"
    assert box_record["object"] == "box"
    assert box_record["id"] == box["id"]
    assert box_record["notes"] == "box note"
    assert latest_messages(ledger, 3) == [
        f"edit(box): rack {box['id']}",
        f"edit(stock): lot {stock['id']}",
        f"edit(entity): item {entity['id']}",
    ]
    assert ledger.driver.query(
        "SELECT `notes` FROM `box` WHERE `id` = %s", (box["id"],)
    ) == [{"notes": "box note"}]


def test_note_object_appends_and_clears(cli_env: None, ledger_factory) -> None:
    ledger = ledger_factory([BASE], name="note-object-append", id_prefix="77")
    stock = make_stock(ledger)

    # An empty note takes the addition alone: no leading newline.
    assert ledger.stocks.note(stock["id"], append="first")["notes"] == "first"
    assert ledger.stocks.note(stock["id"], append="second")["notes"] == (
        "first\nsecond"
    )
    assert ledger.stocks.note(stock["id"], clear=True)["notes"] is None
    assert stock_notes(ledger, stock["id"]) is None


def test_note_object_refuses_aliquots_and_ambiguous_modes(
    cli_env: None, ledger_factory
) -> None:
    ledger = ledger_factory([BASE], name="note-object-refusals", id_prefix="77")
    stock = make_stock(ledger)
    before = commit_count(ledger)

    with pytest.raises(UsageError, match="only entity, stock and box"):
        note_object(ledger, "aliquot", stock["id"], text="x")
    with pytest.raises(UsageError, match="exactly one"):
        note_object(ledger, "stock", stock["id"])
    with pytest.raises(UsageError, match="exactly one"):
        note_object(ledger, "stock", stock["id"], text="x", clear=True)
    with pytest.raises(UsageError, match="--clear"):
        note_object(ledger, "stock", stock["id"], text="   ")

    assert commit_count(ledger) == before
    assert stock_notes(ledger, stock["id"]) is None
    assert dirty(ledger) == []


def test_note_object_ambiguous_id_prefix_is_a_usage_error(ledger_factory) -> None:
    ledger = ledger_factory([BASE], name="note-object-ambiguous", id_prefix="77")
    entity = ledger.entities.add("item", name="alpha")
    for stock_id in AMBIGUOUS_IDS:
        ledger.stocks.add("lot", entity=entity["id"], aliquots=0, id=stock_id)
    before = commit_count(ledger)

    with pytest.raises(UsageError, match="ambiguous") as raised:
        ledger.stocks.note(AMBIGUOUS_PREFIX, append="x")
    message = str(raised.value)
    assert all(stock_id in message for stock_id in AMBIGUOUS_IDS)

    assert commit_count(ledger) == before
    for stock_id in AMBIGUOUS_IDS:
        assert stock_notes(ledger, stock_id) is None


# ---------------------------------------------------------------------------
# The CLI
# ---------------------------------------------------------------------------


def test_cli_stock_note_saves_the_editor_result_in_one_commit(
    cli_env: None, ledger_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = ledger_factory([BASE], name="cli-note-save", id_prefix="77")
    stock = make_stock(ledger)
    fake_editor(monkeypatch, result="line one\nline two")
    before = commit_count(ledger)

    result = read(ledger, "stock", "note", stock["id"], "--json")

    assert commit_count(ledger) == before + 1
    assert json.loads(result.output)["notes"] == "line one\nline two"
    assert stock_notes(ledger, stock["id"]) == "line one\nline two"
    assert latest_messages(ledger, 1) == [f"edit(stock): lot {stock['id']}"]


def test_cli_stock_note_unchanged_prints_no_changes_and_writes_nothing(
    cli_env: None, ledger_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = ledger_factory([BASE], name="cli-note-unchanged", id_prefix="77")
    stock = make_stock(ledger)
    ledger.stocks.note(stock["id"], text="keep me")
    fake_editor(monkeypatch)  # identity: the buffer comes back unchanged
    before = commit_count(ledger)

    result = read(ledger, "stock", "note", stock["id"], "--json")

    assert result.output.strip() == "no changes"
    assert commit_count(ledger) == before
    assert stock_notes(ledger, stock["id"]) == "keep me"
    assert dirty(ledger) == []


def test_cli_stock_note_untouched_empty_note_is_no_changes(
    cli_env: None, ledger_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty buffer over an empty note is unchanged, not an empty result."""
    ledger = ledger_factory([BASE], name="cli-note-empty-untouched", id_prefix="77")
    stock = make_stock(ledger)
    fake_editor(monkeypatch)  # identity: "" comes back unchanged
    before = commit_count(ledger)

    result = read(ledger, "stock", "note", stock["id"])

    assert result.output.strip() == "no changes"
    assert commit_count(ledger) == before
    assert stock_notes(ledger, stock["id"]) is None


def test_cli_stock_note_empty_result_without_clear_is_refused(
    cli_env: None, ledger_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = ledger_factory([BASE], name="cli-note-empty", id_prefix="77")
    stock = make_stock(ledger)
    ledger.stocks.note(stock["id"], text="keep me")
    fake_editor(monkeypatch, result="")
    before = commit_count(ledger)

    result = invoke(ledger, "stock", "note", stock["id"])

    assert isinstance(result.exception, UsageError)
    assert result.exception.exit_code == 2
    assert "--clear" in str(result.exception)
    assert commit_count(ledger) == before
    assert stock_notes(ledger, stock["id"]) == "keep me"
    assert dirty(ledger) == []


def test_cli_stock_note_clear_sets_notes_to_null(
    cli_env: None, ledger_factory
) -> None:
    ledger = ledger_factory([BASE], name="cli-note-clear", id_prefix="77")
    stock = make_stock(ledger)
    ledger.stocks.note(stock["id"], text="remove me")

    cleared = json.loads(
        write(ledger, "stock", "note", stock["id"], "--clear", "--json").output
    )

    assert cleared["notes"] is None
    assert stock_notes(ledger, stock["id"]) is None
    assert latest_messages(ledger, 1) == [f"edit(stock): lot {stock['id']}"]


def test_cli_stock_note_append_to_empty_and_non_empty_notes(
    cli_env: None, ledger_factory
) -> None:
    ledger = ledger_factory([BASE], name="cli-note-append", id_prefix="77")
    stock = make_stock(ledger)

    first = json.loads(
        write(
            ledger, "stock", "note", stock["id"], "--append", "first", "--json"
        ).output
    )
    assert first["notes"] == "first"

    second = json.loads(
        write(
            ledger,
            "stock",
            "note",
            stock["id"],
            "second",
            "--append",
            "--json",
        ).output
    )
    assert second["notes"] == "first\nsecond"
    assert stock_notes(ledger, stock["id"]) == "first\nsecond"


def test_cli_stock_note_bare_append_starts_from_an_empty_buffer(
    cli_env: None, ledger_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = ledger_factory([BASE], name="cli-note-bare-append", id_prefix="77")
    stock = make_stock(ledger)
    ledger.stocks.note(stock["id"], text="keep")
    seen: list[str] = []
    fake_editor(monkeypatch, result="more", seen=seen)

    result = read(ledger, "stock", "note", stock["id"], "--append", "--json")

    assert seen == [""]
    assert json.loads(result.output)["notes"] == "keep\nmore"
    assert stock_notes(ledger, stock["id"]) == "keep\nmore"


def test_cli_stock_note_bare_append_with_an_empty_editor_writes_nothing(
    cli_env: None, ledger_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = ledger_factory([BASE], name="cli-note-bare-append-empty", id_prefix="77")
    stock = make_stock(ledger)
    ledger.stocks.note(stock["id"], text="keep")
    fake_editor(monkeypatch, result="")
    before = commit_count(ledger)

    result = read(ledger, "stock", "note", stock["id"], "--append")

    assert result.output.strip() == "no changes"
    assert commit_count(ledger) == before
    assert stock_notes(ledger, stock["id"]) == "keep"


def test_cli_stock_note_global_json_and_dry_run(
    cli_env: None, ledger_factory
) -> None:
    ledger = ledger_factory([BASE], name="cli-note-json-dry-run", id_prefix="77")
    stock = make_stock(ledger)
    ledger.stocks.note(stock["id"], text="keep")
    before = commit_count(ledger)

    dry = read(
        ledger,
        "--dry-run",
        "stock",
        "note",
        stock["id"],
        "--append",
        "added",
        "--json",
    )
    plan = json.loads(dry.output)
    assert plan["dry_run"] is True
    assert "UPDATE `stock`" in plan["sql"]
    assert "`notes`" in plan["sql"]
    assert plan["result"]["notes"] == "keep\nadded"

    assert commit_count(ledger) == before
    assert stock_notes(ledger, stock["id"]) == "keep"


def test_cli_stock_note_global_json_flag_is_honoured(
    cli_env: None, ledger_factory
) -> None:
    ledger = ledger_factory([BASE], name="cli-note-global-json", id_prefix="77")
    stock = make_stock(ledger)

    result = read(ledger, "--json", "stock", "note", stock["id"], "--clear")

    assert json.loads(result.output)["notes"] is None


def test_cli_stock_note_rejects_misused_flags(
    cli_env: None, ledger_factory
) -> None:
    ledger = ledger_factory([BASE], name="cli-note-flags", id_prefix="77")
    stock = make_stock(ledger)
    before = commit_count(ledger)

    text_only = invoke(ledger, "stock", "note", stock["id"], "orphan text")
    assert isinstance(text_only.exception, UsageError)
    assert "--append" in str(text_only.exception)

    clear_with_append = invoke(
        ledger, "stock", "note", stock["id"], "--append", "--clear"
    )
    assert isinstance(clear_with_append.exception, UsageError)
    assert "--clear" in str(clear_with_append.exception)

    clear_with_text = invoke(
        ledger, "stock", "note", stock["id"], "orphan text", "--append", "--clear"
    )
    assert isinstance(clear_with_text.exception, UsageError)

    empty_append = invoke(ledger, "stock", "note", stock["id"], "--append", "")
    assert isinstance(empty_append.exception, UsageError)
    assert "--clear" in str(empty_append.exception)

    assert commit_count(ledger) == before
    assert stock_notes(ledger, stock["id"]) is None
    assert dirty(ledger) == []
