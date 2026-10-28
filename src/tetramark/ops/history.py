"""``tm history``: events and commits touching one object (spec §6).

Two independent trails are merged into one answer:

* ``aliquot_event`` rows — the app-level audit trail (create, move, use,
  status, edit).  They are recorded inside the same transaction as the change,
  so they are the authoritative order of physical actions;
* ``dolt_diff_<table>`` rows — the Dolt-level trail.  They catch edits that
  have no event (renaming an entity, changing a note) and show which commit
  carries them.

Their order is different by nature: events have a timestamp (and an
auto-increment id as tie-break), commits have ``dolt_log.commit_order``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from tetramark.ops.find import resolve_any
from tetramark.schema.ddl import table_name

if TYPE_CHECKING:  # pragma: no cover - import only for type checkers
    from tetramark.core.ledger import Ledger

__all__ = ["HISTORY_COLUMNS", "history", "history_rows", "render_history_text"]

#: Stable CSV header for :func:`history_rows`.
HISTORY_COLUMNS: tuple[str, ...] = (
    "section",
    "at",
    "event",
    "aliquot_id",
    "amount",
    "unit",
    "from_box",
    "from_slot",
    "to_box",
    "to_slot",
    "old_status",
    "new_status",
    "actor",
    "purpose",
    "note",
    "commit_hash",
    "message",
)


def history(ledger: Ledger, ref: str) -> dict[str, Any]:
    """Events and commits for one object, resolved by id, box name or alias."""
    resolved = resolve_any(ledger, ref)
    object_type = str(resolved["object"])
    object_id = str(resolved["id"])
    kind_name = str(resolved["kind"])

    events = _events(ledger, object_type, object_id)
    box_names = {
        str(row["id"]): str(row["name"])
        for row in ledger.driver.query("SELECT `id`, `name` FROM `box`")
    }
    for event in events:
        for side in ("from", "to"):
            box_id = event.get(f"{side}_box_id")
            event[f"{side}_box"] = (
                None if box_id is None else box_names.get(str(box_id), str(box_id))
            )
    commits = _commits(ledger, object_type, kind_name, object_id)
    aliases = [
        str(row["alias"])
        for row in ledger.driver.query(
            f"SELECT `alias` FROM `alias` "
            f"WHERE `object_type` = %s AND `object_id` = %s ORDER BY `alias`",
            (object_type, object_id),
        )
    ]
    return {
        "object": object_type,
        "kind": kind_name,
        "id": object_id,
        "aliases": aliases,
        "events": events,
        "commits": commits,
    }


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


def _events(ledger: Ledger, object_type: str, object_id: str) -> list[dict[str, Any]]:
    if object_type == "aliquot":
        where = "`aliquot_id` = %s"
        params: tuple[Any, ...] = (object_id,)
    elif object_type == "stock":
        where = "`aliquot_id` IN (SELECT `id` FROM `aliquot` WHERE `stock_id` = %s)"
        params = (object_id,)
    elif object_type == "entity":
        where = (
            "`aliquot_id` IN ("
            "SELECT a.`id` FROM `aliquot` a "
            "JOIN `stock` s ON s.`id` = a.`stock_id` "
            "WHERE s.`entity_id` = %s)"
        )
        params = (object_id,)
    else:  # box
        where = "(`from_box_id` = %s OR `to_box_id` = %s)"
        params = (object_id, object_id)
    return ledger.driver.query(
        f"SELECT * FROM `aliquot_event` WHERE {where} ORDER BY `at`, `id`",
        params,
    )


# ---------------------------------------------------------------------------
# Dolt diffs
# ---------------------------------------------------------------------------


def _commits(
    ledger: Ledger, object_type: str, kind_name: str, object_id: str
) -> list[dict[str, Any]]:
    # Each entry is (table, WHERE clause, params, is_box_move): ``is_box_move``
    # asks for the aliquots' entering/leaving wording instead of the raw
    # Dolt diff type.
    queries: list[tuple[str, str, tuple[Any, ...], bool]] = [
        (
            object_type,
            "`to_id` = %s OR `from_id` = %s",
            (object_id, object_id),
            False,
        )
    ]
    if object_type != "aliquot":
        queries.append(
            (
                table_name(object_type, kind_name),
                "`to_id` = %s OR `from_id` = %s",
                (object_id, object_id),
                False,
            )
        )
    queries.append(
        (
            "alias",
            "(`to_object_type` = %s AND `to_object_id` = %s) "
            "OR (`from_object_type` = %s AND `from_object_id` = %s)",
            (object_type, object_id, object_type, object_id),
            False,
        )
    )
    if object_type == "box":
        # Moving an aliquot in or out changes the aliquot table, not the box
        # row; those diffs are the commits a box's history needs to show.
        queries.append(
            (
                "aliquot",
                "`to_box_id` = %s OR `from_box_id` = %s",
                (object_id, object_id),
                True,
            )
        )
    elif object_type == "stock":
        # Aliquot rows carry the stock id, so their create/move/use commits
        # are part of the stock's history.
        queries.append(
            (
                "aliquot",
                "`to_stock_id` = %s OR `from_stock_id` = %s",
                (object_id, object_id),
                False,
            )
        )
    elif object_type == "entity":
        # Stock rows carry the entity id; a new stock is entity history too.
        queries.append(
            (
                "stock",
                "`to_entity_id` = %s OR `from_entity_id` = %s",
                (object_id, object_id),
                False,
            )
        )

    log = {
        str(row["commit_hash"]): row
        for row in ledger.driver.query("SELECT * FROM `dolt_log`")
    }
    commits: dict[str, dict[str, Any]] = {}
    for table, where, params, is_box_move in queries:
        rows = ledger.driver.query(
            f"SELECT * FROM `dolt_diff_{table}` WHERE {where}",
            params,
        )
        for row in rows:
            diff_type = str(row.get("diff_type") or "")
            if is_box_move:
                diff_type = _aliquot_diff_type(row, object_id)
            commit_hash = row.get("to_commit")
            if commit_hash is None:
                continue
            hash_text = str(commit_hash)
            if hash_text.upper() == "WORKING":
                entry = commits.setdefault(
                    hash_text,
                    {
                        "commit_hash": hash_text,
                        "date": None,
                        "message": "(uncommitted changes)",
                        "author": None,
                        "order": 2**31,
                        "changes": [],
                    },
                )
            else:
                meta = log.get(hash_text, {})
                entry = commits.setdefault(
                    hash_text,
                    {
                        "commit_hash": hash_text,
                        "date": meta.get("date"),
                        "message": meta.get("message"),
                        "author": meta.get("author") or meta.get("committer"),
                        "order": int(meta.get("commit_order") or 0),
                        "changes": [],
                    },
                )
            change = {"table": table, "diff_type": diff_type}
            if change not in entry["changes"]:
                entry["changes"].append(change)

    for entry in commits.values():
        entry["changes"].sort(key=lambda change: change["table"])
    result = sorted(
        commits.values(),
        key=lambda entry: (int(entry["order"]), str(entry["commit_hash"])),
    )
    return result


def _aliquot_diff_type(row: dict[str, Any], box_id: str) -> str:
    """Describe a box-relevant aliquot diff as entering/leaving the box."""
    entering = row.get("to_box_id") == box_id
    leaving = row.get("from_box_id") == box_id
    if entering and leaving:
        return "modified"
    if entering:
        return "added"
    if leaving:
        return "removed"
    return str(row.get("diff_type") or "")


# ---------------------------------------------------------------------------
# Text / CSV rendering
# ---------------------------------------------------------------------------


def render_history_text(result: dict[str, Any]) -> str:
    """A plain, sectioned history for ``--format table``."""
    lines = [
        f"Object: {result['object']} {result['id']} ({result['kind']})",
    ]
    if result.get("aliases"):
        lines.append("Aliases: " + ", ".join(result["aliases"]))

    events = result.get("events") or []
    lines.append(f"Events ({len(events)}):")
    if not events:
        lines.append("  (none)")
    for event in events:
        lines.append("  " + _event_line(event))

    commits = result.get("commits") or []
    lines.append(f"Commits ({len(commits)}):")
    if not commits:
        lines.append("  (none)")
    for commit in commits:
        lines.append("  " + _commit_line(commit))
    return "\n".join(lines)


def _event_line(event: dict[str, Any]) -> str:
    parts = [
        str(event.get("at") or ""),
        str(event.get("event") or ""),
        str(event.get("aliquot_id") or ""),
    ]
    amount = event.get("amount")
    if amount is not None:
        unit = event.get("unit")
        parts.append(f"amount={amount}{'' if unit is None else ' ' + str(unit)}")
    source = _place(
        event.get("from_box") or event.get("from_box_id"), event.get("from_slot")
    )
    target = _place(
        event.get("to_box") or event.get("to_box_id"), event.get("to_slot")
    )
    if source:
        parts.append(f"from={source}")
    if target:
        parts.append(f"to={target}")
    old_status = event.get("old_status")
    new_status = event.get("new_status")
    if old_status and new_status and old_status != new_status:
        parts.append(f"status={old_status}->{new_status}")
    elif new_status:
        parts.append(f"status={new_status}")
    elif old_status:
        parts.append(f"status={old_status}")
    if event.get("actor"):
        parts.append(f"by={event['actor']}")
    return " ".join(part for part in parts if part)


def _place(box_id: Any, slot: Any) -> str:
    if box_id is None and slot is None:
        return ""
    text = "" if box_id is None else str(box_id)
    if slot is not None:
        text = f"{text}:{slot}" if text else str(slot)
    return text


def _commit_line(commit: dict[str, Any]) -> str:
    changes = ", ".join(
        f"{change['table']}:{change['diff_type']}" for change in commit["changes"]
    )
    prefix = str(commit.get("commit_hash") or "")[:10]
    return f"{prefix} {commit.get('date') or ''} {commit.get('message') or ''} [{changes}]"


def history_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten a history into one CSV row per event and per commit."""
    rows: list[dict[str, Any]] = []
    for event in result.get("events") or []:
        rows.append(
            {
                "section": "event",
                "at": event.get("at"),
                "event": event.get("event"),
                "aliquot_id": event.get("aliquot_id"),
                "amount": event.get("amount"),
                "unit": event.get("unit"),
                "from_box": event.get("from_box"),
                "from_slot": event.get("from_slot"),
                "to_box": event.get("to_box"),
                "to_slot": event.get("to_slot"),
                "old_status": event.get("old_status"),
                "new_status": event.get("new_status"),
                "actor": event.get("actor"),
                "purpose": event.get("purpose"),
                "note": event.get("note"),
                "commit_hash": "",
                "message": "",
            }
        )
    for commit in result.get("commits") or []:
        rows.append(
            {
                "section": "commit",
                "at": commit.get("date"),
                "event": ",".join(
                    change["diff_type"] for change in commit.get("changes") or []
                ),
                "aliquot_id": "",
                "amount": "",
                "unit": "",
                "from_box": "",
                "from_slot": "",
                "to_box": "",
                "to_slot": "",
                "old_status": "",
                "new_status": "",
                "actor": commit.get("author"),
                "purpose": "",
                "note": "",
                "commit_hash": commit.get("commit_hash"),
                "message": commit.get("message"),
            }
        )
    return rows
