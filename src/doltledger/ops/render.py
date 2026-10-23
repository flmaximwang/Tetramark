"""Read-side rendering: ``box show`` (spec §6).

A box is shown either as a grid (``grid`` geometry), as a slot list
(``named`` geometry) or as a contents list (``slotless`` geometry).  The
renderer is split in two:

* :func:`box_view` reads the box and returns a plain dictionary, the shape the
  CLI prints for ``--json``/``--format csv`` and the tests snapshot;
* :func:`render_box` turns that dictionary into a ``rich`` renderable (status
  colouring included) and :func:`render_box_text` renders it without colour
  for snapshots.

The dictionary shape is ``{box, kind, geometry, path, slots}``; a slotless box
has no slots, so its contents are returned as ``occupants`` instead.
"""

from __future__ import annotations

import io
from typing import TYPE_CHECKING, Any

from rich.console import Console, Group
from rich.table import Table
from rich.text import Text

from doltledger.core.objects import _box_paths, _resolve_box
from doltledger.schema.model import GeometryType, Kind

if TYPE_CHECKING:  # pragma: no cover - import only for type checkers
    from doltledger.core.ledger import Ledger

__all__ = [
    "STATUS_STYLES",
    "box_csv_rows",
    "box_view",
    "render_box",
    "render_box_text",
]

#: Colour assigned to each aliquot status in the grid/list renderings.
STATUS_STYLES: dict[str, str] = {
    "stored": "green",
    "pending": "yellow",
    "missing": "red",
    "consumed": "dim",
}


def box_view(ledger: Ledger, ref: str) -> dict[str, Any]:
    """Read one box and return its renderable shape.

    The box is resolved by human name first, then id, then alias.  Slotted
    boxes list every slot (empty ones included) in ordinal order; a slotless
    box has ``slots: []`` and an ``occupants`` list of its direct aliquots and
    child boxes.
    """
    box_row = _resolve_box(ledger.driver, ref)
    box_id = str(box_row["id"])
    kind_name = str(box_row["kind"])
    kind = ledger.schema.kind(kind_name)
    geometry = kind.geometry
    if geometry is None:  # pragma: no cover - box kinds always have one
        raise ValueError(f"box kind {kind_name!r} has no geometry")

    paths = _box_paths(
        ledger.driver.query("SELECT `id`, `name`, `parent_box_id` FROM `box`")
    )

    view: dict[str, Any] = {
        "box": str(box_row["name"]),
        "kind": kind_name,
        "geometry": _geometry_json(kind),
        "path": paths.get(box_id, str(box_row["name"])),
        "slots": [],
    }

    if geometry.is_slotted:
        view["slots"] = _slots(ledger, box_id)
    else:
        view["occupants"] = _slotless_occupants(ledger, box_id)
    return view


def _geometry_json(kind: Kind) -> dict[str, Any]:
    geometry = kind.geometry
    assert geometry is not None  # caller checked
    if geometry.type is GeometryType.grid:
        return {
            "type": "grid",
            "rows": geometry.row_labels,
            "cols": geometry.col_labels,
            "slot_capacity": kind.slot_capacity,
        }
    if geometry.type is GeometryType.named:
        return {
            "type": "named",
            "slots": geometry.labels,
            "slot_capacity": kind.slot_capacity,
        }
    return {"type": "slotless", "capacity": geometry.capacity}


def _slots(ledger: Ledger, box_id: str) -> list[dict[str, Any]]:
    """Every slot of a slotted box, in ordinal order, with its occupants."""
    rows = ledger.driver.query(
        "SELECT * FROM `v_box_slots` WHERE `box_id` = %s ORDER BY `ordinal`",
        (box_id,),
    )
    slots: list[dict[str, Any]] = []
    by_label: dict[str, dict[str, Any]] = {}
    for row in rows:
        label = str(row["slot"])
        slot = by_label.get(label)
        if slot is None:
            slot = {
                "label": label,
                "row": row.get("row_label"),
                "col": row.get("col_label"),
                "occupants": [],
            }
            by_label[label] = slot
            slots.append(slot)
        occupant_type = row.get("occupant_type")
        if occupant_type == "aliquot":
            slot["occupants"].append(
                {
                    "type": "aliquot",
                    "id": str(row["aliquot_id"]),
                    "stock_id": str(row["stock_id"]),
                    "entity_id": (
                        None if row.get("entity_id") is None else str(row["entity_id"])
                    ),
                    "status": row.get("status"),
                }
            )
        elif occupant_type == "box":
            slot["occupants"].append(
                {
                    "type": "box",
                    "id": str(row["child_box_id"]),
                    "name": row.get("child_box_name"),
                    "status": None,
                }
            )
    return slots


def _slotless_occupants(ledger: Ledger, box_id: str) -> list[dict[str, Any]]:
    """Direct aliquots and child boxes of a slotless box, in a stable order."""
    occupants: list[dict[str, Any]] = []
    aliquots = ledger.driver.query(
        "SELECT a.*, s.`entity_id` AS `entity_id` FROM `aliquot` a "
        "JOIN `stock` s ON s.`id` = a.`stock_id` "
        "WHERE a.`box_id` = %s AND a.`slot` IS NULL ORDER BY a.`id`",
        (box_id,),
    )
    for row in aliquots:
        occupants.append(
            {
                "type": "aliquot",
                "id": str(row["id"]),
                "stock_id": str(row["stock_id"]),
                "entity_id": (
                    None if row.get("entity_id") is None else str(row["entity_id"])
                ),
                "status": row.get("status"),
                "quantity": row.get("quantity"),
                "unit": row.get("unit"),
            }
        )
    children = ledger.driver.query(
        "SELECT `id`, `name` FROM `box` "
        "WHERE `parent_box_id` = %s AND `parent_slot` IS NULL ORDER BY `name`",
        (box_id,),
    )
    for row in children:
        occupants.append(
            {
                "type": "box",
                "id": str(row["id"]),
                "name": row.get("name"),
                "status": None,
            }
        )
    return occupants


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_box(view: dict[str, Any]) -> Group:
    """A rich renderable for one :func:`box_view` dictionary."""
    header = Text()
    header.append(str(view["box"]), style="bold")
    header.append(f"  {view['kind']}  path: {view['path']}", style="dim")

    geometry = view.get("geometry") or {}
    if geometry.get("type") == "slotless":
        summary = Text(_slotless_summary(view, geometry), style="dim")
        table = _slotless_table(view)
    else:
        summary = Text(_slot_summary(view, geometry), style="dim")
        table = _slot_table(view, geometry)
    return Group(header, summary, table)


