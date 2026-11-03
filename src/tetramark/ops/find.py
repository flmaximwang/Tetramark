"""``tm find``: resolve a handle to objects (spec §6).

Search precedence follows the zsqlab ``find_entity.sh`` reference:

1. an exact core id (entity, stock, aliquot or box);
2. an exact alias (per object type, so one alias text may hit several);
3. a name or a ``string``/``text`` field of a kind table, case-insensitively.

Every hit carries the object type, kind, id, a human summary, its location
(``Freezer/Shelf/Box:A1`` for aliquots and everything above them) and the way
it matched.  The result list is ranked: exact ids before aliases before name
matches before field matches, exact before prefix before substring.  Ties are
broken by object type and id so output is stable.

The search is deliberately literal: ``%`` and ``_`` are ordinary characters
(LOCATE, not LIKE), matching the reference script.  Whitespace in the query is
*not* split into tokens; the whole trimmed string is one handle.  A caller that
needs token splitting (SnapGene file names) can call ``find`` once per token.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from tetramark.core.objects import (
    OBJECT_TYPES,
    _box_paths,
    box_kind_columns,
    box_kind_from,
)
from tetramark.errors import LedgerError, UsageError
from tetramark.schema.ddl import CORE_COLUMNS
from tetramark.schema.model import FieldType, Kind

if TYPE_CHECKING:  # pragma: no cover - import only for type checkers
    from tetramark.core.ledger import Ledger

__all__ = [
    "OBJECT_ORDER",
    "RANKS",
    "find",
    "resolve_any",
]

#: Stable tie-break order for equal scores.
OBJECT_ORDER: dict[str, int] = {
    "entity": 0,
    "stock": 1,
    "aliquot": 2,
    "box": 3,
}

#: Match tiers.  Higher wins; the ``match`` label names the winning tier.
RANKS: dict[str, int] = {
    "id": 1000,
    "alias": 900,
    "name_exact": 800,
    "name_prefix": 700,
    "name_contains": 600,
    "field_exact": 500,
    "field_contains": 400,
}


def _quote(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


# ---------------------------------------------------------------------------
# Lookups shared with history
# ---------------------------------------------------------------------------


def _core_row(ledger: Ledger, object_type: str, object_id: str) -> dict[str, Any] | None:
    rows = ledger.driver.query(
        f"SELECT * FROM {_quote(object_type)} WHERE {_quote('id')} = %s",
        (object_id,),
    )
    return dict(rows[0]) if rows else None


def _with_object_kind(
    ledger: Ledger, row: dict[str, Any], object_type: str
) -> dict[str, Any]:
    """Attach ``object`` (and the derived ``kind`` for aliquots) to a core row."""
    resolved = {**row, "object": object_type}
    if object_type == "aliquot":
        stocks = ledger.driver.query(
            "SELECT `kind` FROM `stock` WHERE `id` = %s",
            (row["stock_id"],),
        )
        if stocks:
            resolved["kind"] = stocks[0]["kind"]
    return resolved


def resolve_any(ledger: Ledger, ref: str) -> dict[str, Any]:
    """Resolve an id, a box name or an alias to one core row.

    The returned mapping is the core row plus an ``object`` key (and a
    ``kind`` key for aliquots, whose kind lives on their stock).  Ids win over
    box names, which win over aliases (the same order ``box show`` uses).  An
    alias shared by several object types is ambiguous and raises
    :class:`UsageError`.
    """
    text = str(ref).strip()
    if not text:
        raise UsageError("an object id or alias must not be empty")

    for object_type in OBJECT_TYPES:
        row = _core_row(ledger, object_type, text)
        if row is not None:
            return _with_object_kind(ledger, row, object_type)

    boxes = ledger.driver.query(
        f"SELECT * FROM {_quote('box')} WHERE {_quote('name')} = %s", (text,)
    )
    if boxes:
        return _with_object_kind(ledger, dict(boxes[0]), "box")

    aliases = ledger.driver.query(
        f"SELECT {_quote('object_type')}, {_quote('object_id')} "
        f"FROM {_quote('alias')} WHERE LOWER({_quote('alias')}) = LOWER(%s) "
        f"ORDER BY {_quote('object_type')}",
        (text,),
    )
    resolved: dict[tuple[str, str], dict[str, Any]] = {}
    for alias_row in aliases:
        object_type = str(alias_row["object_type"])
        object_id = str(alias_row["object_id"])
        row = _core_row(ledger, object_type, object_id)
        if row is not None:
            resolved[(object_type, object_id)] = _with_object_kind(
                ledger, row, object_type
            )
    if len(resolved) == 1:
        return next(iter(resolved.values()))
    if len(resolved) > 1:
        kinds = ", ".join(
            f"{row['object']} {row['id']}" for row in resolved.values()
        )
        raise UsageError(
            f"alias {text!r} is ambiguous: it names {kinds}; pass the object id"
        )
    raise LedgerError(f"unknown object {text!r} (no such id, box name or alias)")


# ---------------------------------------------------------------------------
# Kind-table columns
# ---------------------------------------------------------------------------


def _view_columns(object_type: str, kind: Kind) -> list[str]:
    """Columns of ``v_<object>_<kind>`` (core columns then kind fields)."""
    core = list(CORE_COLUMNS[object_type])
    overridden = {name for name in kind.fields if name in core}
    columns = [name for name in core if name not in overridden]
    columns.extend(kind.fields)
    return columns


def _search_columns(kind: Kind, view_columns: list[str]) -> tuple[list[str], list[str]]:
    """The name columns and the other string/text columns of a kind view."""
    name_columns = ["name"] if "name" in view_columns else []
    text_columns = [
        name
        for name in view_columns
        if name in kind.fields
        and kind.fields[name].type in (FieldType.string, FieldType.text)
        and name not in name_columns
    ]
    return name_columns, text_columns


# ---------------------------------------------------------------------------
# Find
# ---------------------------------------------------------------------------


def find(ledger: Ledger, query: str) -> list[dict[str, Any]]:
    """Search one ledger by id, alias, name or kind string/text field."""
    text = str(query).strip()
    if not text:
        raise UsageError("the search query must not be empty")
    needle = text.lower()

    candidates: dict[tuple[str, str], dict[str, Any]] = {}

    def offer(
        object_type: str,
        object_id: str,
        *,
        kind: str | None,
        score: int,
        match: str,
        name: str | None = None,
    ) -> None:
        key = (object_type, object_id)
        current = candidates.get(key)
        if current is not None and current["score"] >= score:
            return
        candidates[key] = {
            "object": object_type,
            "kind": kind,
            "id": object_id,
            "name": name,
            "score": score,
            "match": match,
        }

    # Every read below is independent, so the whole search runs as one
    # driver.query_many batch: the command must not pay one dolt process per
    # kind, per field or per hit.  Result-dependent lookups (an alias hit's
    # kind/name, an aliquot's stock and status, an object's name and location)
    # are answered from fixed statements in the same batch.
    batch: list[tuple[str, tuple[Any, ...]]] = []
    positions: dict[str, int] = {}

    def add(key: str, sql: str, params: tuple[Any, ...] = ()) -> None:
        positions[key] = len(batch)
        batch.append((sql, params))

    # 1. Exact core ids (aliquot ids live on the aliquot table; their kind
    #    comes from the stock, not from the aliquot row).
    for object_type in ("entity", "stock", "aliquot", "box"):
        if object_type == "aliquot":
            add(
                f"id:{object_type}",
                "SELECT a.`id` AS `id`, s.`kind` AS `kind` "
                "FROM `aliquot` a JOIN `stock` s ON s.`id` = a.`stock_id` "
                "WHERE a.`id` = %s",
                (text,),
            )
        else:
            add(
                f"id:{object_type}",
                f"SELECT `id`, `kind` FROM {_quote(object_type)} WHERE `id` = %s",
                (text,),
            )

    # 2. Exact aliases.
    add(
        "alias",
        f"SELECT {_quote('object_type')}, {_quote('object_id')} "
        f"FROM {_quote('alias')} WHERE LOWER({_quote('alias')}) = LOWER(%s) "
        f"ORDER BY {_quote('object_type')}",
        (text,),
    )

    # 3. Name and kind string/text fields, one view per kind.
    scans: list[tuple[str, list[str], list[str], int]] = []
    for object_type in ("entity", "stock", "box"):
        kinds = ledger.schema.kinds_for(object_type)
        for kind_name in sorted(kinds):
            kind = kinds[kind_name]
            view_columns = _view_columns(object_type, kind)
            name_columns, text_columns = _search_columns(kind, view_columns)
            if not name_columns and not text_columns:
                continue
            search_columns = [*name_columns, *text_columns]
            select_columns = list(dict.fromkeys(["id", "kind", *search_columns]))
            # Spec 5 dropped the per-kind box views; search the core `box` row
            # joined with its `box_<kind>` row instead (same columns).
            if object_type == "box":
                column_sql = box_kind_columns(kind)
                expressions = {
                    column: column_sql[column] for column in select_columns
                }
                from_sql = box_kind_from(kind_name)
            else:
                expressions = {
                    column: _quote(column) for column in select_columns
                }
                from_sql = _quote(f"v_{object_type}_{kind_name}")
            clauses: list[str] = []
            params: list[Any] = []
            for column in search_columns:
                clauses.append(
                    "LOCATE(LOWER(%s), LOWER(COALESCE("
                    f"{expressions[column]}, ''))) > 0"
                )
                params.append(needle)
            sql = (
                "SELECT "
                + ", ".join(
                    f"{expressions[column]} AS {_quote(column)}"
                    for column in select_columns
                )
                + f" FROM {from_sql} WHERE "
                + " OR ".join(clauses)
            )
            scans.append((object_type, name_columns, text_columns, len(batch)))
            add(f"scan:{object_type}:{kind_name}", sql, tuple(params))

    # 4. Fixed enrichment and identity maps.  Fetching the whole entity/stock/
    #    box/aliquot identity up front is what lets an alias hit be resolved
    #    without a follow-up query.
    add(
        "aliases",
        f"SELECT {_quote('object_type')}, {_quote('object_id')}, {_quote('alias')} "
        f"FROM {_quote('alias')} ORDER BY {_quote('object_type')}, {_quote('alias')}",
    )
    add(
        "locations",
        "SELECT l.`aliquot_id` AS `aliquot_id`, l.`stock_id` AS `stock_id`, "
        "s.`entity_id` AS `entity_id`, l.`location` AS `location` "
        "FROM `v_aliquot_location` l JOIN `stock` s ON s.`id` = l.`stock_id`",
    )
    add("entities", "SELECT `id`, `name`, `kind` FROM `entity`")
    add("stocks", "SELECT `id`, `entity_id`, `kind` FROM `stock`")
    add("boxes", "SELECT `id`, `name`, `parent_box_id`, `kind` FROM `box`")
    add("aliquots", "SELECT `id`, `stock_id`, `status` FROM `aliquot`")

    fetched = ledger.driver.query_many(batch)

    entity_rows = fetched[positions["entities"]]
    entity_names = {str(row["id"]): row.get("name") for row in entity_rows}
    entity_kinds = {str(row["id"]): str(row["kind"]) for row in entity_rows}
    stock_rows = fetched[positions["stocks"]]
    stock_entities = {
        str(row["id"]): str(row["entity_id"]) for row in stock_rows
    }
    stock_kinds = {str(row["id"]): str(row["kind"]) for row in stock_rows}
    box_rows = fetched[positions["boxes"]]
    box_names = {str(row["id"]): row.get("name") for row in box_rows}
    box_kinds = {str(row["id"]): str(row["kind"]) for row in box_rows}
    aliquot_rows = fetched[positions["aliquots"]]
    aliquot_stocks = {str(row["id"]): str(row["stock_id"]) for row in aliquot_rows}
    aliquot_statuses = {str(row["id"]): row.get("status") for row in aliquot_rows}

    def identity(
        object_type: str, object_id: str
    ) -> tuple[str, str | None] | None:
        """``(kind, name)`` of one object from the batch (no follow-up query)."""
        if object_type == "entity":
            if object_id not in entity_kinds:
                return None
            return entity_kinds[object_id], entity_names.get(object_id)
        if object_type == "stock":
            if object_id not in stock_kinds:
                return None
            return stock_kinds[object_id], None
        if object_type == "box":
            if object_id not in box_kinds:
                return None
            return box_kinds[object_id], box_names.get(object_id)
        if object_type == "aliquot":
            if object_id not in aliquot_stocks:
                return None
            return stock_kinds.get(aliquot_stocks[object_id], ""), None
        return _identity(ledger, object_type, object_id)  # pragma: no cover

    for object_type in ("entity", "stock", "aliquot", "box"):
        for row in fetched[positions[f"id:{object_type}"]]:
            offer(
                object_type,
                str(row["id"]),
                kind=str(row["kind"]),
                score=RANKS["id"],
                match="id",
            )

    # 2. Exact aliases.
    for row in fetched[positions["alias"]]:
        object_type = str(row["object_type"])
        object_id = str(row["object_id"])
        found = identity(object_type, object_id)
        if found is None:
            continue
        offer(
            object_type,
            object_id,
            kind=found[0],
            name=found[1],
            score=RANKS["alias"],
            match="alias",
        )

    # 3. Name and kind string/text fields, one view per kind.
    for object_type, name_columns, text_columns, index in scans:
        for row in fetched[index]:
            score, match = _score_row(
                row, needle, name_columns=name_columns, text_columns=text_columns
            )
            if score is None:
                continue
            name = _first_text(row, name_columns)
            offer(
                object_type,
                str(row["id"]),
                kind=str(row["kind"]),
                name=name,
                score=score,
                match=match,
            )

    if not candidates:
        return []

    # Enrich with aliases, locations and missing names/kinds (all fetched).
    aliases = _alias_map(fetched[positions["aliases"]])
    entity_locations, stock_locations, aliquot_locations = _locations(
        fetched[positions["locations"]]
    )
    paths = _box_paths(box_rows)

    results: list[dict[str, Any]] = []
    for key, candidate in candidates.items():
        object_type = candidate["object"]
        object_id = candidate["id"]
        kind_name = candidate["kind"]
        name = candidate["name"]
        if kind_name is None or (name is None and object_type in ("entity", "box")):
            found = identity(object_type, object_id)
            if found is not None:
                kind_name = kind_name or found[0]
                if name is None:
                    name = found[1]
        kind_name = kind_name or ""

        entity_id: str | None = None
        stock_id: str | None = None
        status: str | None = None
        if object_type == "stock":
            entity_id = stock_entities.get(object_id)
        elif object_type == "aliquot":
            stock_id = aliquot_stocks.get(object_id)
            entity_id = stock_entities.get(stock_id or "")
            status = aliquot_statuses.get(object_id)

        if object_type == "box":
            kind_name = kind_name or box_kinds.get(object_id, "")
            location = paths.get(object_id, name or object_id)
        elif object_type == "aliquot":
            location = aliquot_locations.get(object_id, "")
        elif object_type == "stock":
            location = ", ".join(sorted(set(stock_locations.get(object_id, []))))
        else:
            location = ", ".join(sorted(set(entity_locations.get(object_id, []))))

        results.append(
            {
                "object": object_type,
                "kind": kind_name,
                "id": object_id,
                "name": name,
                "summary": _summary(
                    object_type,
                    kind_name,
                    object_id,
                    name,
                    entity_id=entity_id,
                    entity_names=entity_names,
                    stock_id=stock_id,
                    status=status,
                ),
                "location": location,
                "aliases": aliases.get(key, []),
                "match": candidate["match"],
                "score": candidate["score"],
            }
        )

    results.sort(
        key=lambda row: (
            -int(row["score"]),
            OBJECT_ORDER.get(str(row["object"]), 99),
            str(row["kind"]),
            str(row["id"]),
        )
    )
    for index, row in enumerate(results, start=1):
        row["rank"] = index
    return results


def _first_text(row: dict[str, Any], columns: list[str]) -> str | None:
    for column in columns:
        value = row.get(column)
        if value is not None and str(value).strip():
            return str(value)
    return None


def _score_row(
    row: dict[str, Any],
    needle: str,
    *,
    name_columns: list[str],
    text_columns: list[str],
) -> tuple[int | None, str]:
    """Score one matching view row, or ``(None, '')`` when it does not match."""
    names = [
        str(row[column]).lower()
        for column in name_columns
        if row.get(column) is not None
    ]
    if any(value == needle for value in names):
        return RANKS["name_exact"], "name"
    if any(value.startswith(needle) for value in names):
        return RANKS["name_prefix"], "name"
    if any(needle in value for value in names):
        return RANKS["name_contains"], "name"

    fields = [
        str(row[column]).lower()
        for column in text_columns
        if row.get(column) is not None
    ]
    if any(value == needle for value in fields):
        return RANKS["field_exact"], "field"
    if any(needle in value for value in fields):
        return RANKS["field_contains"], "field"
    return None, ""


def _identity(
    ledger: Ledger, object_type: str, object_id: str
) -> tuple[str, str | None] | None:
    """``(kind, name)`` of one object, or ``None`` when it does not exist."""
    if object_type == "aliquot":
        rows = ledger.driver.query(
            "SELECT s.`kind` AS `kind` FROM `aliquot` a "
            "JOIN `stock` s ON s.`id` = a.`stock_id` WHERE a.`id` = %s",
            (object_id,),
        )
        return (str(rows[0]["kind"]), None) if rows else None
    row = _core_row(ledger, object_type, object_id)
    if row is None:
        return None
    name = row.get("name") if object_type in ("entity", "box") else None
    return str(row["kind"]), None if name is None else str(name)


def _summary(
    object_type: str,
    kind_name: str,
    object_id: str,
    name: str | None,
    *,
    entity_id: str | None,
    entity_names: dict[str, Any],
    stock_id: str | None,
    status: str | None,
) -> str:
    if object_type == "entity":
        return f"{kind_name} {name or object_id}"
    if object_type == "box":
        return f"{kind_name} {name or object_id}"
    if object_type == "stock":
        entity_name = entity_names.get(entity_id or "")
        suffix = f" of {entity_name}" if entity_name else ""
        return f"{kind_name} {object_id}{suffix}"
    suffix = f" ({status})" if status else ""
    return f"aliquot {object_id} of stock {stock_id or '?'}{suffix}"


def _alias_map(
    rows: list[dict[str, Any]],
) -> dict[tuple[str, str], list[str]]:
    """Aliases per ``(object_type, object_id)`` from the fetched alias rows."""
    aliases: dict[tuple[str, str], list[str]] = {}
    for row in rows:
        key = (str(row["object_type"]), str(row["object_id"]))
        aliases.setdefault(key, []).append(str(row["alias"]))
    return aliases


def _locations(
    rows: list[dict[str, Any]],
) -> tuple[dict[str, list[str]], dict[str, list[str]], dict[str, str]]:
    """Locations grouped by entity, by stock, and by aliquot (fetched rows)."""
    by_entity: dict[str, list[str]] = {}
    by_stock: dict[str, list[str]] = {}
    by_aliquot: dict[str, str] = {}
    for row in rows:
        location = row.get("location")
        if not location:
            continue
        text = str(location)
        aliquot_id = str(row["aliquot_id"])
        stock_id = str(row["stock_id"])
        entity_id = str(row["entity_id"])
        by_aliquot[aliquot_id] = text
        by_stock.setdefault(stock_id, []).append(text)
        by_entity.setdefault(entity_id, []).append(text)
    return by_entity, by_stock, by_aliquot
