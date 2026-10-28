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

The graph is built eagerly from the core tables plus one query per
``lineage: true`` field (only rows where the field is set).  Traversal is
lazy: it walks only the requested directions, stops at ``--depth`` and never
visits a node twice, so a cycle in hand-written data (a ``cell_line.parental``
loop) terminates instead of hanging.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterable, Sequence

from tetramark.errors import UsageError
from tetramark.ops.find import resolve_any
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
STOCK_COUNT_COLUMNS: tuple[str, str] = ("aliquots_stored", "aliquots_total")

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


def _aliquot_stocks(ledger: Ledger, aliquot_ids: Sequence[str]) -> dict[str, str]:
    """``{aliquot id: stock id}`` for the given aliquots (one query per chunk)."""
    owners: dict[str, str] = {}
    ids = list(dict.fromkeys(str(aliquot_id) for aliquot_id in aliquot_ids))
    for start in range(0, len(ids), _CHUNK):
        chunk = ids[start : start + _CHUNK]
        rows = ledger.driver.query(
            f"SELECT {_quote('id')}, {_quote('stock_id')} FROM {_quote('aliquot')} "
            f"WHERE {_quote('id')} IN ({_placeholders(len(chunk))})",
            tuple(chunk),
        )
        for row in rows:
            owners[str(row["id"])] = str(row["stock_id"])
    return owners


def _build(ledger: Ledger) -> _Graph:
    """Every entity, stock, referenced aliquot and their descent edges."""
    graph = _Graph()
    schema = ledger.schema

    entity_names: dict[str, Any] = {}
    for row in ledger.driver.query(
        f"SELECT {_quote('id')}, {_quote('kind')}, {_quote('name')} "
        f"FROM {_quote('entity')}"
    ):
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

    stock_rows = [
        dict(row)
        for row in ledger.driver.query(
            f"SELECT {_quote('id')}, {_quote('kind')}, {_quote('entity_id')}, "
            f"{_quote('parent_stock_id')}, {_quote('source_aliquot_id')} "
            f"FROM {_quote('stock')}"
        )
    ]
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
        rows = ledger.driver.query(
            f"SELECT {_quote('id')}, {select} FROM {_quote(table)} WHERE {where}"
        )
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

    owners = _aliquot_stocks(ledger, [aliquot_id for _child, aliquot_id, _via in links])
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
    return graph


# ---------------------------------------------------------------------------
# The count accessor ticket 25 changes
# ---------------------------------------------------------------------------


def stock_aliquot_counts(
    ledger: Ledger, kind: str, stock_ids: Iterable[str]
) -> dict[str, tuple[int, int]]:
    """``{stock id: (aliquots in stock, aliquots total)}`` for one stock kind.

    This is the **only** reader of the ``v_stock_<kind>`` count columns, and
    :data:`STOCK_COUNT_COLUMNS` is the only place that names them: when the
    stock views rename ``aliquots_stored`` to ``aliquots_in_stock`` (ticket
    25), change that tuple and nothing else in this module (or in the CLI).
    """
    ids = list(dict.fromkeys(str(stock_id) for stock_id in stock_ids))
    if not ids:
        return {}
    in_stock_column, total_column = STOCK_COUNT_COLUMNS
    view = _quote(f"v_stock_{kind}")
    counts: dict[str, tuple[int, int]] = {}
    for start in range(0, len(ids), _CHUNK):
        chunk = ids[start : start + _CHUNK]
        rows = ledger.driver.query(
            f"SELECT {_quote('id')}, {_quote(in_stock_column)} AS {_quote('in_stock')}, "
            f"{_quote(total_column)} AS {_quote('total')} FROM {view} "
            f"WHERE {_quote('id')} IN ({_placeholders(len(chunk))})",
            tuple(chunk),
        )
        for row in rows:
            counts[str(row["id"])] = (
                int(row.get("in_stock") or 0),
                int(row.get("total") or 0),
            )
    return counts


def _stock_counts(ledger: Ledger, nodes: Iterable[LineageNode]) -> dict[str, tuple[int, int]]:
    """Counts of the visited stock nodes, one view query per stock kind."""
    by_kind: dict[str, list[str]] = defaultdict(list)
    for node in nodes:
        if node.object == "stock":
            by_kind[node.kind].append(node.id)
    counts: dict[str, tuple[int, int]] = {}
    for kind_name, stock_ids in by_kind.items():
        counts.update(stock_aliquot_counts(ledger, kind_name, stock_ids))
    return counts


# ---------------------------------------------------------------------------
# Root resolution and traversal
# ---------------------------------------------------------------------------


def _root_node(ledger: Ledger, graph: _Graph, ref: str) -> LineageNode:
    """Resolve ``ID`` like ``tm find`` (id, then alias) to one lineage node."""
    resolved = resolve_any(ledger, ref)
    object_type = str(resolved["object"])
    object_id = str(resolved["id"])
    if object_type == "box":
        raise UsageError(
            f"box {object_id!r} has no lineage: the graph nodes are entities, "
            "stocks and aliquots"
        )
    node = graph.nodes.get(object_id)
    if node is not None:
        return node
    if object_type == "aliquot":
        owners = _aliquot_stocks(ledger, [object_id])
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

    graph = _build(ledger)
    root = _root_node(ledger, graph, ref)

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

    counts = _stock_counts(ledger, ordered)
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
