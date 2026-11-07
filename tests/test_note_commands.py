"""``tm entity|stock|box note``: editor, ``--append`` and ``--clear``.

Tetramark-8km.3.  A note is the core ``notes`` column of an entity, stock or
box; the three commands share one implementation
(:mod:`tetramark.cli.notes`) over the generic
:func:`tetramark.core.objects.note_object`, so their behaviour is identical and
this module parametrizes the shared cases over all three kinds instead of
copying them.  One call is one write session and one Dolt commit, the summary
is ``edit(<object>): <kind> <id>`` (ASCII only -- the note text never enters
it), and an empty result is refused unless ``--clear`` is given.

The editor is injected in these tests: the CLI tests monkeypatch
``tetramark.cli.notes.edit_plain_text`` and the Dolt-free editor tests pass a
``run=`` that rewrites the temp file, exactly like the form editor tests.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import typer.main
from typer.testing import CliRunner

from tetramark.cli import notes as notes_cli
from tetramark.cli.app import app
from tetramark.core.objects import note_object, read_note
from tetramark.errors import UsageError
from tetramark.form.editor import (
    SCISSORS,
    edit_plain_text,
    plain_editor_text,
    strip_editor_text,
)

from conftest import PRESETS

BASE = PRESETS / "base"

#: The object kinds that have a note command (aliquots deliberately do not).
OBJECT_KINDS = ("entity", "stock", "box")

#: Two entity ids that share the ``aaaaaaaa`` prefix (ambiguity is deliberate).
ENTITY_IDS = (
    "aaaaaaaa-1111-4111-8111-111111111111",
    "aaaaaaaa-1111-4111-8111-222222222222",
)
ENTITY_PREFIX = "aaaaaaaa"

#: Two stock ids that share the ``7721A10`` prefix (ambiguity is deliberate).
STOCK_IDS = ("7721A101", "7721A102")
STOCK_PREFIX = "7721A10"


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


def object_notes(ledger, object_type: str, obj: dict) -> str | None:
    rows = ledger.driver.query(
        f"SELECT `notes` FROM `{object_type}` WHERE `id` = %s",
        (obj["record"]["id"],),
    )
    assert len(rows) == 1
    return rows[0]["notes"]


def make_object(ledger, object_type: str) -> dict:
    """Create one object of the kind via the CLI; return its ref and record.

    The ref is what a user would type: the id for an entity/stock and the
    human name for a box, so every shared test also covers the box's
    name-first resolution.
    """
    if object_type == "entity":
        record = json.loads(
            write(
                ledger, "entity", "add", "item", "--name", "alpha", "--json"
            ).output
        )
    elif object_type == "stock":
        entity = json.loads(
            write(
                ledger, "entity", "add", "item", "--name", "alpha", "--json"
            ).output
        )
        record = json.loads(
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
    else:
        record = json.loads(
            write(ledger, "box", "add", "rack", "alpha", "--json").output
        )
    ref = record["name"] if object_type == "box" else record["id"]
    return {"ref": ref, "record": record}


def fake_editor(monkeypatch: pytest.MonkeyPatch, result: str | None = None, seen=None):
    """Replace the shared editor: return ``result`` (identity when ``None``)."""

    def fake(value: str, **kwargs: object) -> str:
        if seen is not None:
            seen.append(value)
        return value if result is None else result

    monkeypatch.setattr(notes_cli, "edit_plain_text", fake)
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


def expected_line(object_type: str, obj: dict) -> str:
    """The human line, as each group's ``edit`` command prints it."""
    record = obj["record"]
    if object_type == "box":
        return f"Edited box {record['name']} ({record['kind']})"
    return f"Edited {object_type} {record['kind']} {record['id']}"


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
@pytest.mark.parametrize("object_type", OBJECT_KINDS)
def test_note_command_surface_is_ref_text_append_clear_json(
    object_type: str,
) -> None:
    """TEXT is a positional argument; ``--append`` cannot take an optional value."""
    note = typer.main.get_command(app).commands[object_type].commands["note"]
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
# The generic core writer and its light read
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
    # The light read resolves the same way and returns the stored note.
    assert read_note(ledger, "entity", entity["id"]) == "entity note"
    assert read_note(ledger, "stock", stock["id"]) == "stock note"
    assert read_note(ledger, "box", "R1") == "box note"
    assert note_object(ledger, "box", "R1", clear=True)["notes"] is None
    assert read_note(ledger, "box", "R1") is None
    assert ledger.driver.query(
        "SELECT `notes` FROM `box` WHERE `id` = %s", (box["id"],)
    ) == [{"notes": None}]


