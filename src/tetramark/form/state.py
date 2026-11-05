"""The ``add-form`` state machine (Dolt-free).

:class:`FormState` owns everything the spec calls the form: the numbered kind
step, one step per schema field, navigation (Enter / Shift+Tab / review jumps),
the per-type checks, multi-value inputs, long-value short forms, the review
screen and the payload that is handed to the *same* ``ledger.<objects>.add``
call as ``tm <obj> add``.

It is fed by an injectable :class:`~tetramark.form.source.InputSource`: the real
terminal form (:mod:`tetramark.form.tui`) or a scripted source in the tests.
Nothing here touches Dolt: ref lookups, search, box lists and free slots go
through the small :class:`Resolver` protocol, which the fast tests fake and the
CLI implements on a real :class:`~tetramark.core.ledger.Ledger`.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping, Protocol

from tetramark.errors import LedgerError
from tetramark.form.spec import FormPlan, Step
from tetramark.form.source import (
    REVIEW_KEYS,
    STEP_KEYS,
    Choice,
    Prompt,
    Reply,
    ReviewRow,
)

__all__ = [
    "LONG_INLINE_LIMIT",
    "FormError",
    "FormState",
    "LongValue",
    "Resolver",
    "parse_multi",
    "short_form",
]

#: Values longer than this (or with a newline) are shown in short form.
LONG_INLINE_LIMIT = 120

_INT_RE = re.compile(r"^[+-]?\d+$")
_BOOL_WORDS: dict[str, str] = {
    "y": "true",
    "yes": "true",
    "t": "true",
    "true": "true",
    "on": "true",
    "1": "true",
    "n": "false",
    "no": "false",
    "f": "false",
    "false": "false",
    "off": "false",
    "0": "false",
}

#: ``_coerce`` returns this for "leave the value out" (optional / skipped).
SKIP: Any = object()


class FormError(LedgerError):
    """A form-level validation error shown on the current step."""


class Resolver(Protocol):
    """Everything the state machine needs from the ledger (injectable)."""

    def resolve_ref(
        self, target: str, text: str, *, allowed_kinds: tuple[str, ...] = ()
    ) -> tuple[str, str]:  # pragma: no cover - protocol
        """``text`` -> (id, display label); raise :class:`FormError` if not found."""
        ...

    def search(
        self, target: str, term: str, *, allowed_kinds: tuple[str, ...] = ()
    ) -> list[Choice]:  # pragma: no cover - protocol
        ...

    def list_refs(
        self, target: str, *, allowed_kinds: tuple[str, ...] = ()
    ) -> list[Choice]:  # pragma: no cover - protocol
        ...

    def ref_tokens(
        self, target: str, *, allowed_kinds: tuple[str, ...] = ()
    ) -> list[str]:  # pragma: no cover - protocol
        """Ids and aliases of ``target`` for Tab completion."""
        ...

    def resolve_box(self, text: str) -> tuple[str, str]:  # pragma: no cover
        ...

    def list_boxes(self, *, parents_only: bool = False) -> list[Choice]:
        ...

    def is_slotted(self, box: str) -> bool:  # pragma: no cover - protocol
        ...

    def free_slots(self, box: str) -> list[str]:  # pragma: no cover - protocol
        ...


def short_form(text: str, *, pasted: bool = False) -> str:
    """The never-in-full rendering of a long value.

    >>> short_form("A" * 5432)
    '[long 5,432 chars · 1 lines · AAAAAAAAAA…AAAAAAAAAA]'
    """
    chars = len(text)
    lines = text.count("\n") + 1
    head = text[:10].replace("\n", " ")
    tail = text[-10:].replace("\n", " ")
    label = "pasted" if pasted else "long"
    return f"[{label} {chars:,} chars · {lines} lines · {head}…{tail}]"


@dataclass(frozen=True)
class LongValue:
    """A ``long: true`` field value plus the way it was entered."""

    text: str
    pasted: bool = False

    def shortened(self) -> bool:
        return len(self.text) > LONG_INLINE_LIMIT or "\n" in self.text

    def display(self) -> str:
        if not self.shortened():
            return self.text
        return short_form(self.text, pasted=self.pasted)


def parse_multi(text: str, *, dedupe: bool = False) -> list[str]:
    """One-per-line or comma-separated; an empty line ends the list."""
    items: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            if items:
                break
            continue
        for part in line.split(","):
            value = part.strip()
            if not value:
                continue
            if dedupe and value in items:
                continue
            items.append(value)
    return items


class FormState:
    """The form's state machine."""

    def __init__(
        self,
        plan: FormPlan,
        resolver: Resolver,
        *,
        today: Callable[[], _dt.date] | None = None,
        now: Callable[[], _dt.datetime] | None = None,
    ) -> None:
        self.plan = plan
        self.resolver = resolver
        self._today = today or _dt.date.today
        self._now = now or _dt.datetime.now

        self.kind: str | None = None
        self.answers: dict[str, Any] = {}
        self.labels: dict[str, str] = {}
        self.long_values: dict[str, str] = {}
        self.long_pasted: dict[str, bool] = {}

        self.index = 0
        self.mode = "input"  # input | review | write | done | cancelled
        self.error: str | None = None
        self.notice: str | None = None
        self.from_review = False

        self.choices: tuple[Choice, ...] = ()
        self.choice_title: str | None = None
        self.choice_kind: str | None = None

    # -- steps -------------------------------------------------------------
    def all_steps(self) -> list[Step]:
        steps = [self.plan.kind_step()]
        if self.kind is not None:
            steps.extend(self.plan.steps(self.kind))
        return steps

    def visible_steps(self) -> list[Step]:
        steps = self.all_steps()
        if self.kind is None:
            return steps
        object_type = self.plan.object_type
        result: list[Step] = []
        for step in steps:
            if step.key == "box" and object_type in ("stock", "aliquot"):
                if self._count() <= 0:
                    continue
            elif step.key == "slots":
                box = self.answers.get("box")
                if not box or self._count() <= 0:
                    continue
                if not self._slotted(box):
                    continue
            elif step.key == "slot":
                parent = self.answers.get("parent")
                if not parent or not self._slotted(parent):
                    continue
            elif step.key == "unit" and object_type == "aliquot":
                # The unit is only asked once a quantity is there: a unit
                # without a quantity is unreachable (the core refuses it).
                if not self.answers.get("quantity"):
                    continue
            result.append(step)
        return result

    def current(self) -> Step | None:
        visible = self.visible_steps()
        if not visible:
            return None
        return visible[min(self.index, len(visible) - 1)]

    def _count(self) -> int:
        key = "aliquots" if self.plan.object_type == "stock" else "count"
        raw = self.answers.get(key)
        if raw is None:
            return 1  # the default of both count steps
        try:
            return int(str(raw))
        except (TypeError, ValueError):
            return 1

    def _slotted(self, box: str) -> bool:
        try:
            return bool(self.resolver.is_slotted(box))
        except FormError:
            return False

    # -- prompts -----------------------------------------------------------
    def prompt(self) -> Prompt:
        """What the source should ask next."""
        visible = self.visible_steps()
        if self.mode == "review":
            return Prompt(
                mode="review",
                total=len(visible),
                rows=tuple(self.review_rows()),
                error=self.error,
                notice=self.notice,
                footer=REVIEW_KEYS,
            )
        step = self.current()
        if step is None:  # pragma: no cover - a plan always has the kind step
            raise FormError("the form has no steps to ask")
        index = visible.index(step)
        value = ""
        long_raw: str | None = None
        if step.key in self.answers:
            raw = self.answers[step.key]
            if isinstance(raw, list):
                value = ", ".join(str(item) for item in raw)
            else:
                value = str(raw)
            if step.long and step.key in self.long_values:
                long_raw = self.long_values[step.key]
                value = LongValue(long_raw, self.long_pasted.get(step.key, False)).display()
        elif step.type == "slots":
            value = self._default_slots()
        if step.type == "kind" and self.kind is not None:
            value = self.kind
        return Prompt(
            mode="step",
            step=step,
            index=index,
            total=len(visible),
            value=value,
            long=step.long,
            long_raw=long_raw,
            error=self.error,
            notice=self.notice,
            choices=(
                tuple(
                    Choice(
                        value=choice.name,
                        label=choice.name,
                        detail=choice.description or "",
                        facts=choice.facts,
                    )
                    for choice in step.choices
                )
                if step.type == "kind"
                else self.choices
            ),
            choice_title=(
                "Choose a kind" if step.type == "kind" and not self.choices else self.choice_title
            ),
            completions=self._completions(step),
            footer=STEP_KEYS,
        )

    def _completions(self, step: Step) -> tuple[str, ...]:
        """Tab-completion candidates for the current step (empty on error)."""
        try:
            if step.type == "kind":
                return tuple(choice.name for choice in self.plan.kinds)
            if step.type == "enum":
                return tuple(step.values)
            if step.type == "ref" and step.target is not None:
                return tuple(
                    self.resolver.ref_tokens(
                        step.target, allowed_kinds=step.allowed_kinds
                    )
                )
            if step.type == "box":
                return tuple(
                    choice.value
                    for choice in self.resolver.list_boxes(
                        parents_only=step.key == "parent"
                    )
                )
            if step.type == "slots" and self.answers.get("box"):
                return tuple(self.resolver.free_slots(str(self.answers["box"])))
            if step.type == "slot" and self.answers.get("parent"):
                return tuple(
                    self.resolver.free_slots(str(self.answers["parent"]))
                )
        except FormError:
            return ()
        return ()

    def _default_slots(self) -> str:
        box = self.answers.get("box")
        if not box:
            return ""
        try:
            free = self.resolver.free_slots(str(box))
        except FormError:
            return ""
        return ", ".join(free[: max(self._count(), 0)])

    # -- input -------------------------------------------------------------
    def apply(self, reply: Reply) -> None:
        """Apply one source answer."""
        if reply.action == "abort":
            self.mode = "cancelled"
            return
        if reply.action == "back":
            self.back()
            return
        if self.mode == "review":
            self.review_input(reply.text)
            return
        self.submit(reply.text, pasted=reply.pasted)

    def submit(self, text: str, *, pasted: bool = False) -> None:
        """Validate one answer for the current step and advance."""
        if self.choices:
            self._choose(text)
            return
        step = self.current()
        if step is None:  # pragma: no cover
            return
        if step.type == "kind":
            self._choose_kind(text)
            return
        if text.startswith("?") and step.type in ("ref", "box", "slots"):
            self._search(step, text)
            return
        if (
            step.type == "slots"
            and not text.strip()
            and self.answers.get("box")
            and "slots" not in self.answers
        ):
            # Enter accepts the next free slots (the prompt shows them).
            text = self._default_slots()
        try:
            value, label = self._coerce(step, text, pasted=pasted)
        except FormError as exc:
            self.error = str(exc)
            self.notice = None
            return
        if value is SKIP:
            self._skip(step)
        else:
            self._store(step, value, label, pasted=pasted)
        self._advance()
        if label is not None and step.type in ("ref", "box"):
            # Spec: a reference is looked up right away and its name is shown
            # back.  The notice rides on the next prompt (and on the review).
            self.notice = label

    def back(self) -> None:
        """Shift+Tab: one step back, with the value pre-filled."""
        if self.choices:
            self.choices = ()
            self.choice_title = None
            self.choice_kind = None
            return
        self.error = None
        self.notice = None
        if self.mode == "review":
            self.mode = "input"
            self.index = max(len(self.visible_steps()) - 1, 0)
            return
        if self.index > 0:
            self.index -= 1
        else:
            self.notice = "This is the first step."

    # -- kind list ---------------------------------------------------------
    def _choose_kind(self, text: str) -> None:
        choices = self.plan.kinds
        answer = text.strip()
        selected: str | None = None
        if not answer:
            if len(choices) == 1:
                selected = choices[0].name
            else:
                self.error = (
                    "choose a kind by number (1-"
                    f"{len(choices)}) or name; Enter accepts only a single kind"
                )
                return
        elif _INT_RE.fullmatch(answer) and 1 <= int(answer) <= len(choices):
            selected = choices[int(answer) - 1].name
        else:
            for choice in choices:
                if choice.name == answer:
                    selected = choice.name
                    break
        if selected is None:
            self.error = f"unknown kind {answer!r}; choose a number 1-{len(choices)} or a name"
            return
        self._set_kind(selected)
        self._advance()

    def _set_kind(self, name: str) -> None:
        if self.kind != name:
            # A different kind has different fields: nothing carries over.
            self.answers.clear()
            self.labels.clear()
            self.long_values.clear()
            self.long_pasted.clear()
            self.choices = ()
            self.choice_title = None
            self.choice_kind = None
        self.kind = name
        self.answers["kind"] = name
        self.error = None
        self.notice = None

    # -- search / numbered choices ----------------------------------------
    def _search(self, step: Step, text: str) -> None:
        term = text[1:].strip()
        try:
            if step.type == "slots":
                box = self.answers.get("box")
                if not box:
                    self.error = "choose a box first"
                    return
                free = self.resolver.free_slots(str(box))
                self.notice = (
                    "free slots: " + (", ".join(free) if free else "(none)")
                )
                self.error = None
                return
            if step.type == "box":
                options = self.resolver.list_boxes(parents_only=step.key == "parent")
                if term:
                    lowered = term.casefold()
                    options = [
                        choice
                        for choice in options
                        if lowered in choice.label.casefold()
                    ]
                title = f"Boxes matching {term!r}" if term else "Choose a box"
            else:
                assert step.target is not None
                if term:
                    options = self.resolver.search(
                        step.target, term, allowed_kinds=step.allowed_kinds
                    )
                    title = f"Search results for {term!r}"
                else:
                    options = self.resolver.list_refs(
                        step.target, allowed_kinds=step.allowed_kinds
                    )
                    title = f"Choose a {step.target.replace('_', ' ')}"
        except FormError as exc:
            self.error = str(exc)
            self.notice = None
            return
        if not options:
            self.notice = f"no match for {term!r}" if term else "nothing to choose from"
            self.error = None
            return
        self.choices = tuple(options)
        self.choice_title = title
        self.choice_kind = step.type
        self.error = None
        self.notice = None

    def _choose(self, text: str) -> None:
        answer = text.strip()
        if not answer:
            self.choices = ()
            self.choice_title = None
            self.choice_kind = None
            return
        if not _INT_RE.fullmatch(answer) or not (
            1 <= int(answer) <= len(self.choices)
        ):
            self.error = f"pick a number 1-{len(self.choices)} (or press Enter to cancel)"
            return
        choice = self.choices[int(answer) - 1]
        step = self.current()
        self.choices = ()
        self.choice_title = None
        self.choice_kind = None
        if step is None:  # pragma: no cover
            return
        self._store(step, choice.value, choice.label, pasted=False)
        self._advance()
        if step.type in ("ref", "box") and choice.label:
            self.notice = choice.label

    # -- validation --------------------------------------------------------
    def _coerce(
        self, step: Step, text: str, *, pasted: bool
    ) -> tuple[Any, str | None]:
        """Validate one textual answer, returning (value, display label)."""
        raw = text
        if not raw.strip() and step.default is not None:
            raw = step.default

        empty = not raw.strip()
        if empty:
            if step.type == "slot":
                parent = self.answers.get("parent")
                if parent and self._slotted(str(parent)):
                    raise FormError("the parent box has slots: a slot is required")
            if step.required:
                raise FormError(f"{step.label} is required")
            return SKIP, None

        value: Any = raw
        label: str | None = None
        kind = step.type

        if kind in ("string", "text"):
            value = raw
        elif kind == "int":
            if not _INT_RE.fullmatch(raw.strip()):
                raise FormError(f"{step.label} must be an integer (got {raw!r})")
            value = str(int(raw.strip()))
        elif kind == "decimal":
            try:
                number = Decimal(raw.strip())
            except (InvalidOperation, ValueError):
                raise FormError(
                    f"{step.label} must be a decimal number (got {raw!r})"
                ) from None
            if not number.is_finite():
                raise FormError(f"{step.label} must be a finite number")
            value = str(number)
        elif kind == "quantity":
            # The core's rules, word for word: a decimal, finite and not
            # negative; zero is a quantity like any other.  The answer is kept
            # as typed (trimmed), so the core parses exactly what was typed.
            text = raw.strip()
            try:
                number = Decimal(text)
            except (InvalidOperation, ValueError):
                raise FormError(
                    f"{step.label} must be a decimal number (got {text!r})"
                ) from None
            if not number.is_finite() or number < 0:
                raise FormError(
                    f"{step.label} must be a finite, non-negative number "
                    f"(got {text!r})"
                )
            value = text
        elif kind == "unit":
            # Free text: trimmed, not empty (the required check above says so)
            # and at most 32 characters, like the core's unit.
            text = raw.strip()
            if len(text) > 32:
                raise FormError(f"{step.label} must be at most 32 characters")
            value = text
        elif kind == "bool":
            word = _BOOL_WORDS.get(raw.strip().lower())
            if word is None:
                raise FormError(f"{step.label} must be y or n (got {raw!r})")
            value = word
        elif kind == "date":
            value = self._date(raw, step)
        elif kind == "datetime":
            value = self._datetime(raw, step)
        elif kind == "enum":
            value = self._enum(step, raw)
        elif kind == "sequence":
            value = re.sub(r"\s+", "", raw).upper()
            if not value:
                raise FormError(f"{step.label} is a sequence and must not be empty")
        elif kind == "ref":
            if step.target is None:  # pragma: no cover - schema guarantees it
                raise FormError(f"{step.label} has no reference target")
            value, label = self.resolver.resolve_ref(
                step.target, raw.strip(), allowed_kinds=step.allowed_kinds
            )
        elif kind == "box":
            value, label = self.resolver.resolve_box(raw.strip())
        elif kind == "slots":
            values = parse_multi(raw)
            if not values:
                raise FormError(f"{step.label}: give at least one slot")
            expected = self._count()
            if len(values) != expected:
                raise FormError(
                    f"{step.label}: give one slot per aliquot "
                    f"({expected} expected, {len(values)} given)"
                )
            value = values
        elif kind == "slot":
            slot = raw.strip()
            parent = self.answers.get("parent")
            if parent:
                free = self.resolver.free_slots(str(parent))
                if free and slot not in free:
                    raise FormError(
                        f"slot {slot!r} is not free in box {parent!r} "
                        f"(free: {', '.join(free)})"
                    )
            value = slot
        elif kind == "aliases":
            value = parse_multi(raw, dedupe=True)
            if not value:
                return SKIP, None
        elif kind == "count":
            if not _INT_RE.fullmatch(raw.strip()):
                raise FormError(f"{step.label} must be an integer (got {raw!r})")
            number = int(raw.strip())
            if number < step.minimum:
                raise FormError(
                    f"{step.label} must be at least {step.minimum} (got {number})"
                )
            value = str(number)
        elif kind in ("computed", "kind"):  # pragma: no cover - never asked
            raise FormError(f"field {step.label!r} cannot be filled in here")
        else:  # pragma: no cover - Step.type is exhaustive
            raise FormError(f"field {step.label!r} has an unsupported type {kind!r}")

        if step.pattern is not None and isinstance(value, str):
            if re.fullmatch(step.pattern, value) is None:
                raise FormError(
                    f"{step.label} does not match the pattern {step.pattern!r} "
                    f"(got {value!r})"
                )
        return value, label

    def _date(self, raw: str, step: Step) -> str:
        word = raw.strip().lower()
        if word in ("today", "now"):
            return self._today().isoformat()
        try:
            return _dt.date.fromisoformat(raw.strip()).isoformat()
        except ValueError:
            raise FormError(
                f"{step.label} must be an ISO date YYYY-MM-DD "
                f"(or today; got {raw!r})"
            ) from None

    def _datetime(self, raw: str, step: Step) -> str:
        word = raw.strip().lower()
        if word == "now":
            return self._now().replace(microsecond=0).isoformat(sep=" ")
        if word == "today":
            return _dt.datetime.combine(self._today(), _dt.time()).isoformat(
                sep=" "
            )
        try:
            return _dt.datetime.fromisoformat(raw.strip()).isoformat(sep=" ")
        except ValueError:
            raise FormError(
                f"{step.label} must be an ISO datetime "
                f"(or now; got {raw!r})"
            ) from None

    def _enum(self, step: Step, raw: str) -> str:
        answer = raw.strip()
        values = list(step.values)
        if _INT_RE.fullmatch(answer) and 1 <= int(answer) <= len(values):
            return values[int(answer) - 1]
        if answer in values:
            return answer
        raise FormError(
            f"{step.label} must be one of: {', '.join(values)} "
            f"(pick by value or number; got {raw!r})"
        )

    # -- storing / advancing ----------------------------------------------
    def _store(
        self, step: Step, value: Any, label: str | None, *, pasted: bool
    ) -> None:
        self.answers[step.key] = value
        if label is not None:
            self.labels[step.key] = label
        if step.long:
            self.long_values[step.key] = str(value)
            self.long_pasted[step.key] = pasted
        if step.key == "box":
            # A new box may have different free slots; drop the old placement.
            self.answers.pop("slots", None)
        if step.key == "parent":
            self.answers.pop("slot", None)

    def _skip(self, step: Step) -> None:
        self.answers.pop(step.key, None)
        self.labels.pop(step.key, None)
        self.long_values.pop(step.key, None)
        self.long_pasted.pop(step.key, None)
        if step.key == "box":
            self.answers.pop("slots", None)
        if step.key == "parent":
            self.answers.pop("slot", None)
        if step.key == "quantity" and self.plan.object_type == "aliquot":
            # A quantity and a unit go together: without a quantity there is
            # no unit, so a cleared quantity drops the unit as well.
            self.answers.pop("unit", None)
            self.labels.pop("unit", None)

    def _advance(self) -> None:
        self.error = None
        self.notice = None
        if self.from_review:
            self.from_review = False
            self.mode = "review"
            return
        self.index += 1
        if self.index >= len(self.visible_steps()):
            self.mode = "review"

    # -- review ------------------------------------------------------------
    def review_rows(self) -> list[ReviewRow]:
        rows: list[ReviewRow] = []
        for number, step in enumerate(self.visible_steps(), start=1):
            rows.append(
                ReviewRow(number=number, label=step.label, value=self.display(step))
            )
        return rows

    def display(self, step: Step) -> str:
        if step.key not in self.answers:
            return ""
        value = self.answers[step.key]
        if step.long and step.key in self.long_values:
            return LongValue(
                self.long_values[step.key],
                self.long_pasted.get(step.key, False),
            ).display()
        if step.key in self.labels and not isinstance(value, list):
            return self.labels[step.key]
        if isinstance(value, list):
            return ", ".join(str(item) for item in value)
        return str(value)

    def review_input(self, text: str) -> None:
        answer = text.strip()
        if answer in ("y", "Y"):
            self.error = None
            self.mode = "write"
            return
        if answer in ("q", "Q"):
            self.error = None
            self.mode = "cancelled"
            return
        if _INT_RE.fullmatch(answer):
            self.jump(int(answer))
            return
        self.error = "enter a step number to jump back, y to write or q to cancel"

    def jump(self, number: int) -> None:
        visible = self.visible_steps()
        if not 1 <= number <= len(visible):
            self.error = f"there is no step {number} (1-{len(visible)})"
            return
        self.index = number - 1
        self.mode = "input"
        self.from_review = True
        self.error = None
        self.notice = None

    # -- write -------------------------------------------------------------
    def payload(self) -> dict[str, Any]:
        """The keyword arguments for the object's ``add`` operation."""
        object_type = self.plan.object_type
        kind = self.kind
        assert kind is not None
        answers = self.answers
        kind_object = self.plan.schema.kind(kind)
        field_values = {
            name: answers[name] for name in kind_object.fields if name in answers
        }
        if object_type == "entity":
            values = dict(field_values)
            if "notes" in answers:
                values["notes"] = answers["notes"]
            name = answers.get("name") if "name" not in kind_object.fields else None
            return {
                "kind": kind,
                "values": values,
                "name": name,
                "aliases": list(answers.get("aliases") or []),
            }
        if object_type == "stock":
            return {
                "kind": kind,
                "entity": answers["entity"],
                "values": field_values,
                "aliquots": self._count(),
                "box": answers.get("box"),
                "slots": list(answers.get("slots") or []),
            }
        if object_type == "aliquot":
            return {
                "stock": answers["stock"],
                "count": self._count(),
                # Always present: None means "no quantity", which the core
                # stores as NULL, exactly as before.
                "quantity": answers.get("quantity"),
                "unit": answers.get("unit"),
                "box": answers.get("box"),
                "slots": list(answers.get("slots") or []),
            }
        return {
            "kind": kind,
            "name": answers["name"],
            "values": field_values,
            "parent": answers.get("parent"),
            "slot": answers.get("slot"),
            "notes": answers.get("notes"),
        }

    def reject(self, message: str) -> None:
        """The core rejected the write: show it and go back to the field."""
        self.error = message
        self.mode = "input"
        self.from_review = False
        step = self._field_for_error(message)
        if step is None:
            self.mode = "review"
            return
        visible = self.visible_steps()
        if step in visible:
            self.index = visible.index(step)

    def _field_for_error(self, message: str) -> Step | None:
        candidates = [
            step
            for step in self.visible_steps()
            if step.type not in ("kind",) and step.key != "kind"
        ]
        # Longest key first, so 'name' does not win over 'category_name'.
        for step in sorted(candidates, key=lambda item: len(item.key), reverse=True):
            if re.search(rf"(?<![\w-]){re.escape(step.key)}(?![\w-])", message):
                return step
        for step in candidates:
            if step.label and step.label in message:
                return step
        return None

    # -- convenience for tests --------------------------------------------
    def fill(self, values: Mapping[str, Any], *, kind: str) -> None:
        """Seed answers directly (tests); does not run validation."""
        self._set_kind(kind)
        for key, value in values.items():
            self.answers[key] = value
