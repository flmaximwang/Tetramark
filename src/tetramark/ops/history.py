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

The ref is resolved in the first read batch.  ``resolve_any`` (``tm find``) is
result-dependent: which object type, id and kind the ref names decides the
event and diff statements, and a single ``dolt sql`` process cannot read a
statement plan out of another statement's result.  The batch therefore carries
a small statement for every possible answer -- per object type and per way of
naming it (id, box name, alias) -- plus the events and the core/alias/extra
diffs; this module keeps only the statements that match the resolved object.
The **kind table's diff** needs the resolved kind, so it is the one follow-up
batch, and it reads only that table: a not-yet-applied kind cannot break the
history of an object of another kind.  Every kept statement is the SQL the
per-query path ran, so the answer (and its JSON/text/CSV renderings) is
unchanged.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from tetramark.core.objects import OBJECT_TYPES
from tetramark.errors import LedgerError, UsageError, try_find_hint
from tetramark.schema.ddl import table_name

if TYPE_CHECKING:  # pragma: no cover - import only for type checkers
    from tetramark.core.ledger import Ledger
    from tetramark.schema.model import EffectiveSchema

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
    "forced",
    "actor",
    "purpose",
    "note",
    "commit_hash",
    "message",
)

#: The three ways ``resolve_any`` can name an object.  A ref matches an id
#: before a box name and a box name before an alias; the batch carries the
#: statements for every way and :func:`_resolve` keeps the winning one.
_ID = "id"
_NAME = "name"
_ALIAS = "alias"

#: Naming ways per object type: only boxes can be named by their name.
_SOURCES: dict[str, tuple[str, ...]] = {
    "entity": (_ID, _ALIAS),
    "stock": (_ID, _ALIAS),
    "aliquot": (_ID, _ALIAS),
    "box": (_ID, _NAME, _ALIAS),
}


