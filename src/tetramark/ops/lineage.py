"""``tm lineage``: the descent graph of entities and stocks (spec §6).

Lineage (pedigree), not phylogeny: it answers *where did this material come
from, and what derives from it*.  The graph is built from data that already
exists; only the ``lineage: true`` flag on a ``type: ref`` field is new.

Edges point **child → parent** (upstream), and there are four sources:

* every stock to its entity (``entity``);
* ``stock.parent_stock_id`` (``parent_stock``);
* ``stock.source_aliquot_id`` — child stock → that aliquot → the stock the
  aliquot belongs to (``source_aliquot``);
* every ``lineage: true`` ref, named ``<kind>.<field>`` in the output, to an
  entity kind, a stock kind, or the core ``entity`` / ``stock`` / ``aliquot``.
  Other refs are ordinary fields and never edges.

Nodes are entities and stocks.  A stock node shows its kind, id, entity name
and the ``aliquots_in_stock/aliquots_total`` counts of its ``v_stock_<kind>``
view; :func:`stock_aliquot_counts` is the one accessor for those columns, so a
rename of the view columns only touches that function (see
:data:`STOCK_COUNT_COLUMNS`).  An aliquot is a node only when a relation names
one; it then has one upstream edge to the stock that owns it, carrying the same
``via`` as the relation that named it, so a ``source_aliquot`` path reads
child stock → source aliquot → that aliquot's stock.

The graph is built eagerly from the core tables plus one query for every kind
that has a ``lineage: true`` field (only rows where the field is set).  Those
reads are independent, so they all run in **one** ``driver.query_many`` batch
together with the rows root resolution needs; the aliquot ownership reads
(which depend on the graph) share a second batch.  Traversal is lazy: it walks
only the requested directions, stops at ``--depth`` and never visits a node
twice, so a cycle in hand-written data (a ``cell_line.parental`` loop)
terminates instead of hanging.  Stock counts are read **after** traversal, in
one batch for the visited stocks only: ``--depth 0`` does not scan the whole
ledger, and an unvisited kind's missing count view cannot fail the walk.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterable, Sequence

from tetramark.core.objects import OBJECT_TYPES
from tetramark.errors import LedgerError, UsageError
from tetramark.schema.ddl import table_name
from tetramark.schema.model import EffectiveSchema, Field, FieldType, Kind, ObjectType

if TYPE_CHECKING:  # pragma: no cover - import only for type checkers
    from tetramark.core.ledger import Ledger

__all__ = [
    "LineageEdge",
    "LineageNode",
    "LineageResult",
    "STOCK_COUNT_COLUMNS",
    "lineage",
    "render_dot",
    "render_text",
    "stock_aliquot_counts",
]

#: Columns of ``v_stock_<kind>`` that the stock node counts are read from, as
#: ``(aliquots in stock, aliquots total)``.  Ticket 25 renames the first one to
#: ``aliquots_in_stock`` when the stock views switch to status sets; that
#: rename is the only change this module needs (the accessor
#: :func:`stock_aliquot_counts` is the single reader).
STOCK_COUNT_COLUMNS: tuple[str, str] = ("aliquots_in_stock", "aliquots_total")

#: Tie-break order of object types in the tree and in the node list.
OBJECT_ORDER: dict[str, int] = {"entity": 0, "stock": 1, "aliquot": 2}

#: SQL parameter chunks (Dolt/MySQL has a limit on placeholders per statement).
_CHUNK = 500


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LineageNode:
    """One node of the lineage graph: an entity, a stock or an aliquot."""

    id: str
    #: ``entity``, ``stock`` or ``aliquot`` (the core object).
    object: str
    kind: str
    #: Display name: the entity name (stock nodes: the entity they belong to).
    label: str
    #: Aliquots only: the stock that owns the tube.
    stock_id: str | None = None
    #: Stocks only: ``(aliquots in stock, aliquots total)``.
    counts: tuple[int, int] | None = None

    def payload(self) -> dict[str, Any]:
        """The JSON shape of ``--json`` (``counts`` only for stocks)."""
        payload: dict[str, Any] = {
            "id": self.id,
            "object": self.object,
            "kind": self.kind,
            "label": self.label,
        }
        if self.counts is not None:
            payload["counts"] = {
                "in_stock": self.counts[0],
                "total": self.counts[1],
            }
        return payload


@dataclass(frozen=True)
class LineageEdge:
    """One descent edge, always written child → parent."""

    child: str
    parent: str
    #: ``entity``, ``parent_stock``, ``source_aliquot`` or ``<kind>.<field>``.
    via: str


@dataclass(frozen=True)
class _Line:
    """One printed tree line (a node reached in one direction)."""

    node_id: str
    depth: int
    #: ``-1`` ancestor (up), ``+1`` descendant (down), ``0`` the root.
    direction: int
    via: str | None
    #: True when the node was already printed: no subtree, ``(see above)``.
    repeated: bool


@dataclass(frozen=True)
class LineageResult:
    """The reachable part of the graph for one root."""

    root: LineageNode
    nodes: list[LineageNode]
    edges: list[LineageEdge]
    lines: list[_Line]

    def as_payload(self) -> dict[str, Any]:
        """The ``--json`` document: ``{root, nodes, edges}`` (spec §6)."""
        return {
            "root": self.root.id,
            "nodes": [node.payload() for node in self.nodes],
            "edges": [
                {"from": edge.child, "to": edge.parent, "via": edge.via}
                for edge in self.edges
            ],
        }


# ---------------------------------------------------------------------------
# Graph building
# ---------------------------------------------------------------------------


def _quote(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


def _placeholders(count: int) -> str:
    return ", ".join(["%s"] * count)


class _Graph:
    """All nodes and child → parent edges of one ledger."""

    def __init__(self) -> None:
        self.nodes: dict[str, LineageNode] = {}
        #: node id → [(neighbour id, via)]; ``parents`` are upstream.
        self.parents: dict[str, list[tuple[str, str]]] = defaultdict(list)
        self.children: dict[str, list[tuple[str, str]]] = defaultdict(list)
        self.edges: dict[tuple[str, str, str], LineageEdge] = {}

    def add_node(self, node: LineageNode) -> None:
        self.nodes.setdefault(node.id, node)

    def add_edge(self, child: str, parent: str, via: str) -> None:
        if child == parent:
            # A self-reference says nothing about descent; keep the graph clean
            # (traversal guards cycles regardless).
            return
        key = (child, parent, via)
        if key in self.edges:
            return
        self.edges[key] = LineageEdge(child=child, parent=parent, via=via)
        self.parents[child].append((parent, via))
        self.children[parent].append((child, via))


def _lineage_fields(kind: Kind) -> dict[str, Field]:
    return {
        name: field
        for name, field in kind.fields.items()
        if field.type is FieldType.ref and field.lineage
    }


def _target_object(schema: EffectiveSchema, target: str) -> str:
    kind = schema.kinds.get(target)
    return target if kind is None else kind.object.value


def _aliquot_owner_queries(
    aliquot_ids: Sequence[str],
) -> list[tuple[str, tuple[str, ...]]]:
    """``{aliquot id: stock id}`` queries, one per 500-id chunk."""
    ids = list(dict.fromkeys(str(aliquot_id) for aliquot_id in aliquot_ids))
    queries: list[tuple[str, tuple[str, ...]]] = []
    for start in range(0, len(ids), _CHUNK):
        chunk = ids[start : start + _CHUNK]
        queries.append(
            (
                f"SELECT {_quote('id')}, {_quote('stock_id')} FROM {_quote('aliquot')} "
                f"WHERE {_quote('id')} IN ({_placeholders(len(chunk))})",
                tuple(chunk),
            )
        )
    return queries


def _collect_owners(
    rows: Iterable[dict[str, Any]], owners: dict[str, str]
) -> None:
    for row in rows:
        owners[str(row["id"])] = str(row["stock_id"])


# ---------------------------------------------------------------------------
# One read batch: the graph rows and everything root resolution needs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Snapshot:
    """The rows the graph-building batch returned, position-mapped by :func:`_load`."""

    entity_rows: list[dict[str, Any]]
    stock_rows: list[dict[str, Any]]
    #: ``(kind name, lineage fields, rows)`` in sorted kind order; the rows are
    #: what the per-kind ``lineage: true`` query returns.
    lineage: list[tuple[str, dict[str, Field], list[dict[str, Any]]]]
    #: Rows of the box table matching the ref by id or by name.
    box_rows: list[dict[str, Any]]
    #: ``id``/``stock_id`` when the ref is an aliquot id (empty otherwise).
    aliquot_rows: list[dict[str, Any]]
    #: Alias rows of the ref that name an existing object, ordered by type.
    alias_rows: list[dict[str, Any]]


def _lineage_queries(
    schema: EffectiveSchema,
) -> list[tuple[str, dict[str, Field], str]]:
    """One SELECT per kind with ``lineage: true`` refs, in sorted kind order."""
    queries: list[tuple[str, dict[str, Field], str]] = []
    for kind_name in sorted(schema.kinds):
        kind = schema.kinds[kind_name]
        if kind.object is ObjectType.box:
            continue
        fields = _lineage_fields(kind)
        if not fields:
            continue
        table = table_name(kind.object, kind_name)
        select = ", ".join(_quote(name) for name in fields)
        where = " OR ".join(f"{_quote(name)} IS NOT NULL" for name in fields)
        queries.append(
            (
                kind_name,
                fields,
                f"SELECT {_quote('id')}, {select} FROM {_quote(table)} WHERE {where}",
            )
        )
    return queries


def _alias_identification_sql() -> str:
    """The ref's alias rows that name an existing object, ordered by type.

    ``resolve_any`` ignores an alias row whose object does not exist, so the
    existence check is part of the batch; ``object_type`` order is what its
    ambiguity message uses.
    """
    exists = " OR ".join(
        f"(al.{_quote('object_type')} = %s AND EXISTS ("
        f"SELECT 1 FROM {_quote(object_type)} alias_target "
        f"WHERE alias_target.{_quote('id')} = al.{_quote('object_id')}))"
        for object_type in OBJECT_TYPES
    )
    return (
        f"SELECT al.{_quote('object_type')}, al.{_quote('object_id')} "
        f"FROM {_quote('alias')} al "
        f"WHERE LOWER(al.{_quote('alias')}) = LOWER(%s) AND ({exists}) "
        f"ORDER BY al.{_quote('object_type')}"
    )


def _load(ledger: Ledger, ref: str) -> _Snapshot:
    """Run every graph/root-resolution read as ONE ``query_many`` batch."""
    schema = ledger.schema
    statements: list[tuple[str, Any]] = []
    positions: dict[str, int] = {}

    def add(key: str, sql: str, params: tuple[Any, ...] = ()) -> None:
        positions[key] = len(statements)
        statements.append((sql, params))

    add(
        "entity",
        f"SELECT {_quote('id')}, {_quote('kind')}, {_quote('name')} "
        f"FROM {_quote('entity')}",
    )
    add(
        "stock",
        f"SELECT {_quote('id')}, {_quote('kind')}, {_quote('entity_id')}, "
        f"{_quote('parent_stock_id')}, {_quote('source_aliquot_id')} "
        f"FROM {_quote('stock')}",
    )
    lineage_queries = _lineage_queries(schema)
    for index, (_kind_name, _fields, sql) in enumerate(lineage_queries):
        add(f"lineage:{index}", sql)
    text = str(ref).strip()
    add(
        "box",
        f"SELECT {_quote('id')}, {_quote('name')}, {_quote('kind')} "
        f"FROM {_quote('box')} WHERE {_quote('id')} = %s OR {_quote('name')} = %s",
        (text, text),
    )
    add(
        "aliquot",
        f"SELECT {_quote('id')}, {_quote('stock_id')} FROM {_quote('aliquot')} "
        f"WHERE {_quote('id')} = %s",
        (text,),
    )
    add(
        "alias",
        _alias_identification_sql(),
        (text, *OBJECT_TYPES),
    )
    fetched = ledger.driver.query_many(statements)
    return _Snapshot(
        entity_rows=fetched[positions["entity"]],
        stock_rows=fetched[positions["stock"]],
        lineage=[
            (kind_name, fields, fetched[positions[f"lineage:{index}"]])
            for index, (kind_name, fields, _sql) in enumerate(lineage_queries)
        ],
        box_rows=fetched[positions["box"]],
        aliquot_rows=fetched[positions["aliquot"]],
        alias_rows=fetched[positions["alias"]],
    )


def _resolve_ref(snapshot: _Snapshot, ref: str) -> tuple[str, str]:
    """Resolve ``ref`` like ``tm find``'s ``resolve_any``: id, box name, alias.

    The id order is :data:`OBJECT_TYPES` (entity, stock, aliquot, box); a box
    name wins over an alias, and an alias naming several existing objects is
    ambiguous.  The error text is the one ``resolve_any`` raises.
    """
    text = str(ref).strip()
    if not text:
        raise UsageError("an object id or alias must not be empty")

    entity_ids = {str(row["id"]) for row in snapshot.entity_rows}
    stock_ids = {str(row["id"]) for row in snapshot.stock_rows}
    aliquot_ids = {str(row["id"]) for row in snapshot.aliquot_rows}
    box_ids = {str(row["id"]) for row in snapshot.box_rows}
    if text in entity_ids:
        return "entity", text
    if text in stock_ids:
        return "stock", text
    if text in aliquot_ids:
        return "aliquot", text
    if text in box_ids:
        return "box", text
    for row in snapshot.box_rows:
        if str(row["name"]) == text:
            return "box", str(row["id"])
    candidates: dict[tuple[str, str], None] = {}
    for row in snapshot.alias_rows:
        candidates.setdefault((str(row["object_type"]), str(row["object_id"])), None)
    if len(candidates) == 1:
        return next(iter(candidates))
    if len(candidates) > 1:
        kinds = ", ".join(
            f"{object_type} {object_id}" for object_type, object_id in candidates
        )
        raise UsageError(
            f"alias {text!r} is ambiguous: it names {kinds}; pass the object id"
        )
    raise LedgerError(f"unknown object {text!r} (no such id, box name or alias)")


def _build(
    snapshot: _Snapshot, schema: EffectiveSchema
) -> tuple[_Graph, list[tuple[str, str, str]]]:
    """Every entity, stock and descent edge; returns the graph and its tube links."""
    graph = _Graph()

    entity_names: dict[str, Any] = {}
    for row in snapshot.entity_rows:
        entity_id = str(row["id"])
        name = row.get("name")
        entity_names[entity_id] = name
        graph.add_node(
            LineageNode(
                id=entity_id,
                object="entity",
                kind=str(row["kind"]),
                label="" if name is None else str(name),
            )
        )

    stock_rows = [dict(row) for row in snapshot.stock_rows]
    for row in stock_rows:
        entity_name = entity_names.get(str(row["entity_id"]))
        graph.add_node(
            LineageNode(
                id=str(row["id"]),
                object="stock",
                kind=str(row["kind"]),
                label="" if entity_name is None else str(entity_name),
            )
        )
    for row in stock_rows:
        stock_id = str(row["id"])
        graph.add_edge(stock_id, str(row["entity_id"]), "entity")
        parent_stock = row.get("parent_stock_id")
        if parent_stock is not None:
            graph.add_edge(stock_id, str(parent_stock), "parent_stock")

    # Aliquots are nodes only where a relation names one.  ``links`` holds
    # (node that names the tube, tube id, via); the tube then continues to the
    # stock that owns it, with the same via.
    links: list[tuple[str, str, str]] = []
    for row in stock_rows:
        source = row.get("source_aliquot_id")
        if source is not None:
            links.append((str(row["id"]), str(source), "source_aliquot"))

    for kind_name, fields, rows in snapshot.lineage:
        for row in rows:
            child_id = str(row["id"])
            if child_id not in graph.nodes:
                continue  # defensive: a kind row without its core row
            for field_name, field in fields.items():
                value = row.get(field_name)
                if value is None or str(value) == "":
                    continue
                via = f"{kind_name}.{field_name}"
                if _target_object(schema, str(field.to)) == "aliquot":
                    links.append((child_id, str(value), via))
                else:
                    graph.add_edge(child_id, str(value), via)
    return graph, links


def _add_aliquots(
    graph: _Graph,
    links: Sequence[tuple[str, str, str]],
    owners: dict[str, str],
) -> None:
    """Add the referenced aliquot nodes and their edges, after the built graph."""
    for child_id, aliquot_id, via in links:
        stock_id = owners.get(aliquot_id)
        if stock_id is None:
            continue  # defensive: a dangling source/ref value
        stock = graph.nodes.get(stock_id)
        graph.add_node(
            LineageNode(
                id=aliquot_id,
                object="aliquot",
                kind="" if stock is None else stock.kind,
                label=f"aliquot of {stock_id}",
                stock_id=stock_id,
            )
        )
        graph.add_edge(child_id, aliquot_id, via)
        graph.add_edge(aliquot_id, stock_id, via)


# ---------------------------------------------------------------------------
# The count accessor ticket 25 changes
# ---------------------------------------------------------------------------


def _count_queries(
    kind: str, stock_ids: Iterable[str]
) -> list[tuple[str, tuple[str, ...]]]:
    """The ``v_stock_<kind>`` count queries, one per 500-id chunk."""
    ids = list(dict.fromkeys(str(stock_id) for stock_id in stock_ids))
    in_stock_column, total_column = STOCK_COUNT_COLUMNS
    view = _quote(f"v_stock_{kind}")
    queries: list[tuple[str, tuple[str, ...]]] = []
    for start in range(0, len(ids), _CHUNK):
        chunk = ids[start : start + _CHUNK]
        queries.append(
            (
                f"SELECT {_quote('id')}, {_quote(in_stock_column)} AS {_quote('in_stock')}, "
                f"{_quote(total_column)} AS {_quote('total')} FROM {view} "
                f"WHERE {_quote('id')} IN ({_placeholders(len(chunk))})",
                tuple(chunk),
            )
        )
    return queries


def _collect_counts(
    rows: Iterable[dict[str, Any]], counts: dict[str, tuple[int, int]]
) -> None:
    for row in rows:
        counts[str(row["id"])] = (
            int(row.get("in_stock") or 0),
            int(row.get("total") or 0),
        )


def stock_aliquot_counts(
    ledger: Ledger, kind: str, stock_ids: Iterable[str]
) -> dict[str, tuple[int, int]]:
    """``{stock id: (aliquots in stock, aliquots total)}`` for one stock kind.

    This is the **only** reader of the ``v_stock_<kind>`` count columns, and
    :data:`STOCK_COUNT_COLUMNS` is the only place that names them: when the
    stock views rename ``aliquots_stored`` to ``aliquots_in_stock`` (ticket
    25), change that tuple and nothing else in this module (or in the CLI).
    """
    counts: dict[str, tuple[int, int]] = {}
    for sql, params in _count_queries(kind, stock_ids):
        _collect_counts(ledger.driver.query(sql, params), counts)
    return counts


# ---------------------------------------------------------------------------
# Root resolution and traversal
# ---------------------------------------------------------------------------


def _root_node(
    graph: _Graph, object_type: str, object_id: str, owners: dict[str, str]
) -> LineageNode:
    """The resolved root as a node; an unreferenced aliquot gets its stock edge."""
    if object_type == "box":
        raise UsageError(
            f"box {object_id!r} has no lineage: the graph nodes are entities, "
            "stocks and aliquots"
        )
    node = graph.nodes.get(object_id)
    if node is not None:
        return node
    if object_type == "aliquot":
        stock_id = owners.get(object_id)
        stock = None if stock_id is None else graph.nodes.get(stock_id)
        node = LineageNode(
            id=object_id,
            object="aliquot",
            kind="" if stock is None else stock.kind,
            label=f"aliquot of {stock_id}" if stock_id else "aliquot",
            stock_id=stock_id,
        )
        graph.add_node(node)
        if stock is not None:
            # No relation names this tube, so --up still reaches its stock
            # through the core column that can: ``source_aliquot_id``.
            graph.add_edge(object_id, stock.id, "source_aliquot")
        return node
    raise UsageError(  # pragma: no cover - every entity/stock is a node
        f"{object_type} {object_id} is not part of the lineage graph"
    )


def _starts(
    graph: _Graph,
    node_id: str,
    neighbours: Sequence[tuple[str, str]],
    *,
    direction: int,
    level: int,
) -> list[tuple[str, int, str, LineageEdge, int]]:
    """Stack items for ``walk``, reversed so ``pop`` visits them in order."""
    def order(pair: tuple[str, str]) -> tuple[int, str, str]:
        neighbour_id, via = pair
        node = graph.nodes.get(neighbour_id)
        if node is None:
            return (9, "", neighbour_id)
        return (OBJECT_ORDER.get(node.object, 9), via, neighbour_id)

    items: list[tuple[str, int, str, LineageEdge, int]] = []
    for neighbour_id, via in sorted(neighbours, key=order):
        neighbour = graph.nodes.get(neighbour_id)
        if neighbour is None:
            continue
        edge = (
            LineageEdge(node_id, neighbour_id, via)
            if direction < 0
            else LineageEdge(neighbour_id, node_id, via)
        )
        # (node id, depth, via, canonical edge, direction)
        items.append((neighbour_id, level, via, edge, direction))
    items.reverse()
    return items


def _owners(
    ledger: Ledger,
    links: Sequence[tuple[str, str, str]],
    root_aliquot: str | None,
) -> dict[str, str]:
    """Aliquot owners the graph may need, in one batch.

    Only aliquots that are actually linked (or the resolved aliquot root) are
    asked about; stock counts are deliberately not read here because the
    traversal has not decided which stocks it will show.
    """
    owners: dict[str, str] = {}
    owner_count = 0
    link_ids = [aliquot_id for _child, aliquot_id, _via in links]
    if root_aliquot is not None:
        link_ids.append(root_aliquot)
    batch: list[tuple[str, tuple[str, ...]]] = []
    for sql, params in _aliquot_owner_queries(link_ids):
        batch.append((sql, params))
        owner_count += 1
    if not batch:
        return owners
    results = ledger.driver.query_many(batch)
    for index in range(owner_count):
        _collect_owners(results[index], owners)
    return owners


def _visited_counts(
    ledger: Ledger, nodes: Sequence[LineageNode]
) -> dict[str, tuple[int, int]]:
    """``{stock id: (in stock, total)}`` for the stocks the walk actually shows.

    One ``query_many`` for every visited stock kind (chunks of 500), instead of
    counting every stock in the ledger before the traversal.  An unvisited
    kind's missing view therefore cannot break a depth-0 or one-branch walk.
    """
    counts: dict[str, tuple[int, int]] = {}
    by_kind: dict[str, list[str]] = defaultdict(list)
    for node in nodes:
        if node.object == "stock":
            by_kind[node.kind].append(node.id)
    statements: list[tuple[str, tuple[str, ...]]] = []
    for kind_name, stock_ids in by_kind.items():
        statements.extend(_count_queries(kind_name, stock_ids))
    if not statements:
        return counts
    for rows in ledger.driver.query_many(statements):
        _collect_counts(rows, counts)
    return counts


def lineage(
    ledger: Ledger,
    ref: str,
    *,
    up: bool = True,
    down: bool = True,
    depth: int | None = None,
) -> LineageResult:
    """The lineage tree(s) around ``ref``, with ``--up``/``--down``/``--depth``.

    ``up`` walks ancestors and ``down`` descendants; with both set the result
    has one tree in each direction under the root (the root is printed once).
    ``depth`` counts edges from the root (``0`` prints only the root,
    ``None`` is unlimited).
    """
    if not up and not down:
        raise UsageError("tm lineage needs at least one direction")
    if depth is not None and depth < 0:
        raise UsageError("--depth must not be negative")

    # One batch for the graph and root identification, a second for the
    # graph-dependent ownership/counts reads; traversal itself is pure Python.
    snapshot = _load(ledger, ref)
    graph, links = _build(snapshot, ledger.schema)
    object_type, object_id = _resolve_ref(snapshot, ref)
    owners = _owners(
        ledger,
        links,
        object_id if object_type == "aliquot" else None,
    )
    _add_aliquots(graph, links, owners)
    root = _root_node(graph, object_type, object_id, owners)

    visited: dict[str, LineageNode] = {}
    ordered: list[LineageNode] = []
    kept: dict[tuple[str, str, str], LineageEdge] = {}
    lines: list[_Line] = [_Line(root.id, 0, 0, None, False)]
    visited[root.id] = root
    ordered.append(root)

    def walk(starts: Sequence[tuple[str, int, str, LineageEdge, int]]) -> None:
        stack = list(starts)
        while stack:
            node_id, level, via, edge, _direction = stack.pop()
            if depth is not None and level > depth:
                # --depth counts edges from the root (which is level 0): never
                # print a node that sits beyond the cut.
                continue
            kept.setdefault((edge.child, edge.parent, edge.via), edge)
            node = graph.nodes.get(node_id)
            if node is None:  # pragma: no cover - starts/edges filter these out
                continue
            if node.id in visited:
                lines.append(_Line(node.id, level, _direction, via, True))
                continue
            visited[node.id] = node
            ordered.append(node)
            lines.append(_Line(node.id, level, _direction, via, False))
            if depth is not None and level >= depth:
                continue
            further = graph.parents if _direction < 0 else graph.children
            stack.extend(
                _starts(
                    graph,
                    node.id,
                    further[node.id],
                    direction=_direction,
                    level=level + 1,
                )
            )

    if up:
        walk(
            _starts(graph, root.id, graph.parents[root.id], direction=-1, level=1)
        )
    if down:
        walk(
            _starts(graph, root.id, graph.children[root.id], direction=+1, level=1)
        )

    counts = _visited_counts(ledger, ordered)
    if counts:
        ordered = [
            LineageNode(
                id=node.id,
                object=node.object,
                kind=node.kind,
                label=node.label,
                stock_id=node.stock_id,
                counts=counts.get(node.id),
            )
            if node.object == "stock"
            else node
            for node in ordered
        ]
        root = ordered[0]
    return LineageResult(
        root=root,
        nodes=ordered,
        edges=list(kept.values()),
        lines=lines,
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _direction_arrow(direction: int) -> str:
    if direction < 0:
        return "↑"
    if direction > 0:
        return "↓"
    return ""


def node_text(node: LineageNode) -> str:
    """One node as the text tree prints it (spec §6)."""
    if node.object == "entity":
        name = f" ({node.label})" if node.label else ""
        return f"{node.kind} {node.id}{name}"
    if node.object == "stock":
        entity = f" of {node.label}" if node.label else ""
        counts = ""
        if node.counts is not None:
            counts = f" (aliquots {node.counts[0]}/{node.counts[1]})"
        return f"{node.kind} {node.id}{entity}{counts}"
    return f"aliquot {node.id} of stock {node.stock_id}"


def render_text(result: LineageResult) -> str:
    """The indented tree (``--format text``, the default)."""
    nodes = {node.id: node for node in result.nodes}
    output: list[str] = []
    for line in result.lines:
        node = nodes.get(line.node_id)
        if node is None:  # pragma: no cover - kept in sync by ``lineage``
            continue
        indent = "  " * line.depth
        if line.repeated:
            output.append(
                f"{indent}{_direction_arrow(line.direction)} {line.via} "
                f"{node.id} (see above)"
            )
        elif line.depth == 0:
            output.append(node_text(node))
        else:
            output.append(
                f"{indent}{_direction_arrow(line.direction)} {line.via} "
                f"{node_text(node)}"
            )
    return "\n".join(output)


def _dot_escape(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
    )


def _dot_key(node: LineageNode) -> str:
    """A Graphviz node id that cannot collide across object types."""
    return f"{node.object}:{node.id}"


def render_dot(result: LineageResult) -> str:
    """A Graphviz digraph (``--format dot``), edge labels are the ``via``."""
    nodes = {node.id: node for node in result.nodes}
    lines = ["digraph lineage {", "  node [shape=box];"]
    for node in result.nodes:
        # The root is bold so a rendered graph shows where the query started.
        style = ', style="bold"' if node.id == result.root.id else ""
        lines.append(
            f'  "{_dot_key(node)}" [label="{_dot_escape(node_text(node))}"{style}];'
        )
    for edge in result.edges:
        child = nodes.get(edge.child)
        parent = nodes.get(edge.parent)
        if child is None or parent is None:  # pragma: no cover - kept in sync
            continue
        lines.append(
            f'  "{_dot_key(child)}" -> "{_dot_key(parent)}" '
            f'[label="{_dot_escape(edge.via)}"];'
        )
    lines.append("}")
    return "\n".join(lines)
