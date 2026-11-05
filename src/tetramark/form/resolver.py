"""The ledger-backed implementation of the form's :class:`Resolver` protocol.

Reads and writes go through the same core helpers as the ``add`` commands
(``_resolve_ref`` / ``_resolve_box`` / the generated ``v_box_slots`` view), so
the form never invents a second way to look something up.  Everything here is
read-only until :mod:`tetramark.form.session` confirms the write.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from tetramark.core.objects import _resolve_box, _resolve_ref
from tetramark.form.source import Choice
from tetramark.form.state import FormError
from tetramark.errors import LedgerError
from tetramark.schema.ddl import table_name

if TYPE_CHECKING:  # pragma: no cover - import only for type checkers
    from tetramark.core.ledger import Ledger

__all__ = ["LedgerResolver"]

#: How many rows ``?`` (no search term) and Tab completion list at most.
LIST_LIMIT = 25


def _quote(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


class LedgerResolver:
    """Refs, search, boxes and free slots on an open ledger."""

    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger
        self.schema = ledger.schema
        #: Box ref -> slotted, for this form session.  The form asks for every
        #: step it renders, and each lookup is a ``dolt sql`` process on the
        #: embedded driver; a box's kind cannot change while the form only
        #: reads (the final ``add`` resolves the box again before writing).
        self._slotted: dict[str, bool] = {}

    @property
    def driver(self) -> Any:
        return self.ledger.driver

    # -- refs --------------------------------------------------------------
    def _target_kind(self, target: str, object_id: str) -> tuple[str, str | None]:
        kind = self.schema.kinds.get(target)
        if kind is not None:
            return kind.object.value, target
        object_type = target
        if object_type == "aliquot":
            rows = self.driver.query(
                "SELECT s.`kind` AS `kind` FROM `aliquot` a "
                "JOIN `stock` s ON s.`id` = a.`stock_id` WHERE a.`id` = %s",
                (object_id,),
            )
        else:
            rows = self.driver.query(
                f"SELECT {_quote('kind')} FROM {_quote(object_type)} "
                f"WHERE {_quote('id')} = %s",
                (object_id,),
            )
        return object_type, (str(rows[0]["kind"]) if rows else None)

    def _label(self, target: str, object_id: str) -> str:
        kind = self.schema.kinds.get(target)
        object_type = kind.object.value if kind is not None else target
        if object_type in ("entity", "box"):
            rows = self.driver.query(
                f"SELECT {_quote('name')} FROM {_quote(object_type)} "
                f"WHERE {_quote('id')} = %s",
                (object_id,),
            )
            if rows and rows[0]["name"]:
                return str(rows[0]["name"])
        return str(object_id)

    def resolve_ref(
        self, target: str, text: str, *, allowed_kinds: tuple[str, ...] = ()
    ) -> tuple[str, str]:
        try:
            object_id = _resolve_ref(self.driver, self.ledger, target, text)
        except LedgerError as exc:
            raise FormError(str(exc)) from None
        object_type, kind_name = self._target_kind(target, object_id)
        if allowed_kinds and kind_name not in allowed_kinds:
            raise FormError(
                f"{text!r} is a {kind_name!r} {object_type}; this step accepts: "
                + ", ".join(allowed_kinds)
            )
        return str(object_id), self._label(target, object_id)

    def search(
        self, target: str, term: str, *, allowed_kinds: tuple[str, ...] = ()
    ) -> list[Choice]:
        try:
            hits = self.ledger.find(term)
        except LedgerError:
            return []
        kind = self.schema.kinds.get(target)
        object_type = kind.object.value if kind is not None else target
        results: list[Choice] = []
        for hit in hits:
            if hit.get("object") != object_type:
                continue
            hit_kind = hit.get("kind")
            if kind is not None and hit_kind != target:
                continue
            if allowed_kinds and hit_kind not in allowed_kinds:
                continue
            results.append(
                Choice(
                    value=str(hit["id"]),
                    label=str(hit.get("summary") or hit["id"]),
                    detail=str(hit.get("location") or ""),
                )
            )
        return results

    def list_refs(
        self, target: str, *, allowed_kinds: tuple[str, ...] = ()
    ) -> list[Choice]:
        kind = self.schema.kinds.get(target)
        object_type = kind.object.value if kind is not None else target
        if object_type in ("entity", "box"):
            table = (
                table_name(kind.object.value, target) if kind is not None else target
            )
            columns = (
                f"{_quote('id')}, {_quote('name')}"
                if kind is None or object_type == "box"
                else f"{_quote('id')}"
            )
            if kind is not None and object_type == "entity":
                columns = f"e.{_quote('id')}, e.{_quote('name')}"
                sql = (
                    f"SELECT {columns} FROM {_quote(target)} k "
                    f"JOIN {_quote('entity')} e ON e.{_quote('id')} = k.{_quote('id')} "
                    f"WHERE e.{_quote('kind')} = %s ORDER BY e.{_quote('name')} "
                    f"LIMIT {LIST_LIMIT}"
                )
                rows = self.driver.query(sql, (target,))
            else:
                sql = (
                    f"SELECT {columns} FROM {_quote(table)} ORDER BY "
                    f"{_quote('name')} LIMIT {LIST_LIMIT}"
                )
                rows = self.driver.query(sql)
        elif object_type == "aliquot":
            rows = self.driver.query(
                f"SELECT {_quote('id')}, NULL AS name FROM {_quote('aliquot')} "
                f"ORDER BY {_quote('id')} LIMIT {LIST_LIMIT}"
            )
        else:
            table = (
                table_name(kind.object.value, target) if kind is not None else target
            )
            rows = self.driver.query(
                f"SELECT s.{_quote('id')}, e.{_quote('name')} AS name "
                f"FROM {_quote(table)} k "
                f"JOIN {_quote('stock')} s ON s.{_quote('id')} = k.{_quote('id')} "
                f"JOIN {_quote('entity')} e ON e.{_quote('id')} = s.{_quote('entity_id')} "
                f"ORDER BY s.{_quote('id')} LIMIT {LIST_LIMIT}"
            )
        choices: list[Choice] = []
        for row in rows:
            object_id = str(row["id"])
            name = row.get("name")
            if allowed_kinds:
                _, row_kind = self._target_kind(target, object_id)
                if row_kind not in allowed_kinds:
                    continue
            label = f"{name} ({object_id})" if name else object_id
            choices.append(Choice(value=object_id, label=label))
        return choices

    def ref_tokens(
        self, target: str, *, allowed_kinds: tuple[str, ...] = ()
    ) -> list[str]:
        tokens = [choice.value for choice in self.list_refs(
            target, allowed_kinds=allowed_kinds
        )]
        kind = self.schema.kinds.get(target)
        object_type = kind.object.value if kind is not None else target
        rows = self.driver.query(
            f"SELECT {_quote('alias')} FROM {_quote('alias')} "
            f"WHERE {_quote('object_type')} = %s ORDER BY {_quote('alias')} LIMIT 200",
            (object_type,),
        )
        tokens.extend(str(row["alias"]) for row in rows)
        # Keep order, drop duplicates.
        return list(dict.fromkeys(tokens))

    # -- boxes and slots ---------------------------------------------------
    def _box_row(self, ref: str) -> dict[str, Any]:
        try:
            return dict(_resolve_box(self.driver, ref))
        except LedgerError as exc:
            raise FormError(str(exc)) from None

    def resolve_box(self, text: str) -> tuple[str, str]:
        row = self._box_row(text)
        name = str(row["name"])
        return name, f"{name} ({row['kind']})"

    def list_boxes(self, *, parents_only: bool = False) -> list[Choice]:
        rows = self.driver.query(
            f"SELECT {_quote('name')}, {_quote('kind')} FROM {_quote('box')} "
            f"ORDER BY {_quote('name')}"
        )
        choices: list[Choice] = []
        for row in rows:
            name = str(row["name"])
            kind_name = str(row["kind"])
            kind = self.schema.kind(kind_name)
            geometry = kind.geometry
            if parents_only and geometry is not None and geometry.type.value == "grid":
                continue
            detail = "slotted" if geometry is not None and geometry.is_slotted else "slotless"
            choices.append(
                Choice(value=name, label=f"{name} ({kind_name})", detail=detail)
            )
        return choices

    def is_slotted(self, box: str) -> bool:
        if box not in self._slotted:
            row = self._box_row(box)
            kind = self.schema.kind(str(row["kind"]))
            geometry = kind.geometry
            self._slotted[box] = bool(geometry is not None and geometry.is_slotted)
        return self._slotted[box]

    def free_slots(self, box: str) -> list[str]:
        row = self._box_row(box)
        rows = self.driver.query(
            "SELECT `slot` FROM `v_box_slots` WHERE `box_name` = %s "
            "AND `occupant_type` IS NULL ORDER BY `ordinal`",
            (str(row["name"]),),
        )
        return [str(row["slot"]) for row in rows]
