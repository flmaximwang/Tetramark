"""``aliquot_event`` rows: the audit trail of every physical unit (spec §3, §6).

An aliquot is a physical tube, so anything that happens to it is appended to
``aliquot_event`` rather than overwriting history: it is created, moved, used
(quantity decremented), its status changes, or its kind-specific fields are
edited.  The table is append-only except for :func:`purge_events`, which the
explicit ``aliquot purge`` command needs because of the foreign key.

Spec 9 added the ``revert_use`` kind: a ``use`` can be undone, and the undo is
its own row that points at the exact original with ``reverts_event_id``.  The
original row is never touched; it stays the record of what happened, and the
reversal is the record of what was restored.

Functions here take a *session* (anything with ``execute``) so the event row
lands inside the same transaction — and therefore the same Dolt commit — as the
change it describes.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from tetramark.errors import LedgerError
from tetramark.schema.ddl import REVERT_EVENT
from tetramark.schema.model import SPEC_VERSION

__all__ = [
    "EVENT_TYPES",
    "Event",
    "purge_events",
    "record_event",
    "require_spec7_event_columns",
    "require_spec9_reversal_columns",
]

#: Values allowed by the ``event`` ENUM in the core DDL.  ``revert_use`` (spec
#: 9) is last, like in the ENUM: older ledgers keep their stored values.
EVENT_TYPES: tuple[str, ...] = ("create", "use", "move", "status", "edit", REVERT_EVENT)


class Executor(Protocol):
    """The part of a write session events need.

    ``ledger`` is :attr:`tetramark.core.ledger.WriteSession.ledger`; the
    spec-7/spec-9 guards read its ``spec_version`` before the INSERT.
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
    #: Spec 9: the ``id`` of the ``use`` this ``revert_use`` undoes.  Exactly
    #: the reversal kinds carry one (the DDL's CHECK enforces both directions).
    reverts_event_id: int | None = None
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
    "reverts_event_id",
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

#: The spec that introduced ``aliquot_event.message`` (and dropped ``note``/
#: ``purpose``).  A ledger below this still has the spec-6 event columns; the
#: counter moves on (spec 8 dropped ``aliquot.notes``), so this guard compares
#: against the spec that changed the event shape, never ``SPEC_VERSION``.
EVENT_MESSAGE_SPEC = 7

#: The spec that introduced ``aliquot_event.reverts_event_id`` and the
#: ``revert_use`` kind.  A spec-8 ledger can still record every ordinary event
#: (the column is simply absent from the INSERT), but it cannot record a
#: reversal: the reference has nowhere to go.
EVENT_REVERSAL_SPEC = 9


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
    if stored >= EVENT_MESSAGE_SPEC:
        return
    raise LedgerError(
        f"ledger is spec {stored}, tm is spec {SPEC_VERSION}: aliquot_event "
        "still has the spec-6 columns (note, purpose). Upgrade it with:\n"
        f"uv run migration/spec/6-to-7/migrate.py --ledger {ledger.path} --apply\n"
        "(dry run without --apply; see migration/spec/6-to-7/README.md)"
    )


def require_spec9_reversal_columns(ledger: Any) -> None:
    """Refuse a reversal on a ledger that has no ``reverts_event_id`` column.

    Spec 9 added the column, the ``revert_use`` kind and the self foreign key,
    so a spec-8 ledger cannot store a reversal at all.  The guard is even more
    targeted than the spec-7 one: an ordinary ``use``/``move``/``status`` event
    still writes fine on a spec-8 ledger (that is the point of the nullable
    column), and only :func:`record_event`'s reversal path calls this.  The
    CLI reaches it through ``tm aliquot use --revert-last``, which is planned
    **before** the interactive ``--yes`` question, so a spec-8 ledger is
    refused with the upgrade text instead of a ``--yes`` hint.

    Like :func:`require_spec7_event_columns`, a missing or unparseable counter
    is not guarded: a very old ledger keeps its current behaviour, and the
    INSERT itself reports the missing column.
    """
    from tetramark.core.ledger import META_SPEC_VERSION

    raw = ledger.meta.get(META_SPEC_VERSION)
    try:
        stored = int(str(raw).strip())
    except (TypeError, ValueError):
        return
    if stored >= EVENT_REVERSAL_SPEC:
        return
    raise LedgerError(
        f"ledger is spec {stored}, tm is spec {SPEC_VERSION}: aliquot_event "
        "has no reversals (reverts_event_id, revert_use). Upgrade it with:\n"
        f"uv run migration/spec/8-to-9/migrate.py --ledger {ledger.path}\n"
        "(add --dry-run to preview; see migration/spec/8-to-9/README.md)"
    )


