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

from tetramark.core.objects import OBJECT_TYPES, _box_paths
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

    # 1. Exact core ids (aliquot ids live on the aliquot table; their kind
    #    comes from the stock, not from the aliquot row).
    for object_type in ("entity", "stock", "aliquot", "box"):
        if object_type == "aliquot":
            rows = ledger.driver.query(
                "SELECT a.`id` AS `id`, s.`kind` AS `kind` "
                "FROM `aliquot` a JOIN `stock` s ON s.`id` = a.`stock_id` "
                "WHERE a.`id` = %s",
                (text,),
            )
        else:
            rows = ledger.driver.query(
                f"SELECT `id`, `kind` FROM {_quote(object_type)} WHERE `id` = %s",
                (text,),
            )
        for row in rows:
            offer(
                object_type,
                str(row["id"]),
                kind=str(row["kind"]),
                score=RANKS["id"],
                match="id",
            )

    # 2. Exact aliases.
    for row in ledger.driver.query(
        f"SELECT {_quote('object_type')}, {_quote('object_id')} "
        f"FROM {_quote('alias')} WHERE LOWER({_quote('alias')}) = LOWER(%s) "
        f"ORDER BY {_quote('object_type')}",
        (text,),
    ):
        object_type = str(row["object_type"])
        object_id = str(row["object_id"])
        identity = _identity(ledger, object_type, object_id)
        if identity is None:
            continue
        offer(
            object_type,
            object_id,
            kind=identity[0],
            name=identity[1],
            score=RANKS["alias"],
            match="alias",
        )

    # 3. Name and kind string/text fields, one view per kind.
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
            clauses: list[str] = []
            params: list[Any] = []
            for column in search_columns:
                clauses.append(
                    f"LOCATE(LOWER(%s), LOWER(COALESCE({_quote(column)}, ''))) > 0"
                )
                params.append(needle)
            sql = (
                f"SELECT {', '.join(_quote(column) for column in select_columns)} "
                f"FROM {_quote(f'v_{object_type}_{kind_name}')} "
                "WHERE " + " OR ".join(clauses)
            )
            for row in ledger.driver.query(sql, tuple(params)):
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

    # Enrich with aliases, locations and missing names/kinds.
    aliases = _alias_map(ledger)
    entity_locations, stock_locations, aliquot_locations = _locations(ledger)
    entity_names = {
        str(row["id"]): row.get("name")
        for row in ledger.driver.query("SELECT `id`, `name` FROM `entity`")
    }
    stock_entities = {
        str(row["id"]): str(row["entity_id"])
        for row in ledger.driver.query("SELECT `id`, `entity_id` FROM `stock`")
    }
    paths = _box_paths(
        ledger.driver.query("SELECT `id`, `name`, `parent_box_id` FROM `box`")
    )

    results: list[dict[str, Any]] = []
    for key, candidate in candidates.items():
        object_type = candidate["object"]
        object_id = candidate["id"]
        kind_name = candidate["kind"]
        name = candidate["name"]
        if kind_name is None or (name is None and object_type in ("entity", "box")):
            identity = _identity(ledger, object_type, object_id)
            if identity is not None:
                kind_name = kind_name or identity[0]
                if name is None:
                    name = identity[1]
        kind_name = kind_name or ""

        entity_id: str | None = None
        stock_id: str | None = None
        status: str | None = None
        if object_type == "stock":
            entity_id = stock_entities.get(object_id)
        elif object_type == "aliquot":
            stock_id = _aliquot_stock(ledger, object_id)
            entity_id = stock_entities.get(stock_id or "")
            status = _aliquot_status(ledger, object_id)

        if object_type == "box":
            kind_name = kind_name or _box_kind(ledger, object_id)
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


def _box_kind(ledger: Ledger, object_id: str) -> str:
    rows = ledger.driver.query(
        "SELECT `kind` FROM `box` WHERE `id` = %s", (object_id,)
    )
    return str(rows[0]["kind"]) if rows else ""


def _aliquot_stock(ledger: Ledger, object_id: str) -> str | None:
    rows = ledger.driver.query(
        "SELECT `stock_id` FROM `aliquot` WHERE `id` = %s", (object_id,)
    )
    return str(rows[0]["stock_id"]) if rows else None


def _aliquot_status(ledger: Ledger, object_id: str) -> str | None:
    rows = ledger.driver.query(
        "SELECT `status` FROM `aliquot` WHERE `id` = %s", (object_id,)
    )
    return None if not rows else rows[0].get("status")


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


def _alias_map(ledger: Ledger) -> dict[tuple[str, str], list[str]]:
    aliases: dict[tuple[str, str], list[str]] = {}
    for row in ledger.driver.query(
        f"SELECT {_quote('object_type')}, {_quote('object_id')}, {_quote('alias')} "
        f"FROM {_quote('alias')} ORDER BY {_quote('object_type')}, {_quote('alias')}"
    ):
        key = (str(row["object_type"]), str(row["object_id"]))
        aliases.setdefault(key, []).append(str(row["alias"]))
    return aliases


def _locations(
    ledger: Ledger,
) -> tuple[dict[str, list[str]], dict[str, list[str]], dict[str, str]]:
    """Locations grouped by entity, by stock, and by aliquot."""
    by_entity: dict[str, list[str]] = {}
    by_stock: dict[str, list[str]] = {}
    by_aliquot: dict[str, str] = {}
    for row in ledger.driver.query(
        "SELECT l.`aliquot_id` AS `aliquot_id`, l.`stock_id` AS `stock_id`, "
        "s.`entity_id` AS `entity_id`, l.`location` AS `location` "
        "FROM `v_aliquot_location` l JOIN `stock` s ON s.`id` = l.`stock_id`"
    ):
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
