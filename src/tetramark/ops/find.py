"""``tm find``: resolve a handle to objects (spec §6).

Search precedence follows the zsqlab ``find_entity.sh`` reference:

1. an exact core id (entity, stock, aliquot or box);
2. an alias (per object type, so one alias text may hit several);
3. a name or a ``string``/``text`` field of a kind table, case-insensitively.

Every hit carries the object type, kind, id, a human summary, its location
(``Freezer/Shelf/Box:A1`` for aliquots and everything above them) and the way
it matched.  The result list is ranked: exact id, exact alias, exact name, then
name prefix, alias prefix, name substring, alias substring, and field matches
last.  Ties are broken by object type and id so output is stable.

Without ``-E`` the search is deliberately literal: ``%`` and ``_`` are ordinary
characters (LOCATE, not LIKE), matching the reference script.  Whitespace in
the query is *not* split into tokens; the whole trimmed string is one handle.
A caller that needs token splitting (SnapGene file names) can call ``find``
once per token.

With ``-E`` (``regex=True``) the query is a SQL (ICU) regular expression and
the database matches it case-insensitively: the pattern is prefixed with
``(?i)``.  It is searched over the same places (ids, aliases, names, kind
``string``/``text`` fields).  A regex has no exact/prefix/substring sub-tiers,
so the scores collapse to one per tier: id > alias > name > field.  An invalid
pattern is a usage error; there is no literal fallback.

``--where`` reuses the shared filter backend of :mod:`tetramark.core.filters`
(``field=value`` / ``field~pattern``).  A hit must satisfy every filter (AND);
only entity/stock/box kinds that have *every* filtered field take part, and
aliquots never do.  With no query, the filters alone list the matching objects
(``match`` is ``"filter"``), sorted by object type, kind and id.

The statements are built first and fetched by a single
:meth:`Driver.query_many`; only an invalid pattern (error-path probes) or an
``=`` filter on a ``ref`` field (the shared coercion may resolve the ref) can
cost extra queries.  :func:`search` is one pure pass and :func:`find` the thin
orchestration around it, so Tetramark-53r.1 can add a zero-hit rewrite fallback
without changing the search core.

``find.rewrites`` rules (``ledger.yaml``, see
:class:`tetramark.schema.model.FindConfig`) may rewrite a *literal* query --
never a ``-E`` pattern and never a ``--where`` pattern.  By default a query
with zero hits is retried with every rule, in one batch, and the first rule that
produces hits is shown; ``--with-rewrite`` always runs every rule next to the
original query in the same batch and appends its hits below the original ones
(ordered by rule, then tier; an object found both ways keeps its original
entry).  A rewrite hit's ``match`` is ``"<tier> via <rule>"`` and its JSON row
carries ``rewrite: {rule, query}``.  Single-object resolution
(:func:`resolve_any` and the show/history/lineage paths) never rewrites; their
not-found errors point at ``tm find`` instead.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from tetramark.core.filters import (
    Filter,
    build_conditions,
    check_fields,
    coerce_value,
    empty_condition,
    filter_fields,
    parse_filters,
    pattern_is_invalid,
    regex_usage_error,
    validate_patterns,
)
from tetramark.core.objects import (
    OBJECT_TYPES,
    _CORE_FILTERS,
    _box_paths,
    box_kind_columns,
    box_kind_from,
)
from tetramark.errors import LedgerError, UsageError, try_find_hint
from tetramark.schema.ddl import CORE_COLUMNS
from tetramark.schema.model import FieldType, Kind, Rewrite

if TYPE_CHECKING:  # pragma: no cover - import only for type checkers
    from tetramark.core.ledger import Ledger

__all__ = [
    "OBJECT_ORDER",
    "RANKS",
    "REGEX_RANKS",
    "find",
    "resolve_any",
    "search",
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
    "alias_prefix": 690,
    "name_contains": 600,
    "alias_contains": 590,
    "field_exact": 500,
    "field_contains": 400,
}

#: Regex match tiers: ``-E`` has no exact/prefix/substring sub-tiers, so the
#: scores collapse to one per tier (id > alias > name > field); ties are broken
#: by object type, kind and id exactly like the literal search.
REGEX_RANKS: dict[str, int] = {
    "id": RANKS["id"],
    "alias": RANKS["alias"],
    "name": RANKS["name_exact"],
    "field": RANKS["field_exact"],
}

#: The label and score of a hit selected by ``--where`` alone (no query).
FILTER_MATCH = "filter"
FILTER_SCORE = 0

#: Marker column of a regex scan: whether the *name* column matched (vs a
#: kind ``string``/``text`` field).
_NAME_MARKER = "__tm_name_match"

#: The ``where=`` argument of the Python API and the CLI option values.
Where = Iterable[str] | Mapping[str, Any] | None


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
    raise LedgerError(
        f"unknown object {text!r} (no such id, box name or alias)"
        f"{try_find_hint(text)}"
    )


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
# The read batch
# ---------------------------------------------------------------------------


class _ReadBatch:
    """Statements of one find pass, run by a single ``driver.query_many``.

    Every lookup is appended here first, so the search never pays one dolt
    process per kind, per field or per hit.  Result-dependent lookups (an alias
    hit's kind/name, an aliquot's stock and status, a hit's name and location)
    are answered from the fixed enrichment maps in the same batch.
    """

    def __init__(self) -> None:
        self.statements: list[tuple[str, tuple[Any, ...]]] = []
        self.positions: dict[str, int] = {}
        #: ``(object_type, kind, result index)`` per kind carrying the filters.
        self.filter_groups: list[tuple[str, str, int]] = []

    def add(self, key: str, sql: str, params: tuple[Any, ...] = ()) -> int:
        index = len(self.statements)
        self.positions[key] = index
        self.statements.append((sql, params))
        return index

    def add_enrichment(self) -> None:
        """The identity, alias and location maps every hit is built from."""
        self.add(
            "aliases",
            f"SELECT {_quote('object_type')}, {_quote('object_id')}, {_quote('alias')} "
            f"FROM {_quote('alias')} ORDER BY {_quote('object_type')}, {_quote('alias')}",
        )
        self.add(
            "locations",
            "SELECT l.`aliquot_id` AS `aliquot_id`, l.`stock_id` AS `stock_id`, "
            "s.`entity_id` AS `entity_id`, l.`location` AS `location` "
            "FROM `v_aliquot_location` l JOIN `stock` s ON s.`id` = l.`stock_id`",
        )
        self.add("entities", "SELECT `id`, `name`, `kind` FROM `entity`")
        self.add("stocks", "SELECT `id`, `entity_id`, `kind` FROM `stock`")
        self.add("boxes", "SELECT `id`, `name`, `parent_box_id`, `kind` FROM `box`")
        self.add("aliquots", "SELECT `id`, `stock_id`, `status` FROM `aliquot`")


class _IdentityMaps:
    """Identity, alias and location maps built from one batch's result sets.

    Loading them inside the search batch is what lets an alias hit, an id hit
    or a filter-only hit be enriched without a follow-up query.
    """

    def __init__(
        self,
        fetched: list[list[dict[str, Any]]],
        positions: Mapping[str, int],
    ) -> None:
        entity_rows = fetched[positions["entities"]]
        self.entity_names = {
            str(row["id"]): row.get("name") for row in entity_rows
        }
        self.entity_kinds = {
            str(row["id"]): str(row["kind"]) for row in entity_rows
        }
        stock_rows = fetched[positions["stocks"]]
        self.stock_entities = {
            str(row["id"]): str(row["entity_id"]) for row in stock_rows
        }
        self.stock_kinds = {
            str(row["id"]): str(row["kind"]) for row in stock_rows
        }
        box_rows = fetched[positions["boxes"]]
        self.box_names = {str(row["id"]): row.get("name") for row in box_rows}
        self.box_kinds = {str(row["id"]): str(row["kind"]) for row in box_rows}
        aliquot_rows = fetched[positions["aliquots"]]
        self.aliquot_stocks = {
            str(row["id"]): str(row["stock_id"]) for row in aliquot_rows
        }
        self.aliquot_statuses = {
            str(row["id"]): row.get("status") for row in aliquot_rows
        }
        self.aliases = _alias_map(fetched[positions["aliases"]])
        (
            self.entity_locations,
            self.stock_locations,
            self.aliquot_locations,
        ) = _locations(fetched[positions["locations"]])
        self.box_paths = _box_paths(box_rows)

    def identity(
        self, object_type: str, object_id: str
    ) -> tuple[str, str | None] | None:
        """``(kind, name)`` of one object, or ``None`` when it does not exist."""
        if object_type == "entity":
            if object_id not in self.entity_kinds:
                return None
            return self.entity_kinds[object_id], self.entity_names.get(object_id)
        if object_type == "stock":
            if object_id not in self.stock_kinds:
                return None
            return self.stock_kinds[object_id], None
        if object_type == "box":
            if object_id not in self.box_kinds:
                return None
            return self.box_kinds[object_id], self.box_names.get(object_id)
        if object_type == "aliquot":
            if object_id not in self.aliquot_stocks:
                return None
            return self.stock_kinds.get(self.aliquot_stocks[object_id], ""), None
        return None


def _add_search_statements(
    batch: _ReadBatch,
    ledger: Ledger,
    text: str,
    *,
    regex: bool,
    prefix: str = "",
) -> list[tuple[str, list[str], list[str], int]]:
    """Append the id, alias and kind-text statements of one search.

    ``prefix`` namespaces the batch keys so several search passes can share one
    ``query_many`` batch (``--with-rewrite`` runs the original query and every
    rewrite together).  Returns this pass's ``(object_type, name columns, text
    columns, result index)`` scans; :func:`_collect_hits` reads them back.
    """
    pattern = _regex_pattern(text) if regex else ""
    needle = text.lower()
    scans: list[tuple[str, list[str], list[str], int]] = []

    # 1. Exact core ids (aliquot ids live on the aliquot table; their kind
    #    comes from the stock, not from the aliquot row).
    for object_type in ("entity", "stock", "aliquot", "box"):
        if object_type == "aliquot":
            sql = (
                "SELECT a.`id` AS `id`, s.`kind` AS `kind` "
                "FROM `aliquot` a JOIN `stock` s ON s.`id` = a.`stock_id` "
            )
            condition = "a.`id` REGEXP %s" if regex else "a.`id` = %s"
        else:
            sql = f"SELECT `id`, `kind` FROM {_quote(object_type)} "
            condition = "`id` REGEXP %s" if regex else "`id` = %s"
        batch.add(
            f"{prefix}id:{object_type}",
            sql + "WHERE " + condition,
            (pattern if regex else text,),
        )

    # 2. Aliases: an exact alias is its own tier; with -E every alias match is
    #    the alias tier (there is no prefix/substring distinction).
    if regex:
        batch.add(
            f"{prefix}alias_hits",
            f"SELECT {_quote('object_type')}, {_quote('object_id')}, {_quote('alias')} "
            f"FROM {_quote('alias')} WHERE {_quote('alias')} REGEXP %s "
            f"ORDER BY {_quote('object_type')}",
            (pattern,),
        )
    else:
        batch.add(
            f"{prefix}alias_hits",
            f"SELECT {_quote('object_type')}, {_quote('object_id')}, {_quote('alias')} "
            f"FROM {_quote('alias')} WHERE LOCATE(LOWER(%s), LOWER({_quote('alias')})) > 0 "
            f"ORDER BY {_quote('object_type')}",
            (needle,),
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

            select_items = [
                f"{expressions[column]} AS {_quote(column)}"
                for column in select_columns
            ]
            clauses: list[str] = []
            params: list[Any] = []
            if regex:
                if name_columns:
                    # Tell the match tier apart without a second scan: a name
                    # match outranks a field match, like the literal tiers.
                    select_items.append(
                        f"({expressions[name_columns[0]]} REGEXP %s) "
                        f"AS {_quote(_NAME_MARKER)}"
                    )
                    params.append(pattern)
                clauses = [
                    f"{expressions[column]} REGEXP %s" for column in search_columns
                ]
                params.extend([pattern] * len(search_columns))
            else:
                for column in search_columns:
                    clauses.append(
                        "LOCATE(LOWER(%s), LOWER(COALESCE("
                        f"{expressions[column]}, ''))) > 0"
                    )
                    params.append(needle)
            sql = (
                "SELECT "
                + ", ".join(select_items)
                + f" FROM {from_sql} WHERE "
                + " OR ".join(clauses)
            )
            index = batch.add(
                f"{prefix}scan:{object_type}:{kind_name}", sql, tuple(params)
            )
            scans.append((object_type, name_columns, text_columns, index))
    return scans


def _add_filter_statements(
    batch: _ReadBatch, ledger: Ledger, filters: tuple[Filter, ...]
) -> None:
    """Append one id query per kind that can express every filter.

    A kind that lacks any filtered field contributes nothing (a listing must
    never ignore a filter for a kind that cannot express it); aliquots have no
    kind row and are never searched when ``--where`` is given.
    """
    if not filters:
        return
    fields = filter_fields(filters)
    for object_type in ("entity", "stock", "box"):
        kinds = ledger.schema.kinds_for(object_type)
        for kind_name in sorted(kinds):
            kind = kinds[kind_name]
            columns = set(_CORE_FILTERS[object_type]) | set(kind.fields)
            if not fields.issubset(columns):
                continue
            # Spec 5 dropped the per-kind box views; filter the same row shape
            # by joining the core `box` row with its `box_<kind>` row.
            column_sql = (
                box_kind_columns(kind) if object_type == "box" else None
            )
            expressions = column_sql or {
                column: _quote(column) for column in columns
            }
            conditions, params = build_conditions(
                filters,
                expressions=expressions,
                coerce=lambda field, raw: coerce_value(
                    kind, field, raw, ledger=ledger, session=ledger.driver
                ),
                empty=lambda field, expression: empty_condition(
                    kind, field, expression
                ),
                kind=kind,
                option="--where",
            )
            from_sql = (
                box_kind_from(kind_name)
                if object_type == "box"
                else _quote(f"v_{object_type}_{kind_name}")
            )
            sql = f"SELECT {expressions['id']} AS {_quote('id')} FROM {from_sql}"
            if conditions:
                sql += " WHERE " + " AND ".join(conditions)
            index = batch.add(
                f"where:{object_type}:{kind_name}", sql, tuple(params)
            )
            batch.filter_groups.append((object_type, kind_name, index))


def _fetch(
    ledger: Ledger,
    batch: _ReadBatch,
    *,
    regex_query: str | None = None,
    filters: tuple[Filter, ...] = (),
) -> list[list[dict[str, Any]]]:
    """Run the batch, turning a refused pattern into a usage error.

    The database compiles patterns, so an invalid one is only known when the
    batch runs.  The error path may cost probe queries: it identifies the
    query's pattern first (``-E``), then the offending ``--where`` filter.
    """
    try:
        return ledger.driver.query_many(batch.statements)
    except LedgerError as error:
        if regex_query is not None and pattern_is_invalid(
            ledger.driver, _regex_pattern(regex_query)
        ):
            raise UsageError(_invalid_regex_message(regex_query)) from error
        usage = regex_usage_error(
            ledger.driver, filters, error, option="--where"
        )
        if usage is None:
            raise
        raise usage from error


# ---------------------------------------------------------------------------
# Find
# ---------------------------------------------------------------------------


def search(
    ledger: Ledger,
    query: str,
    *,
    regex: bool = False,
    where: Where = (),
    filters: tuple[Filter, ...] | None = None,
) -> list[dict[str, Any]]:
    """One search pass over one ledger (the pure core of :func:`find`).

    The hits are sorted but carry no ``rank`` (the orchestration numbers them).
    Exactly one search batch is built and fetched, though a ``=`` filter on a
    ``ref`` field may still resolve its ref through the shared coercion.  A
    zero-hit result is honest: no rewrite fallback lives here.  Tetramark-53r.1
    adds that in :func:`find`, which searches rewritten queries too and merges
    the hits; this function never ranks them.

    ``where`` is the filter syntax; ``filters`` is its already-parsed form,
    which :func:`find` passes so the filters are parsed once (the two are
    alternatives -- ``filters`` wins when both are given).
    """
    parsed = (
        parse_filters(where, option="--where") if filters is None else filters
    )
    _check_where_fields(ledger, parsed)
    text = str(query).strip()
    if not text:
        raise UsageError("the search query must not be empty")
    return _search_pass(ledger, text, regex=regex, filters=parsed)


def find(
    ledger: Ledger,
    query: str | None = None,
    *,
    regex: bool = False,
    where: Where = (),
    with_rewrite: bool = False,
) -> list[dict[str, Any]]:
    """Search one ledger by id, alias, name, kind text and/or ``--where``.

    ``query`` is literal unless ``regex`` is true, in which case it is a
    database (ICU) regular expression matched case-insensitively.  ``where``
    takes the same ``field=value`` / ``field~pattern`` filters as the
    entity/stock/box listings and applies to every hit (AND); aliquots are
    excluded whenever it is given.  With ``where`` and no query, every matching
    object is listed with ``match == "filter"``, sorted by object type, kind and
    id.  Passing neither a query nor a filter is a usage error.

    ``find.rewrites`` rules (``ledger.yaml``) rewrite a literal query only.
    By default they are a fallback for a query with zero hits: every rule is
    tried and the first one that produces hits is shown.  With ``with_rewrite``
    every rule runs next to the original query; original hits come first, then
    each rule's hits in rule order.  Rewrite hits carry
    ``match == "<tier> via <rule>"`` and ``rewrite == {"rule": ..., "query":
    ...}``; an object found both ways keeps its original entry.  A ``-E``
    pattern and a ``--where`` pattern are never rewritten, and the filters
    narrow rewrite hits exactly like original ones.
    """
    filters = parse_filters(where, option="--where")
    _check_where_fields(ledger, filters)
    text = "" if query is None else str(query).strip()
    if not text:
        if not filters:
            raise UsageError(
                "the search query must not be empty; pass a QUERY or at least "
                "one --where filter"
            )
        return _rank(_filter_pass(ledger, filters))

    rewrites = [] if regex else ledger.config.find.rewrites_for(text)
    if not rewrites:
        return _rank(search(ledger, text, regex=regex, filters=filters))
    if with_rewrite:
        # The original query and every rewrite in one batch.  Original hits
        # first, then each rule's tier-sorted hits in rule order; a duplicate
        # object keeps the entry it already has.
        groups = _search_many(
            ledger,
            [text, *(rewrite.query for rewrite in rewrites)],
            regex=False,
            filters=filters,
        )
        return _merge_rewrites(groups[0], rewrites, groups[1:])

    results = search(ledger, text, regex=regex, filters=filters)
    if results:
        return _rank(results)
    # Zero hits: try every rule in one batch and show the first rule whose
    # rewrite produces hits.  Each rule is matched against the *original*
    # query only, so rules never chain.
    groups = _search_many(
        ledger,
        [rewrite.query for rewrite in rewrites],
        regex=False,
        filters=filters,
    )
    for rewrite, hits in zip(rewrites, groups):
        if hits:
            return _rank(_annotate_rewrite(hits, rewrite))
    return []


def _annotate(row: dict[str, Any], rewrite: Rewrite) -> dict[str, Any]:
    """Mark one hit as produced by a rewrite rule (table and JSON shape)."""
    row["match"] = f"{row['match']} via {rewrite.rule}"
    row["rewrite"] = {"rule": rewrite.rule, "query": rewrite.query}
    return row


def _annotate_rewrite(
    hits: list[dict[str, Any]], rewrite: Rewrite
) -> list[dict[str, Any]]:
    """Annotate every hit of one successful rewrite fallback."""
    return [_annotate(row, rewrite) for row in hits]


def _merge_rewrites(
    original: list[dict[str, Any]],
    rewrites: Sequence[Rewrite],
    groups: Sequence[list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Original hits, then every rule's hits in rule order (no duplicates).

    An object found by the original query and by a rewrite (or by two rules)
    keeps the entry it was seen with first, as the spec requires.  Ranks are
    numbered over the merged list; the per-row ``score`` still names the tier
    that matched.
    """
    merged = list(original)
    seen = {(str(row["object"]), str(row["id"])) for row in merged}
    for rewrite, hits in zip(rewrites, groups):
        for row in hits:
            key = (str(row["object"]), str(row["id"]))
            if key in seen:
                continue
            seen.add(key)
            merged.append(_annotate(row, rewrite))
    return _number(merged)