def _quote(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


def _params(text: str, sql: str) -> tuple[str, ...]:
    """One ref value per placeholder of ``sql`` (every placeholder is the ref)."""
    return (text,) * sql.count("%s")


def _alias_ids_sql(object_type: str) -> str:
    """The object ids one alias text names for ``object_type``."""
    return (
        f"(SELECT {_quote('object_id')} FROM {_quote('alias')} "
        f"WHERE {_quote('object_type')} = '{object_type}' "
        f"AND LOWER({_quote('alias')}) = LOWER(%s))"
    )


def _first_box_id_sql() -> str:
    """The id of the first box with the ref's name (``resolve_any`` takes it)."""
    return (
        f"(SELECT {_quote('id')} FROM {_quote('box')} "
        f"WHERE {_quote('name')} = %s LIMIT 1)"
    )


# ---------------------------------------------------------------------------
# The single read batch
# ---------------------------------------------------------------------------


class _Batch:
    """Statements of one history read batch, addressable by a stable key."""

    def __init__(self) -> None:
        self.statements: list[tuple[str, tuple[Any, ...]]] = []
        self._index: dict[str, int] = {}

    def add(self, key: str, sql: str, params: tuple[Any, ...] = ()) -> None:
        self._index[key] = len(self.statements)
        self.statements.append((sql, params))

    def rows(
        self, results: list[list[dict[str, Any]]], key: str
    ) -> list[dict[str, Any]]:
        return results[self._index[key]]


def _add_resolution(batch: _Batch, text: str) -> None:
    """The rows ``resolve_any`` reads: ids, the box name and the alias matches."""
    for object_type in OBJECT_TYPES:
        if object_type == "aliquot":
            # An aliquot's kind lives on the stock that owns it.
            sql = (
                f"SELECT 'aliquot' AS {_quote('object_type')}, "
                f"a.{_quote('id')} AS {_quote('object_id')}, "
                f"s.{_quote('kind')} AS {_quote('kind')} "
                f"FROM {_quote('aliquot')} a LEFT JOIN {_quote('stock')} s "
                f"ON s.{_quote('id')} = a.{_quote('stock_id')} "
                f"WHERE a.{_quote('id')} = %s"
            )
        else:
            sql = (
                f"SELECT '{object_type}' AS {_quote('object_type')}, "
                f"{_quote('id')} AS {_quote('object_id')}, "
                f"{_quote('kind')} AS {_quote('kind')} "
                f"FROM {_quote(object_type)} WHERE {_quote('id')} = %s"
            )
        batch.add(f"resolve:{object_type}", sql, (text,))
    batch.add(
        "resolve:name",
        f"SELECT {_quote('id')} AS {_quote('object_id')}, "
        f"{_quote('kind')} AS {_quote('kind')} "
        f"FROM {_quote('box')} WHERE {_quote('name')} = %s",
        (text,),
    )
    batch.add("resolve:alias", _alias_resolution_sql(), (text,))


def _alias_resolution_sql() -> str:
    """The alias rows of the ref with the kind and existence ``resolve_any`` needs.

    ``resolve_any`` ignores an alias row whose object does not exist, and an
    aliquot's kind comes from its stock; both are answered by the joins, so a
    later ``_core_row`` query is not needed.
    """
    return (
        f"SELECT al.{_quote('object_type')} AS {_quote('object_type')}, "
        f"al.{_quote('object_id')} AS {_quote('object_id')}, "
        f"COALESCE(e.{_quote('kind')}, s.{_quote('kind')}, b.{_quote('kind')}, "
        f"st.{_quote('kind')}) AS {_quote('kind')}, "
        f"CASE WHEN e.{_quote('id')} IS NOT NULL OR s.{_quote('id')} IS NOT NULL "
        f"OR b.{_quote('id')} IS NOT NULL OR a.{_quote('id')} IS NOT NULL "
        f"THEN 1 ELSE 0 END AS {_quote('present')} "
        f"FROM {_quote('alias')} al "
        f"LEFT JOIN {_quote('entity')} e ON al.{_quote('object_type')} = 'entity' "
        f"AND e.{_quote('id')} = al.{_quote('object_id')} "
        f"LEFT JOIN {_quote('stock')} s ON al.{_quote('object_type')} = 'stock' "
        f"AND s.{_quote('id')} = al.{_quote('object_id')} "
        f"LEFT JOIN {_quote('box')} b ON al.{_quote('object_type')} = 'box' "
        f"AND b.{_quote('id')} = al.{_quote('object_id')} "
        f"LEFT JOIN {_quote('aliquot')} a ON al.{_quote('object_type')} = 'aliquot' "
        f"AND a.{_quote('id')} = al.{_quote('object_id')} "
        f"LEFT JOIN {_quote('stock')} st ON st.{_quote('id')} = a.{_quote('stock_id')} "
        f"WHERE LOWER(al.{_quote('alias')}) = LOWER(%s) "
        f"ORDER BY al.{_quote('object_type')}"
    )


def _resolve(
    batch: _Batch, results: list[list[dict[str, Any]]], text: str
) -> tuple[str, str, str, str]:
    """``(object, id, kind, source)`` of the ref, resolved like ``resolve_any``.

    Ids win over a box name, which wins over an alias; an alias naming several
    existing objects is ambiguous.  The error text is the one ``resolve_any``
    raises.  ``source`` says which of the batch's statements answered.
    """
    for object_type in OBJECT_TYPES:
        rows = batch.rows(results, f"resolve:{object_type}")
        if rows:
            return (
                object_type,
                str(rows[0]["object_id"]),
                str(rows[0]["kind"]),
                _ID,
            )
    rows = batch.rows(results, "resolve:name")
    if rows:
        return "box", str(rows[0]["object_id"]), str(rows[0]["kind"]), _NAME
    resolved: dict[tuple[str, str], str] = {}
    for row in batch.rows(results, "resolve:alias"):
        if not int(row.get("present") or 0):
            continue
        resolved.setdefault(
            (str(row["object_type"]), str(row["object_id"])),
            str(row["kind"]),
        )
    if len(resolved) == 1:
        (object_type, object_id), kind = next(iter(resolved.items()))
        return object_type, object_id, kind, _ALIAS
    if len(resolved) > 1:
        kinds = ", ".join(
            f"{object_type} {object_id}" for object_type, object_id in resolved
        )
        raise UsageError(
            f"alias {text!r} is ambiguous: it names {kinds}; pass the object id"
        )
    raise LedgerError(
        f"unknown object {text!r} (no such id, box name or alias)"
        f"{try_find_hint(text)}"
    )


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


def _event_where(object_type: str, source: str) -> str:
    """The ``aliquot_event`` filter for one object and one naming way."""
    if object_type == "aliquot":
        if source == _ID:
            return f"{_quote('aliquot_id')} = %s"
        return f"{_quote('aliquot_id')} IN {_alias_ids_sql('aliquot')}"
    if object_type == "stock":
        if source == _ID:
            return (
                f"{_quote('aliquot_id')} IN ("
                f"SELECT {_quote('id')} FROM {_quote('aliquot')} "
                f"WHERE {_quote('stock_id')} = %s)"
            )
        return (
            f"{_quote('aliquot_id')} IN ("
            f"SELECT {_quote('id')} FROM {_quote('aliquot')} "
            f"WHERE {_quote('stock_id')} IN {_alias_ids_sql('stock')})"
        )
    if object_type == "entity":
        join = (
            f"SELECT a.{_quote('id')} FROM {_quote('aliquot')} a "
            f"JOIN {_quote('stock')} s ON s.{_quote('id')} = a.{_quote('stock_id')} "
            "WHERE "
        )
        if source == _ID:
            return f"{_quote('aliquot_id')} IN ({join}s.{_quote('entity_id')} = %s)"
        return (
            f"{_quote('aliquot_id')} IN "
            f"({join}s.{_quote('entity_id')} IN {_alias_ids_sql('entity')})"
        )
    # box: the tube's location moves are the events that touch the box.
    if source == _ID:
        return f"({_quote('from_box_id')} = %s OR {_quote('to_box_id')} = %s)"
    if source == _NAME:
        first = _first_box_id_sql()
        return f"({_quote('from_box_id')} = {first} OR {_quote('to_box_id')} = {first})"
    ids = _alias_ids_sql("box")
    return f"({_quote('from_box_id')} IN {ids} OR {_quote('to_box_id')} IN {ids})"


def _add_events(batch: _Batch, text: str) -> None:
    for object_type in ("entity", "stock", "aliquot", "box"):
        for source in _SOURCES[object_type]:
            where = _event_where(object_type, source)
            sql = (
                f"SELECT * FROM {_quote('aliquot_event')} WHERE {where} "
                f"ORDER BY {_quote('at')}, {_quote('id')}"
            )
            batch.add(f"events:{object_type}:{source}", sql, _params(text, sql))


# ---------------------------------------------------------------------------
# Dolt diffs
# ---------------------------------------------------------------------------


def _diff_where(object_type: str, source: str, left: str, right: str) -> str:
    """The diff filter for one object and one naming way, over two id columns."""
    if source == _ID:
        return f"(d.{_quote(left)} = %s OR d.{_quote(right)} = %s)"
    if source == _ALIAS:
        ids = _alias_ids_sql(object_type)
        return f"(d.{_quote(left)} IN {ids} OR d.{_quote(right)} IN {ids})"
    first = _first_box_id_sql()
    return f"(d.{_quote(left)} = {first} OR d.{_quote(right)} = {first})"


def _add_core_diffs(batch: _Batch, text: str) -> None:
    for object_type in ("entity", "stock", "aliquot", "box"):
        for source in _SOURCES[object_type]:
            where = _diff_where(object_type, source, "to_id", "from_id")
            sql = (
                f"SELECT * FROM {_quote('dolt_diff_' + object_type)} d "
                f"WHERE {where}"
            )
            batch.add(f"diff:{object_type}:{source}", sql, _params(text, sql))


def _kind_diff_branch(table: str, source: str, where: str) -> str:
    return (
        f"SELECT '{table}' AS {_quote('tm_table')}, '{source}' AS {_quote('tm_src')}, "
        f"d.{_quote('diff_type')} AS {_quote('diff_type')}, "
        f"d.{_quote('to_commit')} AS {_quote('to_commit')} "
        f"FROM {_quote('dolt_diff_' + table)} d WHERE {where}"
    )


def _kind_diff_statements(
    object_type: str, table: str, text: str
) -> list[tuple[str, tuple[str, ...]]]:
    """The diff reads for the one kind table the resolved object uses.

    ``history`` resolves the object first, then asks only this table: a schema
    kind whose table is not applied yet cannot break the history of an object
    of another kind, and the read does not scan every kind's diff table.
    """
    statements: list[tuple[str, tuple[str, ...]]] = []
    for source in _SOURCES[object_type]:
        where = _diff_where(object_type, source, "to_id", "from_id")
        statements.append(
            (_kind_diff_branch(table, source, where), _params(text, where))
        )
    return statements


def _add_alias_diffs(batch: _Batch, text: str) -> None:
    branches: list[str] = []
    params: list[str] = []
    for object_type in ("entity", "stock", "aliquot", "box"):
        for source in _SOURCES[object_type]:
            if source == _ID:
                left = (
                    f"(d.{_quote('to_object_type')} = '{object_type}' AND "
                    f"d.{_quote('to_object_id')} = %s)"
                )
                right = (
                    f"(d.{_quote('from_object_type')} = '{object_type}' AND "
                    f"d.{_quote('from_object_id')} = %s)"
                )
            elif source == _ALIAS:
                ids = _alias_ids_sql(object_type)
                left = (
                    f"(d.{_quote('to_object_type')} = '{object_type}' AND "
                    f"d.{_quote('to_object_id')} IN {ids})"
                )
                right = (
                    f"(d.{_quote('from_object_type')} = '{object_type}' AND "
                    f"d.{_quote('from_object_id')} IN {ids})"
                )
            else:
                first = _first_box_id_sql()
                left = (
                    f"(d.{_quote('to_object_type')} = 'box' AND "
                    f"d.{_quote('to_object_id')} = {first})"
                )
                right = (
                    f"(d.{_quote('from_object_type')} = 'box' AND "
                    f"d.{_quote('from_object_id')} = {first})"
                )
            where = f"({left} OR {right})"
            branches.append(
                f"SELECT '{object_type}' AS {_quote('tm_type')}, "
                f"'{source}' AS {_quote('tm_src')}, "
                f"d.{_quote('diff_type')} AS {_quote('diff_type')}, "
                f"d.{_quote('to_commit')} AS {_quote('to_commit')} "
                f"FROM {_quote('dolt_diff_alias')} d WHERE {where}"
            )
            params.extend(_params(text, where))
    batch.add("diff:alias", " UNION ALL ".join(branches), tuple(params))


def _add_extra_diffs(batch: _Batch, text: str) -> None:
    """The core-table diffs a box, stock or entity history needs in addition."""
    # A box's moves change the aliquot table (to/from_box_id), not the box row.
    for source in _SOURCES["box"]:
        where = _diff_where("box", source, "to_box_id", "from_box_id")
        sql = (
            f"SELECT 'box' AS {_quote('tm_case')}, '{source}' AS {_quote('tm_src')}, "
            f"d.{_quote('diff_type')} AS {_quote('diff_type')}, "
            f"d.{_quote('to_commit')} AS {_quote('to_commit')}, "
            f"d.{_quote('from_box_id')} AS {_quote('from_box_id')}, "
            f"d.{_quote('to_box_id')} AS {_quote('to_box_id')} "
            f"FROM {_quote('dolt_diff_aliquot')} d WHERE {where}"
        )
        batch.add(f"diff:extra:box:{source}", sql, _params(text, sql))
    # Aliquot rows carry the stock id, so their diffs are stock history too.
    for source in _SOURCES["stock"]:
        where = _diff_where("stock", source, "to_stock_id", "from_stock_id")
        sql = (
            f"SELECT 'stock' AS {_quote('tm_case')}, '{source}' AS {_quote('tm_src')}, "
            f"d.{_quote('diff_type')} AS {_quote('diff_type')}, "
            f"d.{_quote('to_commit')} AS {_quote('to_commit')} "
            f"FROM {_quote('dolt_diff_aliquot')} d WHERE {where}"
        )
        batch.add(f"diff:extra:stock:{source}", sql, _params(text, sql))
    # Stock rows carry the entity id, so a new stock is entity history too.
    for source in _SOURCES["entity"]:
        where = _diff_where("entity", source, "to_entity_id", "from_entity_id")
        sql = (
            f"SELECT 'entity' AS {_quote('tm_case')}, '{source}' AS {_quote('tm_src')}, "
            f"d.{_quote('diff_type')} AS {_quote('diff_type')}, "
            f"d.{_quote('to_commit')} AS {_quote('to_commit')} "
            f"FROM {_quote('dolt_diff_stock')} d WHERE {where}"
        )
        batch.add(f"diff:extra:entity:{source}", sql, _params(text, sql))


def _add_static_reads(batch: _Batch) -> None:
    """The ref-independent reads: box names, the commit log and every alias."""
    batch.add("box_names", f"SELECT {_quote('id')}, {_quote('name')} FROM {_quote('box')}")
    batch.add("dolt_log", f"SELECT * FROM {_quote('dolt_log')}")
    batch.add(
        "aliases",
        f"SELECT {_quote('object_type')}, {_quote('object_id')}, {_quote('alias')} "
        f"FROM {_quote('alias')} "
        f"ORDER BY {_quote('object_type')}, {_quote('alias')}",
    )


# ---------------------------------------------------------------------------
# The answer
# ---------------------------------------------------------------------------


def history(ledger: "Ledger", ref: str) -> dict[str, Any]:
    """Events and commits for one object, resolved by id, box name or alias."""
    text = str(ref).strip()
    if not text:
        raise UsageError("an object id or alias must not be empty")

    batch = _Batch()
    _add_resolution(batch, text)
    _add_events(batch, text)
    _add_core_diffs(batch, text)
    _add_alias_diffs(batch, text)
    _add_extra_diffs(batch, text)
    _add_static_reads(batch)

    results = ledger.driver.query_many(batch.statements)
    object_type, object_id, kind_name, source = _resolve(batch, results, text)

    # The kind table is the only part of the batch whose SQL needs the resolved
    # kind.  Read it after resolution, and only that one table.
    kind_rows: list[dict[str, Any]] = []
    if object_type != "aliquot" and kind_name in ledger.schema.kinds:
        kind_table = table_name(object_type, kind_name)
        for rows in ledger.driver.query_many(
            _kind_diff_statements(object_type, kind_table, text)
        ):
            kind_rows.extend(rows)

    events = [dict(row) for row in batch.rows(results, f"events:{object_type}:{source}")]
    box_names = {
        str(row["id"]): str(row["name"]) for row in batch.rows(results, "box_names")
    }
    for event in events:
        for side in ("from", "to"):
            box_id = event.get(f"{side}_box_id")
            event[f"{side}_box"] = (
                None if box_id is None else box_names.get(str(box_id), str(box_id))
            )
    commits = _commits(
        batch, results, object_type, kind_name, object_id, source, kind_rows
    )
    aliases = [
        str(row["alias"])
        for row in batch.rows(results, "aliases")
        if str(row["object_type"]) == object_type
        and str(row["object_id"]) == object_id
    ]
    return {
        "object": object_type,
        "kind": kind_name,
        "id": object_id,
        "aliases": aliases,
        "events": events,
        "commits": commits,
    }


def _alias_diff_rows(
    rows: list[dict[str, Any]], object_type: str, source: str
) -> list[dict[str, Any]]:
    return [
        row
        for row in rows
        if str(row.get("tm_type")) == object_type
        and str(row.get("tm_src")) == source
    ]


def _commits(
    batch: _Batch,
    results: list[list[dict[str, Any]]],
    object_type: str,
    kind_name: str,
    object_id: str,
    source: str,
    kind_rows: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    # Each entry is (table, rows, is_box_move): ``is_box_move`` asks for the
    # aliquots' entering/leaving wording instead of the raw Dolt diff type.
    sources: list[tuple[str, list[dict[str, Any]], bool]] = [
        (
            object_type,
            batch.rows(results, f"diff:{object_type}:{source}"),
            False,
        )
    ]
    if object_type != "aliquot":
        table = table_name(object_type, kind_name)
        sources.append((table, list(kind_rows), False))
    sources.append(
        (
            "alias",
            _alias_diff_rows(batch.rows(results, "diff:alias"), object_type, source),
            False,
        )
    )
    if object_type == "box":
        # Moving an aliquot in or out changes the aliquot table, not the box
        # row; those diffs are the commits a box's history needs to show.
        sources.append(
            ("aliquot", batch.rows(results, f"diff:extra:box:{source}"), True)
        )
    elif object_type == "stock":
        # Aliquot rows carry the stock id, so their create/move/use commits
        # are part of the stock's history.
        sources.append(
            ("aliquot", batch.rows(results, f"diff:extra:stock:{source}"), False)
        )
    elif object_type == "entity":
        # Stock rows carry the entity id; a new stock is entity history too.
        sources.append(
            ("stock", batch.rows(results, f"diff:extra:entity:{source}"), False)
        )

    log = {
        str(row["commit_hash"]): row for row in batch.rows(results, "dolt_log")
    }
    commits: dict[str, dict[str, Any]] = {}
    for table, rows, is_box_move in sources:
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
    if event.get("forced") in (1, True):
        parts.append("(forced)")
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
                "forced": event.get("forced"),
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
                "forced": "",
                "actor": commit.get("author"),
                "purpose": "",
                "note": "",
                "commit_hash": commit.get("commit_hash"),
                "message": commit.get("message"),
            }
        )
    return rows
