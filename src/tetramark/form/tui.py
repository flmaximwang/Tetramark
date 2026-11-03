"""The real terminal input source of ``tm <obj> add-form``.

:class:`PromptToolkitSource` shows one :class:`~tetramark.form.source.Prompt`
with prompt_toolkit and turns the key presses into the small reply vocabulary
of :mod:`tetramark.form.source`.  Navigation, per-type validation, the review
screen and the write all live in :mod:`tetramark.form.state`; this module only
knows how to ask one question and read one answer, which is why it is imported
lazily by :mod:`tetramark.form.session` and never by the fast state tests.

The interesting cases are :func:`tetramark.form.editor.edit_value` (Ctrl+E:
exit the prompt, run the editor, re-prompt with the edited answer) and the
``long: true`` fields (bracketed paste shows a short form but submits the full
text -- the full text is never rendered).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import (
    CompleteEvent,
    Completer,
    Completion,
    WordCompleter,
)
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.input import Input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.output import Output
from prompt_toolkit.styles import Style

from tetramark.form import editor
from tetramark.form.source import Choice, Prompt, Reply
from tetramark.form.state import LongValue

__all__ = ["PromptToolkitSource"]


_STYLE = Style.from_dict(
    {
        "header": "bold",
        "required": "bold ansired",
        "description": "ansibrightblack",
        "help": "ansibrightblack italic",
        "choice-number": "bold",
        "choice-detail": "ansibrightblack",
        "notice": "ansiyellow",
        "error": "bold ansired",
        "review-number": "bold",
        "review-label": "bold",
        "prompt": "",
    }
)


class _Sentinel:
    """A non-text prompt result (back, abort, edit); never confused with input."""

    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{self.name}>"


_BACK = _Sentinel("back")
_ABORT = _Sentinel("abort")
_EDIT = _Sentinel("edit")


@dataclass(frozen=True)
class _EditRequest:
    """Ctrl+E: leave the prompt with the buffer text for the external editor."""

    text: str


@dataclass
class _Draft:
    """The part of a long field's state that survives a Ctrl+E round trip."""

    text: str
    long_raw: str | None = None
    placeholder: str | None = None
    pasted: bool = False


def _choice_text(choice: Choice) -> str:
    """The primary label of a numbered choice (id included when it adds info)."""
    text = choice.label or choice.value
    if choice.value and choice.value != text and choice.value not in text:
        text = f"{text} ({choice.value})"
    return text


def _message(prompt: Prompt) -> FormattedText:
    """The header above the input line (a long value is never shown in full)."""
    fragments: list[tuple[str, str]] = []
    if prompt.mode == "review":
        for row in prompt.rows:
            fragments.append(("class:review-number", f" {row.number:>2}. "))
            fragments.append(("class:review-label", f"{row.label}: "))
            fragments.append(("", f"{row.value}\n"))
        if prompt.notice:
            fragments.append(("class:notice", f"  {prompt.notice}\n"))
        if prompt.error:
            fragments.append(("class:error", f"  {prompt.error}\n"))
        fragments.append(("", "review> "))
        return FormattedText(fragments)

    step = prompt.step
    if step is None:  # pragma: no cover - the state machine always sets it
        raise ValueError("a step prompt needs a step")
    fragments.append(
        ("class:header", f"{prompt.index + 1}/{prompt.total}  {step.label}")
    )
    if step.required:
        fragments.append(("class:required", " *"))
    fragments.append(("", "\n"))
    if step.description:
        fragments.append(("class:description", f"  {step.description}\n"))
    if step.help:
        fragments.append(("class:help", f"  {step.help}\n"))

    if prompt.choices:
        if prompt.choice_title:
            fragments.append(("class:header", f"  {prompt.choice_title}\n"))
        for number, choice in enumerate(prompt.choices, start=1):
            fragments.append(("class:choice-number", f"  {number}. "))
            fragments.append(("", _choice_text(choice)))
            if choice.detail:
                fragments.append(("class:choice-detail", f"  {choice.detail}"))
            fragments.append(("", "\n"))
    elif step.type == "enum" and step.values:
        # Enum values can be picked by number as well as by value.
        for number, value in enumerate(step.values, start=1):
            fragments.append(("class:choice-number", f"  {number}. "))
            fragments.append(("", f"{value}\n"))

    if prompt.notice:
        fragments.append(("class:notice", f"  {prompt.notice}\n"))
    if prompt.error:
        fragments.append(("class:error", f"  {prompt.error}\n"))
    fragments.append(("class:prompt", f"{step.label}> "))
    return FormattedText(fragments)


