"""``aliquot_event`` rows: the audit trail of every physical unit (spec §3, §6).

An aliquot is a physical tube, so anything that happens to it is appended to
``aliquot_event`` rather than overwriting history: it is created, moved, used
(quantity decremented), its status changes, or its kind-specific fields are
edited.  The table is append-only except for :func:`purge_events`, which the
explicit ``aliquot purge`` command needs because of the foreign key.

Functions here take a *session* (anything with ``execute``) so the event row
lands inside the same transaction — and therefore the same Dolt commit — as the
change it describes.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from tetramark.errors import LedgerError
from tetramark.schema.model import SPEC_VERSION

__all__ = [
    "EVENT_TYPES",
    "Event",
    "purge_events",
    "record_event",
    "require_spec7_event_columns",
]

#: Values allowed by the ``event`` ENUM in the core DDL.
EVENT_TYPES: tuple[str, ...] = ("create", "use", "move", "status", "edit")


class Executor(Protocol):
    """The part of a write session events need.

    ``ledger`` is :attr:`tetramark.core.ledger.WriteSession.ledger`; the
    spec-7 guard reads its ``spec_version`` before the INSERT.
    """

    #: The ledger this session writes to (a ``WriteSession`` attribute).
    ledger: Any

    def execute(self, sql: str, params: Any = None) -> str: ...  # pragma: no cover

    def query(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        ...  # pragma: no cover


@dataclass(frozen=True)
class Event:
    """One ``aliquot_event`` row, before it is written."""

    aliquot_id: str
    event: str
    amount: Decimal | None = None
    unit: str | None = None
    from_box_id: str | None = None
    from_slot: str | None = None
    to_box_id: str | None = None
    to_slot: str | None = None
    old_status: str | None = None
    new_status: str | None = None
    #: True when ``--force`` overrode a status transition rule (spec 4); the
    #: generated ``core_status_event_allowed`` test skips forced events.
    forced: bool = False
    actor: str | None = None
    #: Why the event happened: the ``-m/--message`` text of ``use``/``move``
    #: or a ``status`` change, ``spec 4-to-5`` for the positions that step
    #: cleared, and so on.
    message: str | None = None


_EVENT_COLUMNS = (
    "aliquot_id",
    "event",
    "amount",
    "unit",
    "from_box_id",
    "from_slot",
    "to_box_id",
    "to_slot",
    "old_status",
    "new_status",
    "forced",
    "actor",
    "message",
)


def require_spec7_event_columns(ledger: Any) -> None:
    """Refuse a spec-6 ``aliquot_event`` shape on the paths that read/write events.

    A spec-6 ledger still has the old ``note``/``purpose`` columns, so an event
    INSERT or a read that names ``message`` would fail with a raw database
    error.  The guard is deliberately **targeted**: only ``record_event``,
    ``show_aliquot`` and the event half of ``tm history`` call it, while every
    other command (``tm status``, ``tm schema plan``, the list commands) keeps
    the warn-only spec notice from
    :meth:`tetramark.core.ledger.Ledger.check_spec_version`.

    A missing or unparseable counter is not guarded, exactly like
    ``check_spec_version``: ``tm adopt`` and very old ledgers keep working.
    """
    # Imported here: ``core.ledger`` imports ``core.objects``, which imports
    # this module, so a top-level import would be a cycle.
    from tetramark.core.ledger import META_SPEC_VERSION

    raw = ledger.meta.get(META_SPEC_VERSION)
    try:
        stored = int(str(raw).strip())
    except (TypeError, ValueError):
        return
    if stored >= SPEC_VERSION:
        return
    raise LedgerError(
        f"ledger is spec {stored}, tm is spec {SPEC_VERSION}: aliquot_event "
        "still has the spec-6 columns (note, purpose). Upgrade it with:\n"
        f"uv run migration/spec/6-to-7/migrate.py --ledger {ledger.path} --apply\n"
        "(dry run without --apply; see migration/spec/6-to-7/README.md)"
    )


def record_event(session: Executor, record: Event | None = None, **fields: Any) -> Event:
    """Append one event row to ``aliquot_event``.

    ``record_event(session, aliquot_id="x.1", event="move", ...)`` is the
    convenient form; passing an :class:`Event` is equivalent.  Returns the
    event that was written.  A spec-6 ledger is refused before the INSERT (see
    :func:`require_spec7_event_columns`), so the write session aborts and
    nothing is committed.
    """
    require_spec7_event_columns(session.ledger)
    if record is None:
        record = Event(**fields)
    elif fields:
        raise TypeError("pass an Event or keyword fields, not both")
    if record.event not in EVENT_TYPES:
        raise LedgerError(
            f"invalid aliquot event {record.event!r}; choose from {', '.join(EVENT_TYPES)}"
        )
    if not record.aliquot_id:
        raise LedgerError("an aliquot event needs an aliquot id")

    values = tuple(getattr(record, column) for column in _EVENT_COLUMNS)
    columns_sql = ", ".join(f"`{column}`" for column in _EVENT_COLUMNS)
    placeholders = ", ".join("%s" for _ in _EVENT_COLUMNS)
    session.execute(
        f"INSERT INTO `aliquot_event` ({columns_sql}) VALUES ({placeholders})",
        values,
    )
    return record


def purge_events(session: Executor, aliquot_id: str) -> int:
    """Delete every event of an aliquot (only ``aliquot purge`` does this)."""
    rows = session.query(
        "SELECT COUNT(*) AS `n` FROM `aliquot_event` WHERE `aliquot_id` = %s",
        (aliquot_id,),
    )
    count = int(rows[0]["n"]) if rows else 0
    session.execute(
        "DELETE FROM `aliquot_event` WHERE `aliquot_id` = %s", (aliquot_id,)
    )
    return count