def _reversal_target(session: Executor, record: Event) -> None:
    """Validate the ``use`` a ``revert_use`` names, inside the transaction.

    The DDL already refuses a reference to a nonexistent row (self foreign key)
    and lets a ``use`` be reversed once (UNIQUE).  What no constraint can say is
    that the *named* row must be a ``use`` **of the same aliquot**, so that is
    checked here, before the INSERT: the same transaction, the same commit, and
    an error the operator can act on instead of a raw database failure.
    """
    assert record.reverts_event_id is not None
    rows = session.query(
        "SELECT `event`, `aliquot_id` FROM `aliquot_event` WHERE `id` = %s",
        (record.reverts_event_id,),
    )
    if not rows:
        raise LedgerError(
            f"aliquot event {record.reverts_event_id} does not exist: a "
            "reversal must name an existing use event"
        )
    target = rows[0]
    if str(target["event"]) != "use":
        raise LedgerError(
            f"aliquot event {record.reverts_event_id} is a "
            f"{target['event']!r} event, not a 'use': only a use can be "
            "reversed"
        )
    if str(target["aliquot_id"]) != record.aliquot_id:
        raise LedgerError(
            f"aliquot event {record.reverts_event_id} belongs to "
            f"{target['aliquot_id']}, not {record.aliquot_id}: a reversal must "
            "undo a use of its own aliquot"
        )


def record_event(session: Executor, record: Event | None = None, **fields: Any) -> Event:
    """Append one event row to ``aliquot_event``.

    ``record_event(session, aliquot_id="x.1", event="move", ...)`` is the
    convenient form; passing an :class:`Event` is equivalent.  Returns the
    event that was written.  A spec-6 ledger is refused before the INSERT (see
    :func:`require_spec7_event_columns`), and a reversal on a spec-8 ledger is
    refused by :func:`require_spec9_reversal_columns`: in both cases the write
    session aborts and nothing is committed.

    An ordinary event is inserted **without** the ``reverts_event_id`` column,
    so a spec-7/8 ledger — where the column does not exist — keeps recording
    ``create``/``use``/``move``/``status``/``edit`` exactly as before.  Only a
    reversal names the column, and that path requires spec 9.
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
    if record.event == REVERT_EVENT:
        if record.reverts_event_id is None:
            raise LedgerError(
                f"a {REVERT_EVENT!r} event must name the use event it reverses "
                "(reverts_event_id); only a use can be reversed"
            )
        require_spec9_reversal_columns(session.ledger)
        _reversal_target(session, record)
    elif record.reverts_event_id is not None:
        raise LedgerError(
            f"only a {REVERT_EVENT!r} event carries reverts_event_id; "
            f"{record.event!r} must leave it empty"
        )

    columns = _EVENT_COLUMNS
    if record.reverts_event_id is None:
        # Ordinary event: leave the spec-9 column out of the INSERT so a
        # spec-7/8 ledger (no such column) keeps accepting these writes.
        columns = tuple(
            column for column in _EVENT_COLUMNS if column != "reverts_event_id"
        )
    values = tuple(getattr(record, column) for column in columns)
    columns_sql = ", ".join(f"`{column}`" for column in columns)
    placeholders = ", ".join("%s" for _ in columns)
    session.execute(
        f"INSERT INTO `aliquot_event` ({columns_sql}) VALUES ({placeholders})",
        values,
    )
    return record


def purge_events(session: Executor, aliquot_id: str) -> int:
    """Delete every event of an aliquot (only ``aliquot purge`` does this).

    The spec-9 self foreign key is ``ON DELETE CASCADE``, so a reversal row
    disappears with the ``use`` it points at when the whole aliquot's history
    goes; the ``DELETE`` never has to delete children first.
    """
    rows = session.query(
        "SELECT COUNT(*) AS `n` FROM `aliquot_event` WHERE `aliquot_id` = %s",
        (aliquot_id,),
    )
    count = int(rows[0]["n"]) if rows else 0
    session.execute(
        "DELETE FROM `aliquot_event` WHERE `aliquot_id` = %s", (aliquot_id,)
    )
    return count