class PromptToolkitSource:
    """Ask the form's questions on the terminal (the real
    :class:`~tetramark.form.source.InputSource`)."""

    def __init__(
        self, *, input: Input | None = None, output: Output | None = None
    ) -> None:
        self._input = input
        self._output = output

    # -- public entry point -------------------------------------------------
    def ask(self, prompt: Prompt) -> Reply:
        """Render ``prompt`` and return the user's answer."""
        if prompt.mode == "review":
            return self._ask_review(prompt)
        return self._ask_step(prompt)

    # -- step prompt --------------------------------------------------------
    def _ask_step(self, prompt: Prompt) -> Reply:
        step = prompt.step
        assert step is not None
        draft = _Draft(
            text=prompt.value,
            long_raw=prompt.long_raw,
            # The short form of a stored long value: accepting it unchanged
            # submits the full text without ever showing it.
            placeholder=(
                prompt.value
                if prompt.long and prompt.long_raw is not None
                else None
            ),
            pasted=prompt.long_raw is not None,
        )
        while True:
            outcome = self._step_once(prompt, draft)
            if outcome is _BACK:
                return Reply(action="back")
            if outcome is _ABORT:
                return Reply(action="abort")
            if isinstance(outcome, _EditRequest):
                self._edit(prompt, draft, outcome.text)
                continue
            text = str(outcome)
            if (
                prompt.long
                and draft.long_raw is not None
                and text == draft.placeholder
            ):
                return Reply(text=draft.long_raw, pasted=draft.pasted)
            if (
                prompt.long
                and draft.placeholder is not None
                and text.startswith(draft.placeholder)
                and text != draft.placeholder
            ):
                # The placeholder is a label, not text: typing after it answers
                # with what was typed, replacing the stored long value.
                text = text[len(draft.placeholder) :]
            return Reply(text=text)

    def _edit(self, prompt: Prompt, draft: _Draft, buffer_text: str) -> None:
        """Ctrl+E: run the editor on the current answer, then re-prompt."""
        step = prompt.step
        assert step is not None
        value = buffer_text
        if (
            prompt.long
            and draft.long_raw is not None
            and buffer_text == draft.placeholder
        ):
            value = draft.long_raw
        edited = editor.edit_value(value, step)
        if prompt.long:
            long_value = LongValue(edited)
            if long_value.shortened():
                draft.long_raw = edited
                draft.placeholder = long_value.display()
                draft.pasted = False
                draft.text = draft.placeholder
            else:
                draft.long_raw = None
                draft.placeholder = None
                draft.pasted = False
                draft.text = edited
        else:
            draft.text = edited

    def _step_once(self, prompt: Prompt, draft: _Draft) -> object:
        """Run one prompt_toolkit session; returns text or a sentinel."""
        step = prompt.step
        assert step is not None
        completer = WordCompleter(list(prompt.completions), ignore_case=False)
        bindings = KeyBindings()

        @bindings.add(Keys.Enter)
        def _accept(event: Any) -> None:
            event.current_buffer.validate_and_handle()

        @bindings.add(Keys.Escape, Keys.Enter)
        def _newline(event: Any) -> None:
            event.current_buffer.insert_text("\n")

        @bindings.add(Keys.ControlJ)
        def _newline_ctrl_j(event: Any) -> None:
            event.current_buffer.insert_text("\n")

        @bindings.add("s-tab")
        def _back(event: Any) -> None:
            event.app.exit(result=_BACK)

        @bindings.add(Keys.ControlE)
        def _edit(event: Any) -> None:
            event.app.exit(result=_EditRequest(event.current_buffer.text))

        @bindings.add(Keys.Tab)
        def _complete(event: Any) -> None:
            self._complete(event.current_buffer, completer)

        @bindings.add(Keys.BracketedPaste)
        def _paste(event: Any) -> None:
            buffer = event.current_buffer
            data = event.data
            if not prompt.long:
                buffer.insert_text(data)
                return
            long_value = LongValue(data, pasted=True)
            display = long_value.display()
            buffer.text = display
            buffer.cursor_position = len(display)
            if long_value.shortened():
                draft.long_raw = data
                draft.placeholder = display
                draft.pasted = True
            else:
                draft.long_raw = None
                draft.placeholder = None
                draft.pasted = False

        @bindings.add(Keys.ControlH)  # the backspace key
        def _backspace(event: Any) -> None:
            buffer = event.current_buffer
            if draft.placeholder is not None and buffer.text == draft.placeholder:
                buffer.text = ""
                draft.long_raw = None
                draft.placeholder = None
                draft.pasted = False
                return
            buffer.delete_before_cursor(1)

        session = self._session(prompt, bindings, completer)
        try:
            return session.prompt(_message(prompt), default=draft.text)
        except (KeyboardInterrupt, EOFError):
            return _ABORT

    @staticmethod
    def _complete(buffer: Any, completer: Completer) -> None:
        """Tab: complete from the prompt's candidates synchronously.

        A single candidate (or a common prefix that extends the typed word) is
        applied immediately; several unrelated candidates open the usual
        prompt_toolkit completion menu instead.
        """
        completions = list(
            completer.get_completions(
                buffer.document, CompleteEvent(completion_requested=True)
            )
        )
        if not completions:
            return
        if len(completions) == 1:
            buffer.apply_completion(completions[0])
            return
        common = os.path.commonprefix(
            [completion.text for completion in completions]
        )
        start_position = completions[0].start_position
        if len(common) > -start_position:
            buffer.apply_completion(
                Completion(common, start_position=start_position)
            )
        else:
            buffer.start_completion(select_first=False)

    # -- review prompt ------------------------------------------------------
    def _ask_review(self, prompt: Prompt) -> Reply:
        bindings = KeyBindings()

        @bindings.add(Keys.Enter)
        def _accept(event: Any) -> None:
            event.current_buffer.validate_and_handle()

        @bindings.add("s-tab")
        def _back(event: Any) -> None:
            event.app.exit(result=_BACK)

        session = self._session(prompt, bindings)
        try:
            outcome = session.prompt(_message(prompt), default="")
        except (KeyboardInterrupt, EOFError):
            return Reply(action="abort")
        if outcome is _BACK:
            return Reply(action="back")
        return Reply(text=str(outcome))

    # -- session plumbing ---------------------------------------------------
    def _session(
        self,
        prompt: Prompt,
        bindings: KeyBindings,
        completer: Completer | None = None,
    ) -> PromptSession:
        options: dict[str, Any] = {}
        if self._input is not None:
            options["input"] = self._input
        if self._output is not None:
            options["output"] = self._output
        return PromptSession(
            multiline=True,
            complete_while_typing=False,
            key_bindings=bindings,
            completer=completer,
            bottom_toolbar=prompt.footer or None,
            style=_STYLE,
            **options,
        )
