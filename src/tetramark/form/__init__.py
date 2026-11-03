"""Interactive ``tm <obj> add-form`` (Tetramark-se9).

The form is for people: it asks the questions a preset defines, so nobody has
to remember field names or flag syntax.  Agents and scripts keep using
``tm <obj> add``; both paths end in the same core ``add`` operation.

* :mod:`tetramark.form.spec` -- the questions a plan asks (pure);
* :mod:`tetramark.form.state` -- the state machine (pure, Dolt-free);
* :mod:`tetramark.form.source` -- the injectable input source;
* :mod:`tetramark.form.tui` -- the prompt_toolkit terminal layer;
* :mod:`tetramark.form.session` -- review, confirm, write.
"""

from __future__ import annotations

from tetramark.form.session import run_add_form, write_payload
from tetramark.form.source import (
    REVIEW_KEYS,
    STEP_KEYS,
    Choice,
    InputSource,
    Prompt,
    Reply,
    ReviewRow,
    ScriptedSource,
)
from tetramark.form.spec import FormPlan, KindChoice, Step, build_plan
from tetramark.form.state import (
    FormError,
    FormState,
    LongValue,
    parse_multi,
    short_form,
)

__all__ = [
    "REVIEW_KEYS",
    "STEP_KEYS",
    "Choice",
    "FormError",
    "FormPlan",
    "FormState",
    "InputSource",
    "KindChoice",
    "LongValue",
    "Prompt",
    "Reply",
    "ReviewRow",
    "ScriptedSource",
    "Step",
    "build_plan",
    "parse_multi",
    "run_add_form",
    "short_form",
    "write_payload",
]
