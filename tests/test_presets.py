"""Bundled presets (ticket 07): loading, init, rules and the library walkthrough.

Like the rest of the suite these tests run against a real ``dolt`` binary and a
workspace whose Dolt/Git configuration lives under ``tmp_path``.
"""

from __future__ import annotations

import json

import pytest

from tetramark.schema.ddl import slot_rows
from tetramark.schema.loader import list_bundled_presets, load_schema
from tetramark.schema.model import (
    FieldType,
    GeometryType,
    LedgerConfig,
    ObjectType,
)

BUNDLED = ("base", "molbio", "library")

EXPECTED_KINDS = {
    "base": {
        "item",
        "lot",
        "grid9x9",
        "grid96",
        "rack",
        "shelf",
        "freezer",
        "drawer",
    },
    "molbio": {
        "item",
        "lot",
        "grid9x9",
        "grid96",
        "rack",
        "shelf",
        "freezer",
        "drawer",
        "construct",
        "plasmid",
        "strain",
        "bacterial",
        "plasmid_prep",
        "protein_prep",
        "pellet",
    },
    "library": {
        "item",
        "lot",
        "grid9x9",
        "grid96",
        "rack",
        "shelf",
        "freezer",
        "drawer",
        "book",
        "copy",
    },
}


def load_bundled(name: str):
    return load_schema(LedgerConfig(name="preset_test", presets=[name]))


def commit_count(ledger) -> int:
    return int(ledger.driver.query("SELECT COUNT(*) AS n FROM dolt_log")[0]["n"])


def table_names(ledger) -> set[str]:
    rows = ledger.driver.query(
        "SELECT table_name AS name FROM information_schema.tables "
        "WHERE table_schema = DATABASE()"
    )
    return {str(row["name"]) for row in rows}


# ---------------------------------------------------------------------------
# Discovery and shape
# ---------------------------------------------------------------------------


def test_all_three_bundled_presets_are_discoverable() -> None:
    assert list_bundled_presets() == ["base", "library", "molbio"]


@pytest.mark.parametrize("name", BUNDLED)
def test_preset_loads_with_expected_kinds_and_rules(name: str) -> None:
    schema = load_bundled(name)

    assert set(schema.kinds) == EXPECTED_KINDS[name]
    assert schema.preset_versions[name] == 1
    # molbio and library both include base.
    if name != "base":
        assert schema.preset_versions["base"] == 1

    own_prefix = {"base": "BASE", "molbio": "MB", "library": "LIB"}[name]
    own_rules = [rule for rule in schema.rules if rule.id.startswith(own_prefix)]
    assert 2 <= len(own_rules) <= 4
    assert {rule.severity.value for rule in own_rules} == {"fail", "info"}
    assert all(rule.sql.strip().upper().startswith("SELECT") for rule in schema.rules)


@pytest.mark.parametrize("name", BUNDLED)
def test_every_kind_and_field_has_a_description(name: str) -> None:
    schema = load_bundled(name)
    for kind_name, kind in schema.kinds.items():
        assert kind.description and kind.description.strip(), kind_name
        for scope in (kind.fields, kind.aliquot_fields):
            for field_name, field in scope.items():
                assert field.description and field.description.strip(), (
                    kind_name,
                    field_name,
                )


def test_base_geometry_and_fields() -> None:
    schema = load_bundled("base")

    item = schema.kinds["item"]
    assert item.object is ObjectType.entity
    assert set(item.fields) == {"name", "description", "category"}
    assert item.fields["name"].required and item.fields["name"].unique

    lot = schema.kinds["lot"]
    assert lot.object is ObjectType.stock
    assert lot.entity_kinds == ["item"]
    assert set(lot.fields) == {
        "supplier",
        "catalog_no",
        "received_on",
        "expires_on",
    }
    assert lot.fields["supplier"].required

    grid9 = schema.kinds["grid9x9"].geometry
    assert grid9 is not None and grid9.type is GeometryType.grid
    assert grid9.row_labels == list("ABCDEFGHJ")
    assert grid9.col_labels == [str(number) for number in range(1, 10)]
    assert len(grid9.labels) == 81
    assert grid9.labels[0] == "A1" and grid9.labels[-1] == "J9"

    grid96 = schema.kinds["grid96"].geometry
    assert grid96 is not None and len(grid96.labels) == 96
    assert grid96.labels[0] == "A1" and grid96.labels[-1] == "H12"

    rack = schema.kinds["rack"].geometry
    assert rack is not None and rack.type is GeometryType.named
    assert rack.labels == [str(number) for number in range(1, 11)]

    for name, capacity in (("shelf", 12), ("freezer", 6), ("drawer", 1)):
        geometry = schema.kinds[name].geometry
        assert geometry is not None
        assert geometry.type is GeometryType.slotless
        assert geometry.labels == []
        assert geometry.capacity == capacity