def test_note_object_refuses_aliquots_and_ambiguous_modes(ledger_factory) -> None:
    ledger = ledger_factory([BASE], name="note-object-refusals", id_prefix="77")
    entity = ledger.entities.add("item", name="alpha")
    stock = ledger.stocks.add("lot", entity=entity["id"], aliquots=0)
    before = commit_count(ledger)

    with pytest.raises(UsageError, match="only entity, stock and box"):
        note_object(ledger, "aliquot", stock["id"], text="x")
    with pytest.raises(UsageError, match="only entity, stock and box"):
        read_note(ledger, "aliquot", stock["id"])
    with pytest.raises(UsageError, match="exactly one"):
        note_object(ledger, "stock", stock["id"])
    with pytest.raises(UsageError, match="exactly one"):
        note_object(ledger, "stock", stock["id"], text="x", clear=True)
    with pytest.raises(UsageError, match="--clear"):
        note_object(ledger, "stock", stock["id"], text="   ")

    assert commit_count(ledger) == before
    assert read_note(ledger, "stock", stock["id"]) is None
    assert dirty(ledger) == []


@pytest.mark.parametrize(
    ("object_type", "ids", "prefix"),
    [
        ("entity", ENTITY_IDS, ENTITY_PREFIX),
        ("stock", STOCK_IDS, STOCK_PREFIX),
    ],
    ids=["entity", "stock"],
)
def test_note_ambiguous_id_prefix_is_a_usage_error(
    cli_env: None, ledger_factory, object_type: str, ids, prefix: str
) -> None:
    ledger = ledger_factory(
        [BASE], name=f"note-object-ambiguous-{object_type}", id_prefix="77"
    )
    if object_type == "entity":
        for index, object_id in enumerate(ids):
            ledger.entities.add("item", name=f"item {index}", id=object_id)
    else:
        entity = ledger.entities.add("item", name="alpha")
        for object_id in ids:
            ledger.stocks.add(
                "lot", entity=entity["id"], aliquots=0, id=object_id
            )
    before = commit_count(ledger)

    with pytest.raises(UsageError, match="ambiguous") as raised:
        note_object(ledger, object_type, prefix, append="x")
    message = str(raised.value)
    assert all(object_id in message for object_id in ids)
    with pytest.raises(UsageError, match="ambiguous"):
        read_note(ledger, object_type, prefix)

    assert commit_count(ledger) == before
    for object_id in ids:
        rows = ledger.driver.query(
            f"SELECT `notes` FROM `{object_type}` WHERE `id` = %s", (object_id,)
        )
        assert rows == [{"notes": None}]


