"""The ``add-form`` state machine, Dolt-free (Tetramark-se9).

Everything here runs in the fast lane: the schema is loaded from YAML, the
ledger is a fake :class:`Resolver`, the terminal is a scripted source.
Covered: the plan/kind step, schema-order fields (computed skipped, sequences
asked), navigation and back pre-fill, per-type checks, refs/search/boxes/slots,
multi-value inputs, long-value paste/display, the review screen and jumps, the
core-rejection focus, and the payload each object's ``add`` receives.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from tetramark.core.objects import _parse_quantity_unit
from tetramark.errors import UsageError
from tetramark.form.editor import (
    SCISSORS,
    edit_value,
    editor_command,
    editor_text,
    strip_editor_text,
)
from tetramark.form.source import Choice, Reply, ScriptedSource
from tetramark.form.spec import Step, box_kind_facts, build_plan
from tetramark.form.state import (
    FormError,
    FormState,
    LongValue,
    parse_multi,
    short_form,
)
from tetramark.schema.loader import load_schema
from tetramark.schema.model import LedgerConfig

pytestmark = pytest.mark.fast

PRESETS = Path(__file__).parent / "data" / "presets"

FULL = load_schema(
    LedgerConfig(name="form", presets=[str(PRESETS / "full")]), base_dir=Path.cwd()
)
SINGLE = load_schema(
    LedgerConfig(name="form", presets=[str(PRESETS / "base")]), base_dir=Path.cwd()
)
#: The repository's own base preset: the box geometries a new ledger starts with.
REPO_BASE = load_schema(
    LedgerConfig(
        name="form",
        presets=[str(Path(__file__).resolve().parents[1] / "presets" / "base.yaml")],
    ),
    base_dir=Path.cwd(),
)

TODAY = dt.date(2026, 11, 3)
NOW = dt.datetime(2026, 11, 3, 12, 30, 5)


# ---------------------------------------------------------------------------
# A Dolt-free Resolver
# ---------------------------------------------------------------------------


class FakeResolver:
    """The ledger-facing half of the form, faked for the fast lane."""

    def __init__(self) -> None:
        self.refs: dict[str, dict[str, tuple[str, str]]] = {
            "entity": {
                "e1": ("e1", "Everything One"),
                "i1": ("i1", "Item One"),
                "p1": ("p1", "Plasmid One"),
            },
            "stock": {"s1": ("s1", "1226FP01")},
            "everything": {"e1": ("e1", "Everything One")},
        }
        self.kinds: dict[str, str] = {
            "e1": "everything",
            "i1": "item",
            "p1": "plasmid",
            "s1": "bacterial",
        }
        self.boxes: dict[str, dict[str, object]] = {
            "GRID1": {"kind": "grid96", "slotted": True, "free": ["A1", "A2", "A3"]},
            "RACK1": {"kind": "rack", "slotted": True, "free": ["top", "middle"]},
            "SHELF": {"kind": "shelf", "slotted": False, "free": []},
        }
        self.search_hits: dict[str, list[tuple[str, str]]] = {
            "bb": [("p1", "Plasmid One")],
            "one": [("e1", "Everything One"), ("i1", "Item One")],
        }

    def resolve_ref(
        self, target: str, text: str, *, allowed_kinds: tuple[str, ...] = ()
    ) -> tuple[str, str]:
        hit = self.refs.get(target, {}).get(text)
        if hit is None:
            raise FormError(
                f"cannot resolve {text!r} for reference to {target!r} "
                "(no such id, and no alias for it)"
            )
        kind = self.kinds.get(hit[0])
        if allowed_kinds and kind not in allowed_kinds:
            raise FormError(
                f"{hit[1]!r} is a {kind!r} entity; this step accepts: "
                + ", ".join(allowed_kinds)
            )
        return hit

    def search(
        self, target: str, term: str, *, allowed_kinds: tuple[str, ...] = ()
    ) -> list[Choice]:
        return [
            Choice(value=object_id, label=label, detail="Freezer/A1")
            for object_id, label in self.search_hits.get(term, [])
            if not allowed_kinds or self.kinds.get(object_id) in allowed_kinds
        ]

    def list_refs(
        self, target: str, *, allowed_kinds: tuple[str, ...] = ()
    ) -> list[Choice]:
        return [
            Choice(value=object_id, label=label)
            for object_id, label in self.refs.get(target, {}).values()
            if not allowed_kinds or self.kinds.get(object_id) in allowed_kinds
        ]

    def ref_tokens(
        self, target: str, *, allowed_kinds: tuple[str, ...] = ()
    ) -> list[str]:
        tokens = [choice.value for choice in self.list_refs(
            target, allowed_kinds=allowed_kinds
        )]
        tokens.extend(["legacy-alias"])
        return tokens

    def resolve_box(self, text: str) -> tuple[str, str]:
        if text not in self.boxes:
            raise FormError(f"cannot find box {text!r}")
        return text, f"{text} ({self.boxes[text]['kind']})"

    def list_boxes(self, *, parents_only: bool = False) -> list[Choice]:
        choices = []
        for name in sorted(self.boxes):
            info = self.boxes[name]
            if parents_only and info["kind"] == "grid96":
                continue
            detail = "slotted" if info["slotted"] else "slotless"
            choices.append(
                Choice(value=name, label=f"{name} ({info['kind']})", detail=detail)
            )
        return choices

    def is_slotted(self, box: str) -> bool:
        return bool(self.boxes.get(box, {}).get("slotted", False))

    def free_slots(self, box: str) -> list[str]:
        return list(self.boxes.get(box, {}).get("free", []))


def state_for(object_type: str = "entity", resolver: FakeResolver | None = None) -> FormState:
    return FormState(
        build_plan(FULL, object_type),
        resolver or FakeResolver(),
        today=lambda: TODAY,
        now=lambda: NOW,
    )


def index_of(state: FormState, key: str) -> int:
    return next(
        index
        for index, step in enumerate(state.visible_steps())
        if step.key == key
    )


def at(state: FormState, key: str) -> FormState:
    """Focus one step (as if the user walked there); clear the last error."""
    state.index = index_of(state, key)
    state.error = None
    return state


# ---------------------------------------------------------------------------
# Plans: kinds and fields
# ---------------------------------------------------------------------------


def test_kind_step_lists_the_schema_kinds() -> None:
    entity = build_plan(FULL, "entity").kind_step()
    assert [choice.name for choice in entity.choices] == [
        "everything",
        "item",
        "plasmid",
    ]
    aliquot = build_plan(FULL, "aliquot").kind_step()
    assert [choice.name for choice in aliquot.choices] == [
        "bacterial",
        "lot",
        "prep",
    ]


def test_box_kind_facts_capacity_matches_the_box_list_formula() -> None:
    """The shown capacity is the one ``tm box list`` computes from the schema."""
    for kind in REPO_BASE.kinds_for("box").values():
        geometry = kind.geometry
        assert geometry is not None
        facts = dict(box_kind_facts(kind))
        capacity = (
            len(geometry.labels) * kind.slot_capacity
            if geometry.is_slotted
            else geometry.capacity
        )
        if capacity is None:
            assert facts["capacity"] == "unlimited"
        elif geometry.is_slotted:
            assert facts["capacity"] == (
                f"{capacity} aliquots "
                f"({len(geometry.labels)} slots × {kind.slot_capacity} per slot)"
            )
        else:
            assert facts["capacity"] == f"{capacity} (aliquots + child boxes)"


def test_entity_steps_follow_schema_order_and_skip_computed() -> None:
    steps = build_plan(FULL, "entity").steps("everything")
    keys = [step.key for step in steps]
    assert keys == [
        "name",
        "description",
        "code",
        "blurb",
        "count",
        "ratio",
        "active",
        "born",
        "seen_at",
        "color",
        "seq",
        "parent",
        "legacy",
        "aliases",
        "notes",
    ]
    assert "display" not in keys  # computed
    by_key = {step.key: step for step in steps}
    assert by_key["name"].required is True
    assert by_key["code"].pattern == "^[A-Z]{3}$"
    assert by_key["color"].values == ("red", "green", "blue")
    assert by_key["color"].default == "red"
    assert by_key["born"].default == "2024-01-31"
    assert by_key["blurb"].long is True
    assert by_key["seq"].long is True
    assert by_key["seq"].type == "sequence"


def test_stock_and_box_and_aliquot_steps() -> None:
    stock = {step.key: step for step in build_plan(FULL, "stock").steps("bacterial")}
    assert list(stock) == [
        "entity",
        "measured_at",
        "amount",
        "strain",
        "medium",
    ]
    assert stock["entity"].required is True
    assert stock["entity"].allowed_kinds == ("item", "plasmid")
    # The stock form creates the stock only; aliquots have their own form.
    assert "aliquots" not in stock and "box" not in stock and "slots" not in stock

    box = [step.key for step in build_plan(FULL, "box").steps("grid96")]
    assert box == ["name", "parent", "slot", "notes"]

    aliquot = [step.key for step in build_plan(FULL, "aliquot").steps("prep")]
    assert aliquot == ["stock", "count", "quantity", "unit", "box", "slots"]


# ---------------------------------------------------------------------------
# Aliquot quantity and unit (Tetramark-b3p)
# ---------------------------------------------------------------------------


def aliquot_state() -> FormState:
    """An aliquot form that has chosen its kind and picked the stock."""
    state = state_for("aliquot")
    state.submit("1")  # kind: bacterial
    at(state, "stock").submit("s1")
    return state


def test_aliquot_asks_the_quantity_and_the_unit_after_the_count() -> None:
    steps = build_plan(FULL, "aliquot").steps("prep")
    assert [step.key for step in steps] == [
        "stock",
        "count",
        "quantity",
        "unit",
        "box",
        "slots",
    ]
    by_key = {step.key: step for step in steps}
    assert by_key["quantity"].type == "quantity"
    assert by_key["quantity"].required is False  # optional
    assert by_key["quantity"].default is None  # nothing is pre-filled
    assert by_key["unit"].type == "unit"
    assert by_key["unit"].required is True  # required once it is asked
    assert by_key["unit"].default is None


def test_the_unit_is_asked_only_after_a_quantity() -> None:
    state = aliquot_state()
    assert "unit" not in [step.key for step in state.visible_steps()]
    at(state, "quantity").submit("2.5")
    assert [step.key for step in state.visible_steps()] == [
        "kind",
        "stock",
        "count",
        "quantity",
        "unit",
        "box",
    ]


def test_an_empty_quantity_skips_the_unit_and_stores_nothing() -> None:
    state = aliquot_state()
    at(state, "quantity").submit("")
    assert "quantity" not in state.answers
    assert "unit" not in state.answers
    assert "unit" not in [step.key for step in state.visible_steps()]
    payload = state.payload()
    assert payload["quantity"] is None
    assert payload["unit"] is None


def test_the_quantity_is_checked_as_typed() -> None:
    for raw, message in (
        ("abc", "quantity must be a decimal number (got 'abc')"),
        ("-1", "quantity must be a finite, non-negative number (got '-1')"),
        ("NaN", "quantity must be a finite, non-negative number (got 'NaN')"),
        (
            "Infinity",
            "quantity must be a finite, non-negative number (got 'Infinity')",
        ),
    ):
        state = aliquot_state()
        at(state, "quantity").submit(raw)
        assert state.error == message
        assert "quantity" not in state.answers

    # Zero is a quantity like any other, and the answer is kept as typed.
    state = aliquot_state()
    at(state, "quantity").submit("0")
    assert state.error is None
    assert state.answers["quantity"] == "0"

    state = aliquot_state()
    at(state, "quantity").submit(" 2.5 ")
    assert state.error is None
    assert state.answers["quantity"] == "2.5"


@pytest.mark.parametrize("raw", ["0", "2.5", "1e3", "1_000", ".5", "007", "3.000", "-0"])
def test_the_quantity_accepts_exactly_what_the_core_accepts(raw: str) -> None:
    """S3: same inputs as core ``_parse_quantity_unit`` -- nothing stricter."""
    assert _parse_quantity_unit(raw, "ml")[0] is not None
    state = aliquot_state()
    at(state, "quantity").submit(raw)
    assert state.error is None
    assert state.answers["quantity"] == raw


@pytest.mark.parametrize("raw", ["abc", "-1", "NaN", "Infinity", "0.5ml"])
def test_the_quantity_refuses_with_the_core_word_for_word(raw: str) -> None:
    """S3: the form's refusal is the core's message, word for word."""
    with pytest.raises(UsageError) as core_error:
        _parse_quantity_unit(raw, "ml")
    state = aliquot_state()
    at(state, "quantity").submit(raw)
    assert state.error == str(core_error.value)


def test_the_long_unit_message_matches_the_core() -> None:
    with pytest.raises(UsageError) as core_error:
        _parse_quantity_unit("1", "m" * 33)
    state = aliquot_state()
    at(state, "quantity").submit("1")
    at(state, "unit").submit("m" * 33)
    assert state.error == str(core_error.value) == "unit must be at most 32 characters"


def test_the_unit_is_required_and_at_most_32_characters() -> None:
    state = aliquot_state()
    at(state, "quantity").submit("2.5")
    at(state, "unit").submit("")
    assert state.error == "unit is required"
    assert "unit" not in state.answers

    at(state, "unit").submit("   ")
    assert state.error == "unit is required"

    at(state, "unit").submit("m" * 33)
    assert state.error == "unit must be at most 32 characters"

    at(state, "unit").submit("  ml  ")
    assert state.error is None
    assert state.answers["unit"] == "ml"

    state = aliquot_state()
    at(state, "quantity").submit("2.5")
    at(state, "unit").submit("m" * 32)
    assert state.error is None
    assert state.answers["unit"] == "m" * 32


def test_clearing_the_quantity_drops_the_unit() -> None:
    state = aliquot_state()
    at(state, "quantity").submit("2.5")
    at(state, "unit").submit("ml")
    assert state.answers["unit"] == "ml"

    state.back()  # Shift+Tab: back to the unit
    assert state.current().key == "unit"
    state.back()  # and back to the quantity
    assert state.current().key == "quantity"
    state.submit("")  # cleared: the unit goes with it
    assert "quantity" not in state.answers
    assert "unit" not in state.answers
    assert "unit" not in [step.key for step in state.visible_steps()]


def test_a_review_jump_that_clears_the_quantity_drops_the_unit() -> None:
    state = aliquot_state()
    at(state, "quantity").submit("2.5")
    at(state, "unit").submit("ml")
    at(state, "box").submit("")
    assert state.mode == "review"
    rows = {row.label: row.value for row in state.review_rows()}
    assert rows["quantity"] == "2.5"
    assert rows["unit"] == "ml"

    state.review_input("4")  # jump back to the quantity step
    assert state.current().key == "quantity"
    state.submit("")
    assert "quantity" not in state.answers
    assert "unit" not in state.answers


def test_the_review_shows_the_quantity_and_the_unit() -> None:
    state = aliquot_state()
    at(state, "quantity").submit("2.5")
    at(state, "unit").submit("ml")
    at(state, "box").submit("")
    assert state.mode == "review"
    rows = {row.label: row.value for row in state.review_rows()}
    assert rows["quantity"] == "2.5"
    assert rows["unit"] == "ml"

    # A skipped quantity shows empty and the unit row is not there at all.
    state = aliquot_state()
    at(state, "quantity").submit("")
    at(state, "box").submit("")
    assert state.mode == "review"
    rows = {row.label: row.value for row in state.review_rows()}
    assert rows["quantity"] == ""
    assert "unit" not in rows


def test_a_core_rejection_about_the_quantity_or_the_unit_focuses_that_step() -> None:
    state = aliquot_state()
    at(state, "quantity").submit("2.5")
    at(state, "unit").submit("ml")

    state.reject("quantity must be a finite, non-negative number (got '-1')")
    assert state.mode == "input"
    assert state.current().key == "quantity"

    state.reject("unit must be at most 32 characters")
    assert state.mode == "input"
    assert state.current().key == "unit"


def test_stock_form_walks_kind_entity_fields_and_reviews_without_aliquots() -> None:
    state = state_for("stock")
    state.submit("1")  # bacterial
    steps = [step.key for step in state.visible_steps()]
    assert steps == ["kind", "entity", "measured_at", "amount", "strain", "medium"]
    assert "aliquots" not in steps and "box" not in steps and "slots" not in steps

    at(state, "entity").submit("p1")
    at(state, "measured_at").submit("")
    at(state, "amount").submit("")
    at(state, "strain").submit("BL21(DE3)")
    at(state, "medium").submit("lb")
    assert state.mode == "review"
    labels = [row.label for row in state.review_rows()]
    assert labels == ["kind", "entity", "measured_at", "amount", "strain", "medium"]
    assert not {"aliquots", "box", "slots"} & set(labels)
    assert [row.number for row in state.review_rows()] == [1, 2, 3, 4, 5, 6]


# ---------------------------------------------------------------------------
# Kind selection and navigation
# ---------------------------------------------------------------------------


def test_enter_accepts_a_single_kind() -> None:
    state = FormState(
        build_plan(SINGLE, "entity"), FakeResolver(), today=lambda: TODAY
    )
    assert [choice.value for choice in state.prompt().choices] == ["item"]
    state.submit("")
    assert state.kind == "item"
    assert state.current() is not None and state.current().key == "name"


def test_kind_by_number_or_name_and_unknown_kind() -> None:
    state = state_for("entity")
    state.submit("2")
    assert state.kind == "item"

    state = state_for("entity")
    state.submit("plasmid")
    assert state.kind == "plasmid"

    state = state_for("entity")
    state.submit("nope")
    assert "unknown kind" in state.error
    assert state.kind is None


def test_box_kind_step_selects_by_number_and_carries_facts() -> None:
    state = state_for("box")
    prompt = state.prompt()
    assert [choice.value for choice in prompt.choices] == [
        "grid96",
        "grid9x9",
        "namedbox",
        "rack",
        "shelf",
        "slotlessbox",
    ]
    assert all(choice.facts for choice in prompt.choices)
    assert dict(prompt.choices[0].facts)["geometry"] == "grid, 8 rows × 12 columns"

    state.submit("1")
    assert state.kind == "grid96"


def test_shift_tab_back_to_the_kind_step_keeps_the_same_facts() -> None:
    state = state_for("box")
    before = state.prompt().choices
    state.submit("1")  # grid96
    assert state.current().key == "name"

    state.back()  # Shift+Tab back to the kind step
    assert state.current().key == "kind"
    after = state.prompt().choices
    assert after == before
    assert all(choice.facts for choice in after)


def test_required_empty_optional_skip_and_back_prefill() -> None:
    state = state_for("entity")
    state.submit("1")  # everything
    assert state.current().key == "name"
    state.submit("")
    assert state.error == "name is required"
    assert "name" not in state.answers

    state.submit("Thing")
    assert state.answers["name"] == "Thing"
    assert state.current().key == "description"

    state.submit("")
    assert "description" not in state.answers
    assert state.current().key == "code"

    state.back()
    assert state.current().key == "description"
    assert state.prompt().value == ""

    state.back()
    assert state.current().key == "name"
    assert state.prompt().value == "Thing"

    state.back()  # the kind step
    assert state.current().key == "kind"
    assert state.prompt().value == "everything"
    state.back()
    assert state.notice == "This is the first step."


def test_defaults_are_accepted_with_enter() -> None:
    state = state_for("entity")
    state.submit("1")
    at(state, "count").submit("")
    assert state.answers["count"] == "0"
    at(state, "active").submit("")
    assert state.answers["active"] == "true"
    at(state, "born").submit("")
    assert state.answers["born"] == "2024-01-31"
    at(state, "color").submit("")
    assert state.answers["color"] == "red"


# ---------------------------------------------------------------------------
# Per-type checks
# ---------------------------------------------------------------------------


def test_enum_by_number_value_and_unknown() -> None:
    state = state_for("entity")
    state.submit("1")
    at(state, "color").submit("2")
    assert state.answers["color"] == "green"
    at(state, "color").submit("blue")
    assert state.answers["color"] == "blue"
    at(state, "color").submit("purple")
    assert "must be one of" in state.error
    assert state.answers["color"] == "blue"


def test_bool_int_decimal_and_pattern() -> None:
    state = state_for("entity")
    state.submit("1")
    at(state, "active").submit("y")
    assert state.answers["active"] == "true"
    at(state, "active").submit("n")
    assert state.answers["active"] == "false"
    at(state, "active").submit("maybe")
    assert "must be y or n" in state.error

    at(state, "count").submit("x")
    assert "must be an integer" in state.error
    at(state, "count").submit(" 12 ")
    assert state.answers["count"] == "12"

    at(state, "ratio").submit("1.250")
    assert state.answers["ratio"] == "1.250"
    at(state, "ratio").submit("NaN")
    assert "finite" in state.error

    at(state, "code").submit("ABC")
    assert state.answers["code"] == "ABC"
    at(state, "code").submit("abcd")
    assert "does not match the pattern" in state.error


def test_date_and_datetime_today_now() -> None:
    state = state_for("entity")
    state.submit("1")
    at(state, "born").submit("today")
    assert state.answers["born"] == "2026-11-03"
    at(state, "born").submit("2025-02-03")
    assert state.answers["born"] == "2025-02-03"
    at(state, "born").submit("03/02/2025")
    assert "ISO date" in state.error

    at(state, "seen_at").submit("now")
    assert state.answers["seen_at"] == "2026-11-03 12:30:05"
    at(state, "seen_at").submit("today")
    assert state.answers["seen_at"] == "2026-11-03 00:00:00"
    at(state, "seen_at").submit("yesterday")
    assert "ISO datetime" in state.error


def test_sequence_is_normalised_and_asked() -> None:
    state = state_for("entity")
    state.submit("1")
    at(state, "seq").submit("acgt\nAC GT")
    assert state.answers["seq"] == "ACGTACGT"
    assert state.current().key == "parent"


# ---------------------------------------------------------------------------
# Refs, search and boxes
# ---------------------------------------------------------------------------


def test_ref_resolution_shows_name_back_and_checks_allowed_kinds() -> None:
    state = state_for("stock")
    state.submit("1")  # bacterial
    at(state, "entity").submit("e1")
    assert "this step accepts" in state.error
    at(state, "entity").submit("p1")
    assert state.answers["entity"] == "p1"
    assert state.labels["entity"] == "Plasmid One"
    at(state, "entity").submit("nope")
    assert "cannot resolve" in state.error


def test_resolved_ref_name_is_shown_back() -> None:
    state = state_for("stock")
    state.submit("1")
    at(state, "entity").submit("p1")
    assert state.notice == "Plasmid One"

    state = state_for("aliquot")
    state.submit("1")
    at(state, "stock").submit("s1")
    at(state, "count").submit("1")
    at(state, "box").submit("GRID1")
    assert state.notice == "GRID1 (grid96)"


def test_ref_search_term_and_numbered_pick() -> None:
    state = state_for("stock")
    state.submit("1")
    at(state, "entity").submit("?one")
    assert [choice.value for choice in state.choices] == ["i1"]  # p1 filtered out
    assert state.choice_title == "Search results for 'one'"
    state.submit("1")
    assert state.answers["entity"] == "i1"
    assert state.labels["entity"] == "Item One"


def test_ref_question_mark_lists_candidates() -> None:
    state = state_for("stock")
    state.submit("1")
    at(state, "entity").submit("?")
    assert {choice.value for choice in state.choices} == {"i1", "p1"}
    state.submit("2")
    assert state.answers["entity"] == "p1"


def test_box_list_search_and_direct_name() -> None:
    state = state_for("aliquot")
    state.submit("1")
    at(state, "stock").submit("s1")
    at(state, "count").submit("1")
    at(state, "box").submit("?")
    assert [choice.value for choice in state.choices] == ["GRID1", "RACK1", "SHELF"]
    state.submit("2")
    assert state.answers["box"] == "RACK1"
    assert state.labels["box"] == "RACK1 (rack)"

    at(state, "box").submit("GRID1")
    assert state.answers["box"] == "GRID1"
    at(state, "box").submit("NOPE")
    assert "cannot find box" in state.error


def test_slots_default_question_mark_and_slotless_box() -> None:
    state = state_for("aliquot")
    state.submit("1")
    at(state, "stock").submit("s1")
    at(state, "count").submit("2")
    at(state, "box").submit("GRID1")
    assert state.current().key == "slots"
    assert state.prompt().value == "A1, A2"
    state.submit("?")
    assert state.notice == "free slots: A1, A2, A3"
    state.submit("A1")
    assert "one slot per aliquot" in state.error
    state.submit("")
    assert state.answers["slots"] == ["A1", "A2"]
    assert state.mode == "review"

    # A slotless box has no slots step at all.
    state = state_for("aliquot")
    state.submit("1")
    at(state, "stock").submit("s1")
    at(state, "count").submit("1")
    at(state, "box").submit("SHELF")
    assert state.mode == "review"
    assert "slots" not in state.answers


def test_aliquot_count_minimum_is_one() -> None:
    state = state_for("aliquot")
    state.submit("1")
    at(state, "stock").submit("s1")
    at(state, "count").submit("0")
    assert "must be at least 1" in state.error
    assert "count" not in state.answers


# ---------------------------------------------------------------------------
# Long values
# ---------------------------------------------------------------------------


def _long_raw() -> str:
    """5,432 chars over 12 lines whose first/last 10 are the spec example."""
    head, tail = "ATGCGTACGT", "TTAGCCGGAA"
    first = head + "A" * 450
    middle = ["A" * 450] * 10
    last = "A" * 451 + tail
    raw = "\n".join([first, *middle, last])
    assert (len(raw), raw.count("\n") + 1) == (5432, 12)
    assert raw[:10] == head and raw[-10:] == tail
    return raw


PLACEHOLDER = "[pasted 5,432 chars · 12 lines · ATGCGTACGT…TTAGCCGGAA]"


def test_long_value_is_never_shown_in_full() -> None:
    raw = _long_raw()
    state = state_for("entity")
    state.submit("1")
    at(state, "blurb").submit(raw, pasted=True)
    assert state.answers["blurb"] == raw

    at(state, "blurb")
    prompt = state.prompt()
    assert prompt.value == PLACEHOLDER
    assert prompt.long is True
    assert prompt.long_raw == raw
    assert raw not in prompt.value

    rows = {row.label: row.value for row in state.review_rows()}
    assert rows["blurb"] == PLACEHOLDER

    # Pasting again replaces the value and the placeholder.
    replacement = "C" * 5000
    at(state, "blurb").submit(replacement, pasted=True)
    at(state, "blurb")
    assert state.prompt().value != PLACEHOLDER
    assert state.prompt().value.startswith("[pasted 5,000 chars")
    assert state.answers["blurb"] == replacement

    # Short typed input stays inline.
    at(state, "blurb").submit("short note")
    at(state, "blurb")
    assert state.answers["blurb"] == "short note"
    assert state.prompt().value == "short note"


def test_short_form_and_long_value_units() -> None:
    assert short_form(_long_raw()) == PLACEHOLDER.replace("pasted", "long")
    value = LongValue(_long_raw(), pasted=True)
    assert value.shortened()
    assert value.display() == PLACEHOLDER
    assert LongValue("ATGC").display() == "ATGC"


def test_sequence_long_value_is_shown_short_after_normalisation() -> None:
    raw = _long_raw()
    state = state_for("entity")
    state.submit("1")
    at(state, "seq").submit(raw, pasted=True)
    stored = state.answers["seq"]
    assert len(stored) == 5421 and "\n" not in stored
    at(state, "seq")
    assert state.prompt().value.startswith("[pasted 5,421 chars · 1 lines")


# ---------------------------------------------------------------------------
# Multi-value inputs
# ---------------------------------------------------------------------------


def test_aliases_one_per_line_or_commas_and_empty_line_ends() -> None:
    state = state_for("entity")
    state.submit("1")
    at(state, "aliases").submit("a\nb, c\n\nignored")
    assert state.answers["aliases"] == ["a", "b", "c"]
    at(state, "aliases").submit(" x , y , x ")
    assert state.answers["aliases"] == ["x", "y"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("a, b\nc", ["a", "b", "c"]),
        ("a\n\nb", ["a"]),
        (" , \n", []),
        ("", []),
    ],
)
def test_parse_multi(text: str, expected: list[str]) -> None:
    assert parse_multi(text) == expected


# ---------------------------------------------------------------------------
# Review screen
# ---------------------------------------------------------------------------


def test_review_rows_jump_write_and_cancel() -> None:
    state = state_for("box")
    state.submit("1")  # grid96 (sorted before grid9x9)
    state.submit("BOX-1")
    state.submit("")  # parent skipped
    state.submit("")  # notes skipped
    assert state.mode == "review"
    rows = state.review_rows()
    assert [row.number for row in rows] == [1, 2, 3, 4]
    assert [row.label for row in rows] == ["kind", "name", "parent", "notes"]
    assert [row.value for row in rows] == ["grid96", "BOX-1", "", ""]
    # The review's kind row is the kind name only: the facts live on the kind
    # step of the form, never in the review or the write payload.
    assert (rows[0].label, rows[0].value) == ("kind", "grid96")

    state.review_input("2")
    assert state.mode == "input"
    assert state.current().key == "name"
    assert state.prompt().value == "BOX-1"
    state.submit("BOX-2")
    assert state.mode == "review"  # a jump returns to the review screen

    state.review_input("q")
    assert state.mode == "cancelled"

    state = state_for("box")
    state.submit("1")
    state.submit("BOX-1")
    state.submit("")
    state.submit("")
    state.review_input("y")
    assert state.mode == "write"


def test_review_rejects_unknown_step() -> None:
    state = state_for("box")
    state.submit("1")
    state.submit("BOX-1")
    state.submit("")
    state.submit("")
    state.review_input("9")
    assert "there is no step 9" in state.error
    state.review_input("x")
    assert "enter a step number" in state.error


def test_core_rejection_focuses_the_failing_field() -> None:
    state = state_for("stock")
    state.submit("1")
    at(state, "entity").submit("p1")
    at(state, "measured_at").submit("")
    at(state, "amount").submit("")
    at(state, "strain").submit("BL21(DE3)")
    at(state, "medium").submit("lb")
    assert state.mode == "review"
    state.review_input("y")
    assert state.mode == "write"

    state.reject("missing required field(s) for kind 'bacterial': strain")
    assert state.mode == "input"
    assert state.current().key == "strain"
    assert "required" in state.error

    # The box/slots steps still exist on the aliquot form: the same rejection
    # mapping must keep working there after the stock form stopped asking them.
    state = state_for("aliquot")
    state.submit("1")
    at(state, "stock").submit("s1")
    at(state, "count").submit("1")
    at(state, "box").submit("GRID1")
    state.submit("")
    assert state.mode == "review"
    state.review_input("y")
    assert state.mode == "write"

    state.reject("box 'X' has slots: every stored aliquot needs --slot")
    assert state.mode == "input"
    assert state.current().key == "slots"  # the longest matching field wins


# ---------------------------------------------------------------------------
# Payloads handed to the core add
# ---------------------------------------------------------------------------


def test_entity_payload() -> None:
    state = state_for("entity")
    state.fill(
        {
            "name": "Thing",
            "seq": "ACGT",
            "aliases": ["t"],
            "notes": "free text",
            "color": "red",
        },
        kind="everything",
    )
    payload = state.payload()
    assert payload["kind"] == "everything"
    assert payload["name"] is None  # the kind has its own name field
    assert payload["aliases"] == ["t"]
    assert payload["values"]["name"] == "Thing"
    assert payload["values"]["notes"] == "free text"
    assert payload["values"]["seq"] == "ACGT"


def test_stock_payload_uses_core_arguments() -> None:
    state = state_for("stock")
    state.fill(
        {
            "entity": "p1",
            "strain": "BL21(DE3)",
            "medium": "lb",
        },
        kind="bacterial",
    )
    assert state.payload() == {
        "kind": "bacterial",
        "entity": "p1",
        "values": {"strain": "BL21(DE3)", "medium": "lb"},
        "aliquots": 0,
    }


def test_aliquot_and_box_payloads() -> None:
    aliquot = state_for("aliquot")
    aliquot.fill(
        {
            "stock": "s1",
            "count": "3",
            "quantity": "0.5",
            "unit": "ml",
            "box": "RACK1",
            "slots": ["top"],
        },
        kind="bacterial",
    )
    payload = aliquot.payload()
    assert payload == {
        "stock": "s1",
        "count": 3,
        "quantity": "0.5",
        "unit": "ml",
        "box": "RACK1",
        "slots": ["top"],
    }

    # Without a quantity there is no unit either: both are None, which the
    # core stores as NULL, exactly as before.
    aliquot = state_for("aliquot")
    aliquot.fill({"stock": "s1", "count": "1"}, kind="bacterial")
    payload = aliquot.payload()
    assert payload == {
        "stock": "s1",
        "count": 1,
        "quantity": None,
        "unit": None,
        "box": None,
        "slots": [],
    }

    box = state_for("box")
    box.fill(
        {"name": "BOX-1", "parent": "SHELF", "slot": None, "notes": "n"},
        kind="grid96",
    )
    assert box.payload() == {
        "kind": "grid96",
        "name": "BOX-1",
        "values": {},
        "parent": "SHELF",
        "slot": None,
        "notes": "n",
    }


# ---------------------------------------------------------------------------
# Scripted source drives the whole loop (no write)
# ---------------------------------------------------------------------------


def test_scripted_source_walks_the_steps() -> None:
    source = ScriptedSource(
        [
            Reply(text="1"),  # everything
            Reply(text="Thing"),
            Reply(action="back"),
            Reply(text="Thing Two"),
        ]
    )
    state = state_for("entity")
    while state.mode == "input" and len(source.prompts) < 4:
        state.apply(source.ask(state.prompt()))
    assert state.answers["name"] == "Thing Two"
    assert source.prompts[0].step.key == "kind"
    assert source.prompts[1].step.key == "name"
    assert source.prompts[2].step.key == "description"
    assert source.prompts[3].step.key == "name"  # after Shift+Tab
    assert source.prompts[3].value == "Thing"


# ---------------------------------------------------------------------------
# Ctrl+E editor helpers
# ---------------------------------------------------------------------------


def _step() -> Step:
    return Step(
        key="notes",
        label="notes",
        type="text",
        description="free text",
        pattern="^[A-Z]+$",
        long=True,
    )


def test_editor_text_puts_metadata_below_the_scissors_line() -> None:
    text = editor_text("hello\n# kept", _step())
    assert "# field: notes" in text
    assert "# description: free text" in text
    assert "# pattern: ^[A-Z]+$" in text
    assert SCISSORS in text
    assert text.index(SCISSORS) > text.index("hello")


def test_strip_editor_text_drops_below_scissors_and_keeps_hash_lines() -> None:
    edited = (
        "hello\n# a kept comment\nmore\n\n"
        + SCISSORS
        + "\n# field: notes\n# description: free text\n"
    )
    assert strip_editor_text(edited) == "hello\n# a kept comment\nmore"


def test_editor_command_precedence() -> None:
    assert editor_command({"VISUAL": "code -w", "EDITOR": "vim"}) == ["code", "-w"]
    assert editor_command({"EDITOR": "nano"}) == ["nano"]
    assert editor_command({}) == ["vi"]


def test_edit_value_runs_the_editor_and_strips_metadata() -> None:
    def fake_run(command: list[str], check: bool = False) -> None:
        Path(command[-1]).write_text(
            "edited\n# kept\n\n" + SCISSORS + "\n# field: notes\n",
            encoding="utf-8",
        )

    assert edit_value("old", _step(), env={"EDITOR": "vi"}, run=fake_run) == (
        "edited\n# kept"
    )