def test_molbio_derives_construct_fields_from_zsqlab08() -> None:
    schema = load_bundled("molbio")

    construct = schema.kinds["construct"]
    assert construct.object is ObjectType.entity
    assert construct.fields["sequence"].type is FieldType.sequence
    assert construct.fields["sequence"].unique
    assert construct.fields["length_bp"].type is FieldType.computed
    assert construct.fields["length_bp"].expr == "CHAR_LENGTH(sequence)"
    assert construct.fields["molecular_weight_da"].type is FieldType.decimal

    reduced = construct.fields["extinction_coefficient_reduced_0p1pct"]
    oxidized = construct.fields["extinction_coefficient_oxidized_0p1pct"]
    assert reduced.type is FieldType.computed
    assert oxidized.type is FieldType.computed
    assert "extinction_coefficient_reduced_m1cm1" in (reduced.expr or "")
    assert "extinction_coefficient_oxidized_m1cm1" in (oxidized.expr or "")
    assert "molecular_weight_da" in (reduced.expr or "")
    assert reduced.decimal_precision == 12 and reduced.decimal_scale == 8

    plasmid = schema.kinds["plasmid"]
    assert plasmid.fields["construct"].type is FieldType.ref
    assert plasmid.fields["construct"].to == "construct"
    assert plasmid.fields["sequence"].type is FieldType.sequence
    assert plasmid.fields["backbone"].type is FieldType.string
    assert plasmid.fields["resistance"].values == ["amp", "kan", "cm", "spec", "tet"]

    strain = schema.kinds["strain"]
    assert set(strain.fields) >= {"name", "description", "genotype", "species"}


def test_molbio_stock_kinds_match_the_old_ledgers() -> None:
    schema = load_bundled("molbio")

    bacterial = schema.kinds["bacterial"]
    assert bacterial.object is ObjectType.stock
    assert bacterial.entity_kinds == ["plasmid", "strain"]
    assert bacterial.fields["strain"].required
    assert set(bacterial.aliquot_fields) == {"glycerol_pct"}
    assert bacterial.aliquot_fields["glycerol_pct"].default == 15

    assert schema.kinds["plasmid_prep"].entity_kinds == ["plasmid"]
    assert "concentration_ng_ul" in schema.kinds["plasmid_prep"].fields
    assert "prep_method" in schema.kinds["plasmid_prep"].fields

    assert schema.kinds["protein_prep"].entity_kinds == ["construct"]
    assert "concentration_mg_ml" in schema.kinds["protein_prep"].fields
    assert "buffer" in schema.kinds["protein_prep"].fields
    assert "purification_id" in schema.kinds["protein_prep"].fields

    pellet = schema.kinds["pellet"]
    assert pellet.entity_kinds == ["plasmid", "strain"]
    assert {"culture_volume_ml", "od600", "induction"} <= set(pellet.fields)
    # Pellet volumes live in the core aliquot quantity, not in an extension table.
    assert pellet.aliquot_fields == {}


def test_library_book_and_copy() -> None:
    schema = load_bundled("library")

    book = schema.kinds["book"]
    assert book.object is ObjectType.entity
    assert book.fields["title"].required
    assert book.fields["isbn"].unique
    assert book.fields["isbn"].pattern == "^[0-9Xx][0-9Xx-]{8,16}$"
    assert book.fields["year"].type is FieldType.int

    copy = schema.kinds["copy"]
    assert copy.object is ObjectType.stock
    assert copy.entity_kinds == ["book"]
    assert copy.fields["condition"].values == ["new", "good", "fair", "poor"]
    assert copy.fields["acquired_on"].type is FieldType.date


def test_combining_base_with_molbio_is_deduplicated() -> None:
    combined = load_schema(
        LedgerConfig(name="demo", presets=["base", "molbio"])
    )
    alone = load_bundled("molbio")
    assert combined.fingerprint() == alone.fingerprint()
    assert combined.preset_versions == {"base": 1, "molbio": 1}


# ---------------------------------------------------------------------------
# Init against real Dolt
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", BUNDLED)
def test_preset_inits_a_ledger_and_its_rules_hold(
    ledger_factory, name: str
) -> None:
    ledger = ledger_factory([name], name=f"preset_{name}")
    schema = ledger.schema

    assert set(schema.kinds) == EXPECTED_KINDS[name]
    assert ledger.meta["fingerprint"] == schema.fingerprint()
    versions = json.loads(ledger.meta["preset_versions"])
    assert versions[name] == 1

    tables = table_names(ledger)
    expected: set[str] = set()
    for kind_name, kind in schema.kinds.items():
        expected.add(f"{kind.object.value}_{kind_name}")
        expected.add(f"v_{kind.object.value}_{kind_name}")
        if kind.aliquot_fields:
            expected.add(f"aliquot_{kind_name}")
    missing = expected - tables
    assert not missing, f"missing generated objects: {sorted(missing)}"

    # Dolt init made one commit; tm init made exactly one more.
    assert commit_count(ledger) == 2
    assert ledger.driver.query("SELECT * FROM dolt_status") == []

    # Every bundled rule is valid SQL and holds on the empty ledger.
    for rule in schema.rules:
        rows = ledger.driver.query(rule.sql)
        assert rows, rule.id
        value = next(iter(rows[0].values()))
        assert rule.expect.kind == "single_value", rule.id
        assert int(value) == rule.expect.value, rule.id