def _search_pass(
    ledger: Ledger, text: str, *, regex: bool, filters: tuple[Filter, ...]
) -> list[dict[str, Any]]:
    """Build, fetch and enrich one search; the hits are sorted but unranked."""
    return _search_many(ledger, [text], regex=regex, filters=filters)[0]


def _search_many(
    ledger: Ledger,
    texts: Sequence[str],
    *,
    regex: bool,
    filters: tuple[Filter, ...],
) -> list[list[dict[str, Any]]]:
    """Search several literal queries in one batch; one hit list per query.

    The original query and every rewritten one ride in the same
    ``query_many`` batch, so the enrichment maps (identities, aliases,
    locations) are read once.  With one text this is exactly the pass behind
    :func:`search`; rewrites are only ever literal, so ``regex`` is never
    combined with more than one text.
    """
    batch = _ReadBatch()
    scopes: list[tuple[str, list[tuple[str, list[str], list[str], int]]]] = []
    for index, text in enumerate(texts):
        prefix = f"q{index}:" if len(texts) > 1 else ""
        scans = _add_search_statements(
            batch, ledger, text, regex=regex, prefix=prefix
        )
        scopes.append((prefix, scans))
    _add_filter_statements(batch, ledger, filters)
    batch.add_enrichment()
    # No kind carries the filters (a combination no kind can express), so no
    # statement compiles their patterns: check them here, otherwise an invalid
    # pattern would silently match nothing.
    if filters and not batch.filter_groups:
        validate_patterns(ledger.driver, filters, option="--where")
    fetched = _fetch(
        ledger,
        batch,
        regex_query=texts[0] if regex else None,
        filters=filters,
    )
    maps = _IdentityMaps(fetched, batch.positions)
    allowed = _allowed_ids(fetched, batch.filter_groups) if filters else None
    return [
        _collect_hits(
            fetched,
            batch.positions,
            scans,
            maps,
            allowed,
            prefix=prefix,
            text=texts[index],
            regex=regex,
        )
        for index, (prefix, scans) in enumerate(scopes)
    ]


