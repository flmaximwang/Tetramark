"""The shared ``*`` relation lines of ``stock show`` / ``aliquot show``.

Pure formatting: no ledger, no Dolt.  The record shapes these lines read are
built by :mod:`tetramark.core.objects`; the real-Dolt behaviour is asserted in
``tests/test_show_related.py``.
"""

from __future__ import annotations

from tetramark.cli.related import (
    RELATED_LABEL_WIDTH,
    related_lines,
    without_related,
)

ENTITY_ID = "e53b45c5-ccf4-4deb-b1de-d307846094c0"
PARENT_ID = "745b8daa-f934-4d6e-98fd-940de7c1828d"
STOCK_ID = "7726L602"
SOURCE_ID = "7726L602.1"


def _entity(name: str | None = "pUC19", *, kind: str = "plasmid") -> dict:
    return {"id": ENTITY_ID, "kind": kind, "name": name}


def test_the_label_column_is_the_shared_spec_column() -> None:
    """Every label is padded to the same width, so values start at column 18."""
    lines = related_lines(
        {
            "entity": _entity(),
            "parent_stock": {
                "id": PARENT_ID,
                "kind": "bacterial_glycerol",
                "entity_id": ENTITY_ID,
                "entity_name": "pUC19",
                "entity_missing": False,
            },
            "source_aliquot": {
                "id": SOURCE_ID,
                "seq": 1,
                "status": "stored",
                "stock_kind": "bacterial_glycerol",
                "entity_id": ENTITY_ID,
                "entity_name": "pUC19",
                "entity_missing": False,
            },
        }
    )
    assert lines == [
        "* entity          plasmid · pUC19",
        f"* parent stock    bacterial_glycerol · {PARENT_ID[:8]} (pUC19)",
        f"* source aliquot  {SOURCE_ID} · stored · bacterial_glycerol of pUC19",
    ]
    for line in lines:
        assert line.startswith("* ")
        assert len(line) > 2 + RELATED_LABEL_WIDTH + 1
        assert line[2 + RELATED_LABEL_WIDTH : 2 + RELATED_LABEL_WIDTH + 2] == "  "


def test_a_null_relation_omits_its_line_and_the_stock_entity_always_prints() -> None:
    """``parent_stock``/``source_aliquot`` may be null; ``entity`` may not."""
    assert related_lines({"entity": _entity(), "parent_stock": None}) == [
        "* entity          plasmid · pUC19"
    ]
    assert related_lines({"entity": _entity(), "source_aliquot": None}) == [
        "* entity          plasmid · pUC19"
    ]
    assert related_lines(None) == []
    assert related_lines({}) == []


def test_an_unnamed_entity_keeps_its_kind_and_names_the_id() -> None:
    """An existing entity without a name: the id says which one it is."""
    assert related_lines({"entity": _entity(None)}) == [
        f"* entity          plasmid · {ENTITY_ID[:8]} (unnamed)"
    ]
    assert related_lines({"entity": _entity("")}) == [
        f"* entity          plasmid · {ENTITY_ID[:8]} (unnamed)"
    ]


def test_a_missing_entity_prints_its_short_id() -> None:
    """An id whose entity row cannot be read: ``<short id> (missing)``."""
    assert related_lines({"entity": {"id": ENTITY_ID, "missing": True}}) == [
        f"* entity          {ENTITY_ID[:8]} (missing)"
    ]


def test_unnamed_and_missing_entities_of_a_related_object_mark_only_the_entity() -> None:
    """A parent stock / source aliquot keeps its own details when its entity is absent."""
    unnamed_parent = {
        "id": PARENT_ID,
        "kind": "bacterial_glycerol",
        "entity_id": ENTITY_ID,
        "entity_name": None,
        "entity_missing": False,
    }
    missing_entity_parent = {
        "id": PARENT_ID,
        "kind": "bacterial_glycerol",
        "entity_id": ENTITY_ID,
        "entity_name": None,
        "entity_missing": True,
    }
    lines = related_lines({"parent_stock": unnamed_parent})
    assert lines == [
        f"* parent stock    bacterial_glycerol · {PARENT_ID[:8]} "
        f"(unnamed entity {ENTITY_ID[:8]})"
    ]
    lines = related_lines({"parent_stock": missing_entity_parent})
    assert lines == [
        f"* parent stock    bacterial_glycerol · {PARENT_ID[:8]} "
        f"(missing entity {ENTITY_ID[:8]})"
    ]

    source = {
        "id": SOURCE_ID,
        "seq": 1,
        "status": "stored",
        "stock_kind": "bacterial_glycerol",
        "entity_id": ENTITY_ID,
        "entity_name": None,
        "entity_missing": True,
    }
    assert related_lines({"source_aliquot": source}) == [
        f"* source aliquot  {SOURCE_ID} · stored · bacterial_glycerol of "
        f"missing entity {ENTITY_ID[:8]}"
    ]


def test_a_missing_parent_stock_or_source_aliquot_prints_its_own_id() -> None:
    """The referenced row itself is gone: the line names it, nothing else."""
    assert related_lines({"parent_stock": {"id": PARENT_ID, "missing": True}}) == [
        f"* parent stock    {PARENT_ID[:8]} (missing)"
    ]
    assert related_lines(
        {"source_aliquot": {"id": SOURCE_ID, "missing": True}}
    ) == [f"* source aliquot  {SOURCE_ID} (missing)"]


def test_control_characters_become_the_literal_separator() -> None:
    """A name cannot split the one-line relation (spec §6: literal `` | ``)."""
    assert related_lines(
        {"entity": _entity("pUC19\nmutant\t2")}
    ) == ["* entity          plasmid · pUC19 | mutant | 2"]
    # A run of controls is one separator, not one per character.
    assert related_lines(
        {"entity": _entity("line\r\none")}
    ) == ["* entity          plasmid · line | one"]


def test_non_uuid_ids_are_kept_whole() -> None:
    """Datecode stock ids stay readable; only uuid ids are shortened."""
    assert related_lines(
        {
            "entity": {"id": STOCK_ID, "kind": "plasmid", "name": "pUC19"},
            "parent_stock": {
                "id": STOCK_ID,
                "kind": "bacterial_glycerol",
                "entity_id": STOCK_ID,
                "entity_name": "pUC19",
                "entity_missing": False,
            },
        }
    ) == [
        "* entity          plasmid · pUC19",
        f"* parent stock    bacterial_glycerol · {STOCK_ID} (pUC19)",
    ]


def test_csv_drops_the_related_key_and_nothing_else() -> None:
    """``--format csv`` keeps exactly the columns it had before."""
    record = {"id": STOCK_ID, "related": {"entity": _entity()}, "kind": "lot"}
    assert without_related(record) == {"id": STOCK_ID, "kind": "lot"}
    assert without_related({"id": STOCK_ID}) == {"id": STOCK_ID}