def test_molbio_bacterial_extension_table_is_generated(ledger_factory) -> None:
    ledger = ledger_factory(["molbio"], name="molbio_tables")
    tables = table_names(ledger)
    assert "aliquot_bacterial" in tables
    assert "stock_pellet" in tables
    assert "stock_plasmid_prep" in tables
    assert "stock_protein_prep" in tables

    columns = ledger.driver.query(
        "SELECT column_name AS name FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND table_name = 'entity_construct'"
    )
    names = {str(row["name"]) for row in columns}
    assert {
        "length_bp",
        "extinction_coefficient_reduced_0p1pct",
        "extinction_coefficient_oxidized_0p1pct",
        "sequence_sha256",
    } <= names


# ---------------------------------------------------------------------------
# library end-to-end (raw SQL: object operations arrive with ticket 10)
# ---------------------------------------------------------------------------


def test_library_add_book_copy_and_aliquot_in_a_box(ledger_factory) -> None:
    ledger = ledger_factory(["library"], name="library_e2e")
    geometry = ledger.schema.kind("grid96").geometry
    assert geometry is not None
    before = commit_count(ledger)

    with ledger.write("seed a book and a copy", verb="add", object="copy") as session:
        session.execute(
            "INSERT INTO `entity` (`id`, `kind`, `name`) VALUES (%s, %s, %s)",
            ("book-0001", "book", "The C Programming Language"),
        )
        session.execute(
            "INSERT INTO `entity_book` "
            "(`id`, `name`, `title`, `authors`, `isbn`, `year`, `publisher`) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                "book-0001",
                "The C Programming Language",
                "The C Programming Language",
                "Brian W. Kernighan; Dennis M. Ritchie",
                "978-0-13-110362-7",
                1978,
                "Prentice Hall",
            ),
        )
        session.execute(
            "INSERT INTO `stock` (`id`, `kind`, `entity_id`) VALUES (%s, %s, %s)",
            ("CPY-0001", "copy", "book-0001"),
        )
        session.execute(
            "INSERT INTO `stock_copy` "
            "(`id`, `acquired_on`, `condition`, `source`, `price`) "
            "VALUES (%s, %s, %s, %s, %s)",
            ("CPY-0001", "2026-01-15", "good", "bookshop", 39.95),
        )
        session.execute(
            "INSERT INTO `box` (`id`, `name`, `kind`) VALUES (%s, %s, %s)",
            ("box-0001", "LIB-01", "grid96"),
        )
        for row in slot_rows("box-0001", geometry):
            session.execute(
                "INSERT INTO `slot` "
                "(`box_id`, `label`, `row_label`, `col_label`, `ordinal`) "
                "VALUES (%s, %s, %s, %s, %s)",
                (
                    row["box_id"],
                    row["label"],
                    row["row_label"],
                    row["col_label"],
                    row["ordinal"],
                ),
            )
        session.execute(
            "INSERT INTO `aliquot` "
            "(`id`, `stock_id`, `seq`, `box_id`, `slot`, `status`, "
            "`quantity`, `unit`, `label`) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            ("CPY-0001.1", "CPY-0001", 1, "box-0001", "A1", "stored", 1, "copy", "first copy"),
        )

    assert commit_count(ledger) == before + 1
    latest = ledger.driver.query(
        "SELECT message FROM dolt_log ORDER BY date DESC, commit_order DESC LIMIT 1"
    )[0]
    assert latest["message"] == "add(copy): seed a book and a copy"

    slot = ledger.driver.query(
        "SELECT * FROM v_box_slots WHERE box_name = %s AND slot = %s",
        ("LIB-01", "A1"),
    )[0]
    assert slot["occupant_type"] == "aliquot"
    assert slot["occupant_id"] == "CPY-0001.1"
    assert slot["stock_id"] == "CPY-0001"
    assert slot["entity_id"] == "book-0001"
    assert slot["status"] == "stored"

    location = ledger.driver.query(
        "SELECT * FROM v_aliquot_location WHERE aliquot_id = %s",
        ("CPY-0001.1",),
    )[0]
    assert location["box_path"] == "LIB-01"
    assert location["location"] == "LIB-01:A1"

    status = ledger.driver.query(
        "SELECT * FROM v_stock_status WHERE stock_id = %s", ("CPY-0001",)
    )[0]
    assert status["status"] == "active"
    assert status["aliquot_count"] == 1
    assert status["stored_count"] == 1

    book = ledger.driver.query(
        "SELECT * FROM v_entity_book WHERE id = %s", ("book-0001",)
    )[0]
    assert book["title"] == "The C Programming Language"
    assert book["isbn"] == "978-0-13-110362-7"


# This module intentionally keeps the embedded-driver path covered.
pytestmark = pytest.mark.embedded