def _collect_hits(
    fetched: list[list[dict[str, Any]]],
    positions: Mapping[str, int],
    scans: Sequence[tuple[str, list[str], list[str], int]],
    maps: _IdentityMaps,
    allowed: set[tuple[str, str, str]] | None,
    *,
    prefix: str,
    text: str,
    regex: bool,
) -> list[dict[str, Any]]:
    """Build the enriched, sorted hits of one search from its batch result."""
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

    # 1. Ids (exact, or a regex match with -E).
    for object_type in ("entity", "stock", "aliquot", "box"):
        for row in fetched[positions[f"{prefix}id:{object_type}"]]:
            offer(
                object_type,
                str(row["id"]),
                kind=str(row["kind"]),
                score=RANKS["id"],
                match="id",
            )

    # 2. Aliases: rank exact/prefix/contains literally; with -E every alias
    #    match is the alias tier.  The identity comes from the batched maps.
    for row in fetched[positions[f"{prefix}alias_hits"]]:
        object_type = str(row["object_type"])
        object_id = str(row["object_id"])
        found = maps.identity(object_type, object_id)
        if found is None:
            continue
        if regex:
            score, match = REGEX_RANKS["alias"], "alias"
        else:
            alias = str(row["alias"]).lower()
            if alias == needle:
                score, match = RANKS["alias"], "alias"
            elif alias.startswith(needle):
                score, match = RANKS["alias_prefix"], "alias"
            else:
                score, match = RANKS["alias_contains"], "alias"
        offer(
            object_type,
            object_id,
            kind=found[0],
            name=found[1],
            score=score,
            match=match,
        )

    # 3. Name and kind string/text fields, one view per kind.
    for object_type, name_columns, text_columns, index in scans:
        for row in fetched[index]:
            if regex:
                matched_name = bool(row.get(_NAME_MARKER))
                score = REGEX_RANKS["name"] if matched_name else REGEX_RANKS["field"]
                match = "name" if matched_name else "field"
            else:
                score, match = _score_row(
                    row,
                    needle,
                    name_columns=name_columns,
                    text_columns=text_columns,
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

    if allowed is not None:
        candidates = {
            key: candidate
            for key, candidate in candidates.items()
            if (
                candidate["object"],
                str(candidate["kind"]),
                candidate["id"],
            )
            in allowed
        }

    if not candidates:
        return []
    return _enrich(fetched, candidates, maps)


def _filter_pass(
    ledger: Ledger, filters: tuple[Filter, ...]
) -> list[dict[str, Any]]:
    """List every object the filters match (the no-QUERY mode)."""
    batch = _ReadBatch()
    _add_filter_statements(batch, ledger, filters)
    batch.add_enrichment()
    # No kind carries the filters: nothing would compile their patterns.
    if not batch.filter_groups:
        validate_patterns(ledger.driver, filters, option="--where")
    fetched = _fetch(ledger, batch, filters=filters)
    maps = _IdentityMaps(fetched, batch.positions)
    candidates: dict[tuple[str, str], dict[str, Any]] = {}
    for object_type, kind_name, index in batch.filter_groups:
        for row in fetched[index]:
            object_id = str(row["id"])
            candidates[(object_type, object_id)] = {
                "object": object_type,
                "kind": kind_name,
                "id": object_id,
                "name": None,
                "score": FILTER_SCORE,
                "match": FILTER_MATCH,
            }
    if not candidates:
        return []
    return _enrich(fetched, candidates, maps)


def _allowed_ids(
    fetched: list[list[dict[str, Any]]],
    groups: Iterable[tuple[str, str, int]],
) -> set[tuple[str, str, str]]:
    """The ``(object_type, kind, id)`` triples the filters let through."""
    allowed: set[tuple[str, str, str]] = set()
    for object_type, kind_name, index in groups:
        for row in fetched[index]:
            allowed.add((object_type, kind_name, str(row["id"])))
    return allowed


def _check_where_fields(ledger: Ledger, filters: tuple[Filter, ...]) -> None:
    """Refuse a filter on a field no entity/stock/box kind can address.

    The valid set is the union of the core filter columns of entity, stock and
    box and every field of every entity/stock/box kind; a *kind* that lacks one
    of the filtered fields still contributes nothing (see
    :func:`_add_filter_statements`).
    """
    if not filters:
        return
    valid: set[str] = set()
    for object_type in ("entity", "stock", "box"):
        valid |= set(_CORE_FILTERS[object_type])
        for kind in ledger.schema.kinds_for(object_type).values():
            valid |= set(kind.fields)
    check_fields(filters, valid, option="--where")


def _enrich(
    fetched: list[list[dict[str, Any]]],
    candidates: Mapping[tuple[str, str], dict[str, Any]],
    maps: _IdentityMaps,
) -> list[dict[str, Any]]:
    """Build the result rows (summary, location, aliases) from the batch."""
    results: list[dict[str, Any]] = []
    for key, candidate in candidates.items():
        object_type = candidate["object"]
        object_id = candidate["id"]
        kind_name = candidate["kind"]
        name = candidate["name"]
        if kind_name is None or (name is None and object_type in ("entity", "box")):
            found = maps.identity(object_type, object_id)
            if found is not None:
                kind_name = kind_name or found[0]
                if name is None:
                    name = found[1]
        kind_name = kind_name or ""

        entity_id: str | None = None
        stock_id: str | None = None
        status: str | None = None
        if object_type == "stock":
            entity_id = maps.stock_entities.get(object_id)
        elif object_type == "aliquot":
            stock_id = maps.aliquot_stocks.get(object_id)
            entity_id = maps.stock_entities.get(stock_id or "")
            status = maps.aliquot_statuses.get(object_id)

        if object_type == "box":
            kind_name = kind_name or maps.box_kinds.get(object_id, "")
            location = maps.box_paths.get(object_id, name or object_id)
        elif object_type == "aliquot":
            location = maps.aliquot_locations.get(object_id, "")
        elif object_type == "stock":
            location = ", ".join(
                sorted(set(maps.stock_locations.get(object_id, [])))
            )
        else:
            location = ", ".join(
                sorted(set(maps.entity_locations.get(object_id, [])))
            )

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
                    entity_names=maps.entity_names,
                    stock_id=stock_id,
                    status=status,
                ),
                "location": location,
                "aliases": maps.aliases.get(key, []),
                "match": candidate["match"],
                "score": candidate["score"],
            }
        )

    results.sort(key=_result_key)
    return results


def _rank(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort and number the hits; ``find`` is where ranks are assigned."""
    results.sort(key=_result_key)
    return _number(results)


def _number(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Number already-ordered hits without re-sorting (rewrite merges)."""
    for index, row in enumerate(results, start=1):
        row["rank"] = index
    return results


def _result_key(row: Mapping[str, Any]) -> tuple[int, int, str, str]:
    """Score first, then the stable tie-break (object type, kind, id)."""
    return (
        -int(row["score"]),
        OBJECT_ORDER.get(str(row["object"]), 99),
        str(row["kind"]),
        str(row["id"]),
    )


def _regex_pattern(text: str) -> str:
    """``-E``'s query as a database regex, matched case-insensitively."""
    return "(?i)" + text


def _invalid_regex_message(query: str) -> str:
    """The usage error for a ``-E`` query the database refuses to compile."""
    return (
        f"invalid regex {query!r}: the pattern is not a valid regular expression"
    )


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
    """Score one literal match, or ``(None, '')`` when it does not match."""
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