# ---------------------------------------------------------------------------
# The CLI: the shared cases, parametrized over all three kinds
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("object_type", OBJECT_KINDS)
def test_cli_note_saves_the_editor_result_in_one_commit(
    cli_env: None, ledger_factory, object_type: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = ledger_factory(
        [BASE], name=f"cli-note-save-{object_type}", id_prefix="77"
    )
    obj = make_object(ledger, object_type)
    fake_editor(monkeypatch, result="line one\nline two")
    before = commit_count(ledger)

    result = read(ledger, object_type, "note", obj["ref"], "--json")

    assert commit_count(ledger) == before + 1
    assert json.loads(result.output)["notes"] == "line one\nline two"
    assert object_notes(ledger, object_type, obj) == "line one\nline two"
    assert latest_messages(ledger, 1) == [
        f"edit({object_type}): {obj['record']['kind']} {obj['record']['id']}"
    ]


@pytest.mark.parametrize("object_type", OBJECT_KINDS)
def test_cli_note_prints_the_group_edit_line(
    cli_env: None, ledger_factory, object_type: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = ledger_factory(
        [BASE], name=f"cli-note-line-{object_type}", id_prefix="77"
    )
    obj = make_object(ledger, object_type)
    fake_editor(monkeypatch, result="saved")

    result = read(ledger, object_type, "note", obj["ref"])

    assert result.output.strip() == expected_line(object_type, obj)


@pytest.mark.parametrize("object_type", OBJECT_KINDS)
def test_cli_note_unchanged_prints_no_changes_and_writes_nothing(
    cli_env: None, ledger_factory, object_type: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = ledger_factory(
        [BASE], name=f"cli-note-unchanged-{object_type}", id_prefix="77"
    )
    obj = make_object(ledger, object_type)
    write(ledger, object_type, "note", obj["ref"], "--append", "keep me")
    fake_editor(monkeypatch)  # identity: the buffer comes back unchanged
    before = commit_count(ledger)

    result = read(ledger, object_type, "note", obj["ref"], "--json")

    assert result.output.strip() == "no changes"
    assert commit_count(ledger) == before
    assert object_notes(ledger, object_type, obj) == "keep me"
    assert dirty(ledger) == []


@pytest.mark.parametrize("object_type", OBJECT_KINDS)
def test_cli_note_untouched_empty_note_is_no_changes(
    cli_env: None, ledger_factory, object_type: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty buffer over an empty note is unchanged, not an empty result."""
    ledger = ledger_factory(
        [BASE], name=f"cli-note-empty-untouched-{object_type}", id_prefix="77"
    )
    obj = make_object(ledger, object_type)
    fake_editor(monkeypatch)  # identity: "" comes back unchanged
    before = commit_count(ledger)

    result = read(ledger, object_type, "note", obj["ref"])

    assert result.output.strip() == "no changes"
    assert commit_count(ledger) == before
    assert object_notes(ledger, object_type, obj) is None


@pytest.mark.parametrize("object_type", OBJECT_KINDS)
def test_cli_note_empty_result_without_clear_is_refused(
    cli_env: None, ledger_factory, object_type: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = ledger_factory(
        [BASE], name=f"cli-note-empty-{object_type}", id_prefix="77"
    )
    obj = make_object(ledger, object_type)
    write(ledger, object_type, "note", obj["ref"], "--append", "keep me")
    fake_editor(monkeypatch, result="")
    before = commit_count(ledger)

    result = invoke(ledger, object_type, "note", obj["ref"])

    assert isinstance(result.exception, UsageError)
    assert result.exception.exit_code == 2
    assert "--clear" in str(result.exception)
    assert commit_count(ledger) == before
    assert object_notes(ledger, object_type, obj) == "keep me"
    assert dirty(ledger) == []


@pytest.mark.parametrize("object_type", OBJECT_KINDS)
def test_cli_note_clear_sets_notes_to_null(
    cli_env: None, ledger_factory, object_type: str
) -> None:
    ledger = ledger_factory(
        [BASE], name=f"cli-note-clear-{object_type}", id_prefix="77"
    )
    obj = make_object(ledger, object_type)
    write(ledger, object_type, "note", obj["ref"], "--append", "remove me")

    cleared = json.loads(
        write(
            ledger, object_type, "note", obj["ref"], "--clear", "--json"
        ).output
    )

    assert cleared["notes"] is None
    assert object_notes(ledger, object_type, obj) is None
    assert latest_messages(ledger, 1) == [
        f"edit({object_type}): {obj['record']['kind']} {obj['record']['id']}"
    ]


@pytest.mark.parametrize("object_type", OBJECT_KINDS)
def test_cli_note_append_to_empty_and_non_empty_notes(
    cli_env: None, ledger_factory, object_type: str
) -> None:
    ledger = ledger_factory(
        [BASE], name=f"cli-note-append-{object_type}", id_prefix="77"
    )
    obj = make_object(ledger, object_type)

    first = json.loads(
        write(
            ledger, object_type, "note", obj["ref"], "--append", "first", "--json"
        ).output
    )
    assert first["notes"] == "first"

    second = json.loads(
        write(
            ledger,
            object_type,
            "note",
            obj["ref"],
            "second",
            "--append",
            "--json",
        ).output
    )
    assert second["notes"] == "first\nsecond"
    assert object_notes(ledger, object_type, obj) == "first\nsecond"


@pytest.mark.parametrize("object_type", OBJECT_KINDS)
def test_cli_note_bare_append_starts_from_an_empty_buffer(
    cli_env: None, ledger_factory, object_type: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = ledger_factory(
        [BASE], name=f"cli-note-bare-append-{object_type}", id_prefix="77"
    )
    obj = make_object(ledger, object_type)
    write(ledger, object_type, "note", obj["ref"], "--append", "keep")
    seen: list[str] = []
    fake_editor(monkeypatch, result="more", seen=seen)

    result = read(
        ledger, object_type, "note", obj["ref"], "--append", "--json"
    )

    assert seen == [""]
    assert json.loads(result.output)["notes"] == "keep\nmore"
    assert object_notes(ledger, object_type, obj) == "keep\nmore"


@pytest.mark.parametrize("object_type", OBJECT_KINDS)
def test_cli_note_bare_append_with_an_empty_editor_writes_nothing(
    cli_env: None, ledger_factory, object_type: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = ledger_factory(
        [BASE], name=f"cli-note-bare-append-empty-{object_type}", id_prefix="77"
    )
    obj = make_object(ledger, object_type)
    write(ledger, object_type, "note", obj["ref"], "--append", "keep")
    fake_editor(monkeypatch, result="")
    before = commit_count(ledger)

    result = read(ledger, object_type, "note", obj["ref"], "--append")

    assert result.output.strip() == "no changes"
    assert commit_count(ledger) == before
    assert object_notes(ledger, object_type, obj) == "keep"


@pytest.mark.parametrize("object_type", OBJECT_KINDS)
def test_cli_note_global_json_and_dry_run(
    cli_env: None, ledger_factory, object_type: str
) -> None:
    ledger = ledger_factory(
        [BASE], name=f"cli-note-json-dry-run-{object_type}", id_prefix="77"
    )
    obj = make_object(ledger, object_type)
    write(ledger, object_type, "note", obj["ref"], "--append", "keep")
    before = commit_count(ledger)

    dry = read(
        ledger,
        "--dry-run",
        object_type,
        "note",
        obj["ref"],
        "--append",
        "added",
        "--json",
    )
    plan = json.loads(dry.output)
    assert plan["dry_run"] is True
    assert any(
        "UPDATE" in statement and "`notes`" in statement
        for statement in plan["sql"].split(";")
    )
    assert plan["result"]["notes"] == "keep\nadded"

    assert commit_count(ledger) == before
    assert object_notes(ledger, object_type, obj) == "keep"


@pytest.mark.parametrize("object_type", OBJECT_KINDS)
def test_cli_note_global_json_flag_is_honoured(
    cli_env: None, ledger_factory, object_type: str
) -> None:
    ledger = ledger_factory(
        [BASE], name=f"cli-note-global-json-{object_type}", id_prefix="77"
    )
    obj = make_object(ledger, object_type)

    result = read(
        ledger, "--json", object_type, "note", obj["ref"], "--clear"
    )

    assert json.loads(result.output)["notes"] is None


@pytest.mark.parametrize("object_type", OBJECT_KINDS)
def test_cli_note_rejects_misused_flags(
    cli_env: None, ledger_factory, object_type: str
) -> None:
    ledger = ledger_factory(
        [BASE], name=f"cli-note-flags-{object_type}", id_prefix="77"
    )
    obj = make_object(ledger, object_type)
    before = commit_count(ledger)

    text_only = invoke(ledger, object_type, "note", obj["ref"], "orphan text")
    assert isinstance(text_only.exception, UsageError)
    assert "--append" in str(text_only.exception)

    clear_with_append = invoke(
        ledger, object_type, "note", obj["ref"], "--append", "--clear"
    )
    assert isinstance(clear_with_append.exception, UsageError)
    assert "--clear" in str(clear_with_append.exception)

    clear_with_text = invoke(
        ledger,
        object_type,
        "note",
        obj["ref"],
        "orphan text",
        "--append",
        "--clear",
    )
    assert isinstance(clear_with_text.exception, UsageError)

    empty_append = invoke(
        ledger, object_type, "note", obj["ref"], "--append", ""
    )
    assert isinstance(empty_append.exception, UsageError)
    assert "--clear" in str(empty_append.exception)

    assert commit_count(ledger) == before
    assert object_notes(ledger, object_type, obj) is None
    assert dirty(ledger) == []


# ---------------------------------------------------------------------------
# Reference resolution per kind (beyond the shared cases)
# ---------------------------------------------------------------------------


def test_box_note_resolves_name_alias_and_unique_id_prefix(
    cli_env: None, ledger_factory
) -> None:
    """A box is looked up by human name first, then alias, then id prefix."""
    ledger = ledger_factory([BASE], name="note-box-refs", id_prefix="77")
    box = json.loads(
        write(ledger, "box", "add", "rack", "R1", "--json").output
    )
    write(ledger, "box", "note", "R1", "--append", "by name")
    write(ledger, "alias", "add", "box", "R1", "R-one")
    write(ledger, "box", "note", "R-one", "--append", "by alias")
    write(ledger, "box", "note", box["id"][:8], "--append", "by prefix")

    assert ledger.driver.query(
        "SELECT `notes` FROM `box` WHERE `id` = %s", (box["id"],)
    ) == [{"notes": "by name\nby alias\nby prefix"}]


def test_entity_note_resolves_an_alias_and_a_unique_id_prefix(
    cli_env: None, ledger_factory
) -> None:
    ledger = ledger_factory([BASE], name="note-entity-refs", id_prefix="77")
    entity = json.loads(
        write(
            ledger,
            "entity",
            "add",
            "item",
            "--name",
            "alpha",
            "--id",
            ENTITY_IDS[0],
            "--json",
        ).output
    )
    write(ledger, "alias", "add", "entity", entity["id"], "E1")
    write(ledger, "entity", "note", "E1", "--append", "by alias")
    write(ledger, "entity", "note", "aaaaaaaa", "--append", "by prefix")

    assert ledger.driver.query(
        "SELECT `notes` FROM `entity` WHERE `id` = %s", (ENTITY_IDS[0],)
    ) == [{"notes": "by alias\nby prefix"}]
