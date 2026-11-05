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
        #: Box ref -> box row, and box name -> free slots, for this form
        #: session.  The form re-renders its steps after every answer and each
        #: lookup is a ``dolt sql`` process on the embedded driver.  Nothing
        #: is written until the final ``add``, which resolves the box and
        #: checks the slots again before writing, so a stale answer can only
        #: become a rejection shown on the form -- never a wrong write.
        self._boxes: dict[str, dict[str, Any]] = {}
        self._free: dict[str, list[str]] = {}

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

    def _list_statement(self, target: str) -> tuple[str, tuple[Any, ...]]:
        """The completion list for ``target``, each row with its ``kind``.

        The kind comes from the same query, so filtering by ``allowed_kinds``
        never costs one lookup per row (on the embedded driver that was one
        ``dolt sql`` process per listed object before the next prompt).
        """
        kind = self.schema.kinds.get(target)
        object_type = kind.object.value if kind is not None else target
        if object_type == "entity" and kind is not None:
            return (
                f"SELECT e.{_quote('id')}, e.{_quote('name')}, e.{_quote('kind')} "
                f"FROM {_quote(target)} k "
                f"JOIN {_quote('entity')} e ON e.{_quote('id')} = k.{_quote('id')} "
                f"WHERE e.{_quote('kind')} = %s ORDER BY e.{_quote('name')} "
                f"LIMIT {LIST_LIMIT}",
                (target,),
            )
        if object_type in ("entity", "box"):
            table = table_name(kind.object.value, target) if kind is not None else target
            columns = (
                f"{_quote('id')}, {_quote('name')}"
                if kind is None or object_type == "box"
                else f"{_quote('id')}"
            )
            kind_column = f", {_quote('kind')}" if kind is None else ""
            return (
                f"SELECT {columns}{kind_column} FROM {_quote(table)} ORDER BY "
                f"{_quote('name')} LIMIT {LIST_LIMIT}",
                (),
            )
        if object_type == "aliquot":
            return (
                f"SELECT a.{_quote('id')}, NULL AS name, s.{_quote('kind')} AS kind "
                f"FROM {_quote('aliquot')} a "
                f"JOIN {_quote('stock')} s ON s.{_quote('id')} = a.{_quote('stock_id')} "
                f"ORDER BY a.{_quote('id')} LIMIT {LIST_LIMIT}",
                (),
            )
        table = table_name(kind.object.value, target) if kind is not None else target
        return (
            f"SELECT s.{_quote('id')}, e.{_quote('name')} AS name, "
            f"s.{_quote('kind')} AS kind "
            f"FROM {_quote(table)} k "
            f"JOIN {_quote('stock')} s ON s.{_quote('id')} = k.{_quote('id')} "
            f"JOIN {_quote('entity')} e ON e.{_quote('id')} = s.{_quote('entity_id')} "
            f"ORDER BY s.{_quote('id')} LIMIT {LIST_LIMIT}",
            (),
        )

    def _choices(
        self,
        target: str,
        rows: list[dict[str, Any]],
        allowed_kinds: tuple[str, ...],
    ) -> list[Choice]:
        kind = self.schema.kinds.get(target)
        choices: list[Choice] = []
        for row in rows:
            object_id = str(row["id"])
            name = row.get("name")
            if allowed_kinds:
                row_kind = target if kind is not None else row.get("kind")
                if row_kind not in allowed_kinds:
                    continue
            label = f"{name} ({object_id})" if name else object_id
            choices.append(Choice(value=object_id, label=label))
        return choices

    def list_refs(
        self, target: str, *, allowed_kinds: tuple[str, ...] = ()
    ) -> list[Choice]:
        sql, params = self._list_statement(target)
        return self._choices(target, self.driver.query(sql, params), allowed_kinds)

    def ref_tokens(
        self, target: str, *, allowed_kinds: tuple[str, ...] = ()
    ) -> list[str]:
        kind = self.schema.kinds.get(target)
        object_type = kind.object.value if kind is not None else target
        # The listing and the aliases are one read batch (one process).
        listed, aliases = self.driver.query_many(
            [
                self._list_statement(target),
                (
                    f"SELECT {_quote('alias')} FROM {_quote('alias')} "
                    f"WHERE {_quote('object_type')} = %s "
                    f"ORDER BY {_quote('alias')} LIMIT 200",
                    (object_type,),
                ),
            ]
        )
        tokens = [
            choice.value for choice in self._choices(target, listed, allowed_kinds)
        ]
        tokens.extend(str(row["alias"]) for row in aliases)
        # Keep order, drop duplicates.
        return list(dict.fromkeys(tokens))

    # -- boxes and slots ---------------------------------------------------
    def _box_row(self, ref: str) -> dict[str, Any]:
        if ref not in self._boxes:
            try:
                self._boxes[ref] = dict(_resolve_box(self.driver, ref))
            except LedgerError as exc:
                raise FormError(str(exc)) from None
        return dict(self._boxes[ref])

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

    def forget(self) -> None:
        """Drop the session caches (the core rejected the write)."""
        self._boxes.clear()
        self._free.clear()

    def is_slotted(self, box: str) -> bool:
        row = self._box_row(box)
        kind = self.schema.kind(str(row["kind"]))
        geometry = kind.geometry
        return bool(geometry is not None and geometry.is_slotted)

    def free_slots(self, box: str) -> list[str]:
        name = str(self._box_row(box)["name"])
        if name not in self._free:
            rows = self.driver.query(
                "SELECT `slot` FROM `v_box_slots` WHERE `box_name` = %s "
                "AND `occupant_type` IS NULL ORDER BY `ordinal`",
                (name,),
            )
            self._free[name] = [str(row["slot"]) for row in rows]
        return list(self._free[name])
