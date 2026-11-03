"""The input source a form is fed by (spec: "the form is a state machine fed
by an injectable input source").

:class:`InputSource` is the seam between the state machine
(:mod:`tetramark.form.state`) and whatever asks the questions:

* :class:`~tetramark.form.tui.PromptToolkitSource` renders the real terminal
  form with prompt_toolkit;
* :class:`ScriptedSource` replays a fixed list of :class:`Reply` objects, so
  the Dolt end-to-end tests (and any future scripting) can drive a whole form
  without a terminal.

The source only answers one :class:`Prompt` at a time: navigation, validation,
the review screen and the write are all state-machine concerns.  That keeps
the fast tests Dolt-free and terminal-free.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence

from tetramark.form.spec import Step

__all__ = [
    "REVIEW_KEYS",
    "STEP_KEYS",
    "Choice",
    "InputSource",
    "Prompt",
    "Reply",
    "ReviewRow",
    "ScriptedSource",
]

#: The footer of a normal step (the form's own key map).
STEP_KEYS = (
    "Enter next · Shift+Tab back · Tab complete · Alt+Enter newline · "
    "Ctrl+E edit · Ctrl+C abort"
)

#: The footer of the review screen.
REVIEW_KEYS = "number jump · y write · q cancel"


@dataclass(frozen=True)
class Choice:
    """One entry of a numbered list (kind list, search hit, box list)."""

    value: str
    label: str
    detail: str = ""


@dataclass(frozen=True)
class ReviewRow:
    """One numbered line of the review screen."""

    number: int
    label: str
    value: str


@dataclass(frozen=True)
class Prompt:
    """Everything a source needs to ask one question."""

    mode: str  # "step" | "review"
    step: Step | None = None
    index: int = 0  # 0-based index among the visible steps
    total: int = 0
    #: Text the input line starts with (the current value, or the long
    #: field's short form -- never a long value in full).
    value: str = ""
    #: ``True`` for a ``long: true`` field.
    long: bool = False
    #: The full text behind a long field's short form; sources must not show
    #: it, but must submit it unchanged when the placeholder is accepted.
    long_raw: str | None = None
    error: str | None = None
    notice: str | None = None
    #: Pending numbered choices (kind list always; search hits / boxes when
    #: the user asked for them).
    choices: tuple[Choice, ...] = ()
    choice_title: str | None = None
    #: Tab-completion candidates of the current step (enum values, ref
    #: ids/aliases, box names, free slots).
    completions: tuple[str, ...] = ()
    rows: tuple[ReviewRow, ...] = ()
    footer: str = ""


@dataclass(frozen=True)
class Reply:
    """One answer from a source."""

    action: str = "value"  # "value" | "back" | "abort"
    text: str = ""
    #: The answer came from a bracketed paste (long-field handling).
    pasted: bool = False


class InputSource(Protocol):
    """The injectable input source of a form."""

    def ask(self, prompt: Prompt) -> Reply:  # pragma: no cover - protocol
        ...


class ScriptedSource:
    """A source that replays a script (tests, and programmatic forms).

    >>> source = ScriptedSource([Reply(text="item"), Reply(text="1")])
    >>> source.ask(prompt).text
    'item'

    Prompts are recorded on :attr:`prompts`, so a test can assert what the
    user was asked (and that long values were never shown in full).
    """

    def __init__(self, replies: Sequence[Reply] = ()) -> None:
        self._replies = list(replies)
        self.prompts: list[Prompt] = []

    def push(self, *replies: Reply) -> None:
        self._replies.extend(replies)

    def ask(self, prompt: Prompt) -> Reply:
        self.prompts.append(prompt)
        if not self._replies:
            raise AssertionError(
                f"the script ran out of replies at mode={prompt.mode!r} "
                f"step={(prompt.step.key if prompt.step else None)!r}"
            )
        return self._replies.pop(0)
