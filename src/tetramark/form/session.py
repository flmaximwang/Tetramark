"""Run one ``add-form``: questions, review, confirm, then the ordinary write.

The session is the only place that touches the database for writing: after the
review screen is confirmed with ``y`` it takes the per-ledger write lock (the
spec: "The write lock is taken only when you confirm with y") and calls the
**same** ``ledger.<objects>.add(...)`` as ``tm <obj> add`` -- there is no
second validation or write path.  A rejection from the core is shown on the
form and the user is taken back to the field that failed.

``--dry-run``/``--no-commit`` come from the already-open ledger, so the form
behaves exactly like ``add`` for those.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from tetramark.errors import LedgerError
from tetramark.form.resolver import LedgerResolver
from tetramark.form.source import InputSource
from tetramark.form.spec import build_plan
from tetramark.form.state import FormState

if TYPE_CHECKING:  # pragma: no cover - import only for type checkers
    from tetramark.core.ledger import Ledger

__all__ = ["run_add_form", "write_payload"]


def write_payload(
    ledger: Ledger, object_type: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """Call the core ``add`` operation with the form's collected values."""
    if object_type == "entity":
        return ledger.entities.add(
            payload["kind"],
            values=payload["values"],
            name=payload.get("name"),
            aliases=payload.get("aliases") or None,
        )
    if object_type == "stock":
        return ledger.stocks.add(
            payload["kind"],
            entity=payload["entity"],
            values=payload["values"],
            aliquots=payload["aliquots"],
            box=payload.get("box"),
            slots=payload.get("slots") or (),
        )
    if object_type == "aliquot":
        return ledger.aliquots.add(
            payload["stock"],
            count=payload["count"],
            box=payload.get("box"),
            slots=payload.get("slots") or (),
        )
    if object_type == "box":
        return ledger.boxes.add(
            payload["kind"],
            payload["name"],
            parent=payload.get("parent"),
            slot=payload.get("slot"),
            notes=payload.get("notes"),
        )
    raise ValueError(f"unknown form object {object_type!r}")


def _default_source() -> InputSource:
    from tetramark.form.tui import PromptToolkitSource

    return PromptToolkitSource()


def run_add_form(
    ledger: Ledger,
    object_type: str,
    source: InputSource | None = None,
    *,
    lock_command: str | None = None,
) -> dict[str, Any] | None:
    """Run the form; return the created object's result, or ``None`` on cancel."""
    source = source if source is not None else _default_source()
    plan = build_plan(ledger.schema, object_type)
    state = FormState(plan, LedgerResolver(ledger))
    command = lock_command or f"tm {object_type} add-form"

    while True:
        if state.mode == "write":
            payload = state.payload()
            try:
                if ledger.dry_run:
                    result = write_payload(ledger, object_type, payload)
                else:
                    with ledger.hold_write_lock(command=command):
                        result = write_payload(ledger, object_type, payload)
            except LedgerError as exc:
                state.reject(str(exc))
                continue
            state.mode = "done"
            return result
        if state.mode in ("cancelled", "done"):
            return None
        reply = source.ask(state.prompt())
        state.apply(reply)