def render_box_text(view: dict[str, Any], *, width: int = 120) -> str:
    """Render a box without colour, for snapshots and plain terminals."""
    buffer = io.StringIO()
    console = Console(
        file=buffer,
        width=width,
        color_system=None,
        force_terminal=False,
        highlight=False,
        legacy_windows=False,
    )
    console.print(render_box(view))
    return buffer.getvalue().rstrip("\n")


def _slot_summary(view: dict[str, Any], geometry: dict[str, Any]) -> str:
    slots = view.get("slots") or []
    used = sum(1 for slot in slots if slot["occupants"])
    capacity = geometry.get("slot_capacity", 1)
    suffix = f", slot capacity {capacity}" if capacity > 1 else ""
    return f"{used} of {len(slots)} slots used{suffix}"


def _slotless_summary(view: dict[str, Any], geometry: dict[str, Any]) -> str:
    occupants = view.get("occupants") or []
    capacity = geometry.get("capacity")
    suffix = f" (capacity {capacity})" if capacity is not None else ""
    return f"{len(occupants)} occupant(s){suffix}"


def _slot_table(view: dict[str, Any], geometry: dict[str, Any]) -> Table:
    geometry_type = geometry.get("type")
    if geometry_type == "grid":
        return _grid_table(view, geometry)
    return _named_table(view)


def _grid_table(view: dict[str, Any], geometry: dict[str, Any]) -> Table:
    rows: list[str] = list(geometry.get("rows") or [])
    cols: list[str] = list(geometry.get("cols") or [])
    table = Table(
        box=None,
        show_header=True,
        header_style="bold",
        pad_edge=False,
    )
    table.add_column("")
    for col in cols:
        table.add_column(str(col), justify="center")
    by_cell = {
        (slot["row"], slot["col"]): slot
        for slot in view.get("slots") or []
        if slot.get("row") is not None and slot.get("col") is not None
    }
    for row in rows:
        cells: list[Text] = []
        for col in cols:
            slot = by_cell.get((row, col))
            cells.append(_occupant_text(slot["occupants"] if slot else []))
        table.add_row(Text(str(row), style="bold"), *cells)
    return table


def _named_table(view: dict[str, Any]) -> Table:
    table = Table(
        box=None,
        show_header=True,
        header_style="bold",
        pad_edge=False,
    )
    table.add_column("slot")
    table.add_column("occupants")
    for slot in view.get("slots") or []:
        table.add_row(str(slot["label"]), _occupant_text(slot["occupants"]))
    return table


def _slotless_table(view: dict[str, Any]) -> Table:
    table = Table(
        box=None,
        show_header=True,
        header_style="bold",
        pad_edge=False,
    )
    table.add_column("type")
    table.add_column("id")
    table.add_column("status")
    occupants = view.get("occupants") or []
    if not occupants:
        table.add_row("(empty)", "", "")
        return table
    for occupant in occupants:
        status = occupant.get("status")
        table.add_row(
            str(occupant["type"]),
            _occupant_label(occupant),
            Text("" if status is None else str(status), style=_status_style(status)),
        )
    return table


def _occupant_text(occupants: list[dict[str, Any]]) -> Text:
    """``id1,id2`` for a slot, each occupant coloured by its status."""
    text = Text()
    for index, occupant in enumerate(occupants):
        if index:
            text.append(",")
        text.append(
            _occupant_label(occupant),
            style=_status_style(occupant.get("status")),
        )
    return text


def _occupant_label(occupant: dict[str, Any]) -> str:
    if occupant.get("type") == "box":
        return str(occupant.get("name") or occupant.get("id"))
    return str(occupant.get("id"))


def _status_style(status: str | None) -> str | None:
    if status is None:
        return "cyan"
    return STATUS_STYLES.get(status, "cyan")


# ---------------------------------------------------------------------------
# CSV rows
# ---------------------------------------------------------------------------


def box_csv_rows(view: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten a box view into CSV rows: one row per slot/occupant."""
    geometry_type = (view.get("geometry") or {}).get("type")
    if geometry_type == "slotless":
        return [
            {
                "slot": "",
                "row": "",
                "col": "",
                "occupants": _csv_occupants([occupant]),
            }
            for occupant in view.get("occupants") or []
        ]
    rows: list[dict[str, Any]] = []
    for slot in view.get("slots") or []:
        rows.append(
            {
                "slot": slot["label"],
                "row": "" if slot.get("row") is None else slot["row"],
                "col": "" if slot.get("col") is None else slot["col"],
                "occupants": _csv_occupants(slot["occupants"]),
            }
        )
    return rows


def _csv_occupants(occupants: list[dict[str, Any]]) -> str:
    return ",".join(_occupant_label(occupant) for occupant in occupants)
