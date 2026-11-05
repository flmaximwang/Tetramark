"""Terminal-free tests for the real add-form input source.

The form's state machine is covered elsewhere; these tests drive
:class:`~tetramark.form.tui.PromptToolkitSource` through a prompt_toolkit
``PipeInput`` and a ``DummyOutput``, exactly like a keyboard would.  No TTY, no
Dolt, no terminal setup -- so they belong to the ``fast`` lane.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.output.plain_text import PlainTextOutput

from tetramark.form import editor
from tetramark.form.source import (
    REVIEW_KEYS,
    STEP_KEYS,
    Choice,
    Prompt,
    Reply,
    ReviewRow,
)
from tetramark.form.spec import Step, build_plan
from tetramark.form.state import FormState, LongValue
from tetramark.form.tui import PromptToolkitSource, _message
from tetramark.schema.loader import load_schema
from tetramark.schema.model import (
    EffectiveSchema,
    Geometry,
    GeometryType,
    Kind,
    LedgerConfig,
    ObjectType,
)

pytestmark = pytest.mark.fast

#: The repository's own base preset (the box geometries a new ledger starts
#: with), loaded with the pure loader exactly as the CLI does.
BASE_SCHEMA = load_schema(
    LedgerConfig(
        name="form",
        presets=[str(Path(__file__).resolve().parents[1] / "presets" / "base.yaml")],
    ),
    base_dir=Path.cwd(),
)


class _KindOnlyResolver:
    """The kind step reads no ledger data; any resolver use is a test bug."""

    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"the kind step must not touch the resolver ({name})")


#: One inline schema (no files) covering the fact rows the base preset does not:
#: a custom slot template with ``slot_capacity: 2``, a large named list with a
#: markup-like label, and both bounded and unlimited slotless kinds.
INLINE_SCHEMA = EffectiveSchema(
    kinds={
        "grid2": Kind(
            object=ObjectType.box,
            description="A 96-well box with a two-place slot capacity.",
            slot_capacity=2,
            geometry=Geometry(
                type=GeometryType.grid,
                rows="A-H",
                cols="1-12",
                label="R{row_index}C{col_index}",
            ),
        ),
        "named14": Kind(
            object=ObjectType.box,
            description="A named box with fourteen slots.",
            geometry=Geometry(
                type=GeometryType.named,
                slots=[
                    "s1",
                    "<b>&{row}",
                    "s3",
                    "s4",
                    "s5",
                    "s6",
                    "s7",
                    "s8",
                    "s9",
                    "s10",
                    "s11",
                    "s12",
                    "s13",
                    "s14",
                ],
            ),
        ),
        "shelf50": Kind(
            object=ObjectType.box,
            description="A shelf with a capacity.",
            geometry=Geometry(type=GeometryType.slotless, capacity=50),
        ),
        "shelfopen": Kind(
            object=ObjectType.box,
            description="A shelf without a capacity.",
            geometry=Geometry(type=GeometryType.slotless),
        ),
        "item": Kind(object=ObjectType.entity, description="An item."),
        "sample": Kind(object=ObjectType.entity, description="A sample."),
    }
)


def _step_prompt(
    *,
    key: str = "name",
    label: str = "name",
    type: str = "string",
    long: bool = False,
    values: tuple[str, ...] = (),
    required: bool = False,
    **prompt_kwargs: object,
) -> Prompt:
    step = Step(
        key=key,
        label=label,
        type=type,
        long=long,
        values=values,
        required=required,
    )
    return Prompt(
        mode="step",
        step=step,
        index=0,
        total=3,
        footer=STEP_KEYS,
        long=long,
        **prompt_kwargs,  # type: ignore[arg-type]
    )


def _review_prompt() -> Prompt:
    return Prompt(
        mode="review",
        total=2,
        rows=(
            ReviewRow(number=1, label="kind", value="plasmid"),
            ReviewRow(number=2, label="name", value="pUC19"),
        ),
        footer=REVIEW_KEYS,
    )


def _ask(prompt: Prompt, keys: str, *, output: object | None = None) -> Reply:
    output = DummyOutput() if output is None else output
    with create_pipe_input() as pipe:
        pipe.send_text(keys)
        source = PromptToolkitSource(input=pipe, output=output)
        return source.ask(prompt)


def _message_text(prompt: Prompt) -> str:
    return "".join(text for _style, text in _message(prompt))


def _kind_message_text(schema: EffectiveSchema, object_type: str = "box") -> str:
    state = FormState(build_plan(schema, object_type), _KindOnlyResolver())
    return _message_text(state.prompt())


def _choice_lines(text: str, number: int) -> list[str]:
    """One numbered choice line plus the indented fact lines under it."""
    lines = text.splitlines()
    start = next(
        index for index, line in enumerate(lines) if line.startswith(f"  {number}. ")
    )
    block = [lines[start]]
    for line in lines[start + 1 :]:
        if not line.startswith("       "):
            break
        block.append(line)
    return block


# -- normal answers ---------------------------------------------------------
def test_enter_submits_the_buffer() -> None:
    assert _ask(_step_prompt(), "plasmid\r") == Reply(text="plasmid")


def test_prefilled_value_is_kept_and_edited_at_the_end() -> None:
    assert _ask(_step_prompt(value="pUC19"), "X\r") == Reply(text="pUC19X")


def test_shift_tab_goes_back() -> None:
    assert _ask(_step_prompt(), "\x1b[Z") == Reply(action="back")


def test_ctrl_c_aborts() -> None:
    assert _ask(_step_prompt(), "\x03") == Reply(action="abort")


@pytest.mark.parametrize(
    "keys",
    ["first\x1b\rsecond\r", "first\nsecond\r"],
    ids=["alt-enter", "ctrl-j"],
)
def test_newline_keys_insert_a_newline(keys: str) -> None:
    assert _ask(_step_prompt(), keys) == Reply(text="first\nsecond")


# -- completion -------------------------------------------------------------
def test_tab_completes_the_only_candidate() -> None:
    prompt = _step_prompt(completions=("aliquot",))
    assert _ask(prompt, "\t\r") == Reply(text="aliquot")


def test_tab_completes_a_common_prefix() -> None:
    prompt = _step_prompt(completions=("plasmid", "plasmids"))
    assert _ask(prompt, "plasm\t\r") == Reply(text="plasmid")


# -- long fields ------------------------------------------------------------
def _long_prompt(raw: str | None = None, **prompt_kwargs: object) -> Prompt:
    value = LongValue(raw).display() if raw is not None else ""
    return _step_prompt(
        key="sequence",
        label="sequence",
        type="sequence",
        long=True,
        value=value,
        long_raw=raw,
        **prompt_kwargs,
    )


def test_bracketed_paste_submits_the_raw_text_and_renders_the_short_form() -> None:
    raw = ("ATGCGTACGT" * 10 + "\n") * 5
    display = LongValue(raw, pasted=True).display()
    screen = io.StringIO()
    reply = _ask(
        _long_prompt(),
        f"\x1b[200~{raw}\x1b[201~\r",
        output=PlainTextOutput(screen),
    )
    assert reply == Reply(text=raw, pasted=True)
    rendered = screen.getvalue()
    assert display in rendered
    assert raw not in rendered


def test_pasting_again_replaces_the_long_value() -> None:
    first = "A" * 200
    second = "C" * 300
    reply = _ask(
        _long_prompt(),
        f"\x1b[200~{first}\x1b[201~"
        f"\x1b[200~{second}\x1b[201~\r",
    )
    assert reply == Reply(text=second, pasted=True)


def test_accepting_the_placeholder_submits_the_stored_long_value() -> None:
    raw = "ACGT" * 100
    assert _ask(_long_prompt(raw), "\r") == Reply(text=raw, pasted=True)


def test_typing_after_a_long_placeholder_replaces_it() -> None:
    raw = "ACGT" * 100
    reply = _ask(_long_prompt(raw), "ATGC\r")
    assert reply == Reply(text="ATGC")
    assert reply.pasted is False


def test_backspace_on_the_placeholder_clears_the_long_value() -> None:
    raw = "ACGT" * 100
    prompt = _long_prompt(raw)
    assert _ask(prompt, "\x7f\r") == Reply(text="")
    assert _ask(prompt, "\x7fshort\r") == Reply(text="short")


def test_huge_long_value_is_never_shown_in_full() -> None:
    raw = "ACGT" * 5_000
    prompt = _long_prompt(raw)
    assert raw not in _message_text(prompt)


def test_bracketed_paste_into_a_short_field_is_plain_text() -> None:
    reply = _ask(_step_prompt(value="pre"), "\x1b[200~fix\x1b[201~\r")
    assert reply == Reply(text="prefix")


# -- Ctrl+E external editor -------------------------------------------------
def test_ctrl_e_edits_the_buffer_and_reprompts(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []

    def fake_edit(value: str, step: Step, **_kwargs: object) -> str:
        calls.append((value, step.key))
        return "edited in $EDITOR"

    monkeypatch.setattr(editor, "edit_value", fake_edit)
    reply = _ask(_step_prompt(), "draft\x05\r")
    assert calls == [("draft", "name")]
    assert reply == Reply(text="edited in $EDITOR")


def test_ctrl_e_on_a_placeholder_edits_the_full_long_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = "ACGT" * 200
    seen: list[str] = []

    def fake_edit(value: str, step: Step, **_kwargs: object) -> str:
        seen.append(value)
        return "short replacement"

    monkeypatch.setattr(editor, "edit_value", fake_edit)
    reply = _ask(_long_prompt(raw), "\x05\r")
    assert seen == [raw]
    assert reply == Reply(text="short replacement")


def test_ctrl_e_that_returns_a_long_value_keeps_it_short_in_the_buffer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = "A" * 300
    edited = "G" * 500
    monkeypatch.setattr(editor, "edit_value", lambda value, step: edited)
    screen = io.StringIO()
    reply = _ask(_long_prompt(raw), "\x05\r", output=PlainTextOutput(screen))
    assert reply == Reply(text=edited)
    assert LongValue(edited).display() in screen.getvalue()
    assert edited not in screen.getvalue()


# -- review screen ----------------------------------------------------------
@pytest.mark.parametrize("typed", ["y", "q", "2"])
def test_review_prompt_returns_the_typed_command(typed: str) -> None:
    assert _ask(_review_prompt(), f"{typed}\r") == Reply(text=typed)


def test_review_ctrl_c_aborts() -> None:
    assert _ask(_review_prompt(), "\x03") == Reply(action="abort")


# -- rendering --------------------------------------------------------------
def test_step_header_shows_index_description_choices_notice_and_error() -> None:
    step = Step(
        key="kind",
        label="kind",
        type="kind",
        description="The record kind.",
        required=True,
    )
    prompt = Prompt(
        mode="step",
        step=step,
        index=1,
        total=4,
        choices=(
            Choice(value="plasmid", label="plasmid", detail="A plasmid."),
            Choice(value="strain", label="strain", detail="A strain."),
        ),
        choice_title="Choose a kind",
        notice="free slots: A1, A2",
        error="name is required",
        footer=STEP_KEYS,
    )
    text = _message_text(prompt)
    assert "2/4  kind *" in text
    assert "The record kind." in text
    assert "Choose a kind" in text
    assert "1. plasmid  A plasmid." in text
    assert "2. strain  A strain." in text
    assert "free slots: A1, A2" in text
    assert "name is required" in text
    assert text.endswith("kind> ")


def test_enum_values_are_numbered_because_they_can_be_picked_by_number() -> None:
    prompt = _step_prompt(
        key="status",
        type="enum",
        values=("stock", "aliquot"),
        completions=("stock", "aliquot"),
    )
    text = _message_text(prompt)
    assert "1. stock" in text
    assert "2. aliquot" in text


def test_review_header_shows_numbered_rows_and_the_review_prompt() -> None:
    text = _message_text(_review_prompt())
    assert " 1. kind: plasmid" in text
    assert " 2. name: pUC19" in text
    assert text.endswith("review> ")


# -- box kind facts ---------------------------------------------------------
def test_base_preset_box_kinds_render_their_structured_facts() -> None:
    """The base box kinds keep their description and gain the settled rows."""
    text = _kind_message_text(BASE_SCHEMA)
    plan = build_plan(BASE_SCHEMA, "box")
    assert [choice.name for choice in plan.kinds] == [
        "drawer",
        "freezer",
        "grid8x12",
        "grid9x9",
        "incubator",
        "rack",
        "shelf",
    ]
    positions = []
    for number, choice in enumerate(plan.kinds, start=1):
        description = BASE_SCHEMA.kind(choice.name).description
        line = f"  {number}. {choice.name}  {description}"
        assert line in text
        positions.append(text.index(line))
    assert positions == sorted(positions)  # numbered 1-7 in sorted order

    assert _choice_lines(text, 4) == [
        "  4. grid9x9  Nine-by-nine tube box (rows A-J without I, columns 1-9).",
        "       geometry: grid, 9 rows × 9 columns",
        "       rows: A B C D E F G H J (9)",
        "       columns: 1 2 3 4 5 6 7 8 9 (9)",
        "       slots: A1 … J9 (81)",
        "       capacity: 81 aliquots (81 slots × 1 per slot)",
        "       holds boxes: no",
    ]
    assert _choice_lines(text, 6) == [
        "  6. rack  Tube rack with ten numbered positions.",
        "       geometry: named, 10 slots",
        "       slots: 1 2 3 4 5 6 7 8 9 10 (10)",
        "       capacity: 10 aliquots (10 slots × 1 per slot)",
        "       holds boxes: yes, one per free slot",
    ]
    assert _choice_lines(text, 7) == [
        "  7. shelf  A freezer shelf; racks or boxes sit on it directly and it has no slots.",
        "       geometry: slotless",
        "       capacity: unlimited",
        "       holds boxes: yes",
    ]


def test_inline_schema_facts_show_templates_capacity_and_bounded_labels() -> None:
    """A custom template, slot_capacity 2 and a >12 label list render exactly."""
    text = _kind_message_text(INLINE_SCHEMA)
    assert _choice_lines(text, 1) == [
        "  1. grid2  A 96-well box with a two-place slot capacity.",
        "       geometry: grid, 8 rows × 12 columns",
        "       rows: A B C D E F G H (8)",
        "       columns: 1 2 3 4 5 6 7 8 9 10 11 12 (12)",
        "       slots: R1C1 … R8C12 (96)",
        "       capacity: 192 aliquots (96 slots × 2 per slot)",
        "       holds boxes: no",
    ]
    assert _choice_lines(text, 2) == [
        "  2. named14  A named box with fourteen slots.",
        "       geometry: named, 14 slots",
        "       slots: s1 <b>&{row} s3 s4 s5 s6 s7 s8 … s13 s14 (14)",
        "       capacity: 14 aliquots (14 slots × 1 per slot)",
        "       holds boxes: yes, one per free slot",
    ]
    assert _choice_lines(text, 3) == [
        "  3. shelf50  A shelf with a capacity.",
        "       geometry: slotless",
        "       capacity: 50 (aliquots + child boxes)",
        "       holds boxes: yes",
    ]
    assert _choice_lines(text, 4) == [
        "  4. shelfopen  A shelf without a capacity.",
        "       geometry: slotless",
        "       capacity: unlimited",
        "       holds boxes: yes",
    ]
    # Markup-like labels are literal text: never escaped, never interpreted.
    assert "<b>&{row}" in text
    assert "&lt;b&gt;" not in text


def test_entity_kind_list_renders_without_facts() -> None:
    """A non-box kind list keeps exactly the numbered description lines."""
    plan = build_plan(INLINE_SCHEMA, "entity")
    prompt = FormState(plan, _KindOnlyResolver()).prompt()
    assert all(choice.facts == () for choice in prompt.choices)
    text = _message_text(prompt)
    assert _choice_lines(text, 1) == ["  1. item  An item."]
    assert _choice_lines(text, 2) == ["  2. sample  A sample."]
    assert "geometry:" not in text
    assert not any(line.startswith("       ") for line in text.splitlines())
