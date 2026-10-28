"""The repository presets (tickets 07, 20): loading, copying, rules and a walkthrough.

Presets live in the repository's ``presets/`` directory and ``tm init`` copies
them (and their include closure) into each ledger.  Like the rest of the suite
these tests run against a real ``dolt`` binary and a workspace whose Dolt/Git
configuration lives under ``tmp_path``.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from tetramark.cli.app import app

from tetramark.core.ledger import init_ledger, open as open_ledger
from tetramark.driver.workspace import Workspace
from tetramark.errors import LedgerError, UsageError
from tetramark.schema.ddl import slot_rows
from tetramark.schema.loader import (
    PresetFile,
    expand_presets,
    list_presets,
    load_ledger_schema,
    load_preset,
    load_schema,
    preset_copy_files,
    resolve_preset_spec,
)
from tetramark.schema.model import (
    FieldType,
    GeometryType,
    LedgerConfig,
    ObjectType,
    SchemaError,
)

from conftest import FACTORY_AUTHOR, PRESETS, REPO_PRESETS

BUNDLED = ("base", "molbio", "library")

#: Current preset versions: molbio is at 2 (ticket 22).
EXPECTED_VERSIONS = {"base": 1, "molbio": 2, "library": 1}

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
        "protein_construct",
        "plasmid",
        "bacterial_strain",
        "cell_line",
        "bacterial_glycerol",
        "plasmid_prep",
        "protein_prep",
        "bacterial_pellet",
        "cell_vial",
        "cell_pellet",
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


def repo_preset(name: str) -> Path:
    return REPO_PRESETS / f"{name}.yaml"


def load_repo_preset(name: str):
    return load_schema(LedgerConfig(name="preset_test", presets=[str(repo_preset(name))]))


def read_tree(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


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


def test_all_three_presets_are_discoverable_in_the_repo() -> None:
    assert list_presets(REPO_PRESETS) == ["base", "library", "molbio"]


def test_presets_are_repo_files_not_package_data() -> None:
    for name in BUNDLED:
        assert repo_preset(name).is_file(), name
        document = yaml.safe_load(repo_preset(name).read_text(encoding="utf-8"))
        assert document["version"] == EXPECTED_VERSIONS[name], name
        assert document["preset"] == name

    assert not (REPO_PRESETS / "base").exists()
    package_root = (
        Path(__file__).resolve().parents[1] / "src" / "tetramark"
    )
    assert not (package_root / "presets").exists()
    assert importlib.util.find_spec("tetramark.presets") is None


def test_repo_molbio_resolves_its_include_chain() -> None:
    preset = load_preset(str(repo_preset("molbio")))
    assert preset.preset == "molbio"
    # "item" comes from base via ``include:``, "plasmid" is molbio's own.
    assert {"item", "plasmid"} <= set(preset.kinds)
    assert preset.include == []


@pytest.mark.parametrize("name", BUNDLED)
def test_preset_loads_with_expected_kinds_and_rules(name: str) -> None:
    schema = load_repo_preset(name)

    assert set(schema.kinds) == EXPECTED_KINDS[name]
    assert schema.preset_versions[name] == EXPECTED_VERSIONS[name]
    # molbio and library both include base.
    if name != "base":
        assert schema.preset_versions["base"] == 1

    own_prefix = {"base": "BASE", "molbio": "MB", "library": "LIB"}[name]
    own_rules = [rule for rule in schema.rules if rule.id.startswith(own_prefix)]
    assert 2 <= len(own_rules) <= 5
    assert {rule.severity.value for rule in own_rules} == {"fail", "info"}
    assert all(rule.sql.strip().upper().startswith("SELECT") for rule in schema.rules)


@pytest.mark.parametrize("name", BUNDLED)
def test_every_kind_and_field_has_a_description(name: str) -> None:
    schema = load_repo_preset(name)
    for kind_name, kind in schema.kinds.items():
        assert kind.description and kind.description.strip(), kind_name
        for field_name, field in kind.fields.items():
                assert field.description and field.description.strip(), (
                    kind_name,
                    field_name,
                )


def test_base_geometry_and_fields() -> None:
    schema = load_repo_preset("base")

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
    schema = load_repo_preset("molbio")

    construct = schema.kinds["protein_construct"]
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
    assert plasmid.fields["construct"].to == "protein_construct"
    assert plasmid.fields["sequence"].type is FieldType.sequence
    assert plasmid.fields["backbone"].type is FieldType.string
    assert plasmid.fields["resistance"].values == ["amp", "kan", "cm", "spec", "tet"]

    strain = schema.kinds["bacterial_strain"]
    assert strain.object is ObjectType.entity
    assert set(strain.fields) >= {"name", "description", "genotype", "species"}

    # The eukaryotic kind: a stable/derived line is its own entity that
    # points at its parent line, and the loader accepts the self-reference.
    cell_line = schema.kinds["cell_line"]
    assert cell_line.object is ObjectType.entity
    assert cell_line.mixins == ["named_fields"]
    assert set(cell_line.fields) == {
        "name",
        "description",
        "species",
        "cell_type",
        "source",
        "genotype",
        "parental",
    }
    assert cell_line.fields["parental"].type is FieldType.ref
    assert cell_line.fields["parental"].to == "cell_line"


def test_molbio_stock_kinds_match_the_old_ledgers() -> None:
    schema = load_repo_preset("molbio")

    # Sugar in the name: neither "bacterial" nor "pellet" is a suffix of the
    # renamed kind, so a short name cannot be mistaken for the old table.
    bacterial = schema.kinds["bacterial_glycerol"]
    assert bacterial.object is ObjectType.stock
    assert bacterial.entity_kinds == ["plasmid", "bacterial_strain"]
    assert bacterial.fields["strain"].required
    # The tube property is a stock field now; molbio has no aliquot fields.
    glycerol = bacterial.fields["glycerol_pct"]
    assert glycerol.type is FieldType.decimal
    assert (glycerol.decimal_precision, glycerol.decimal_scale) == (5, 2)
    assert glycerol.default == 15

    assert schema.kinds["plasmid_prep"].entity_kinds == ["plasmid"]
    assert "concentration_ng_ul" in schema.kinds["plasmid_prep"].fields
    assert "prep_method" in schema.kinds["plasmid_prep"].fields

    assert schema.kinds["protein_prep"].entity_kinds == ["protein_construct"]
    assert "concentration_mg_ml" in schema.kinds["protein_prep"].fields
    assert "buffer" in schema.kinds["protein_prep"].fields
    assert "purification_id" in schema.kinds["protein_prep"].fields

    pellet = schema.kinds["bacterial_pellet"]
    assert pellet.entity_kinds == ["plasmid", "bacterial_strain"]
    assert {"culture_volume_ml", "od600", "induction"} <= set(pellet.fields)
    # Pellet volumes live in the core aliquot quantity, not in an extension table.

    vial = schema.kinds["cell_vial"]
    assert vial.object is ObjectType.stock
    assert vial.entity_kinds == ["cell_line"]
    assert set(vial.fields) == {
        "passage",
        "medium",
        "freezing_medium",
        "cells_per_vial",
        "dmso_pct",
    }
    assert vial.fields["dmso_pct"].default == 10

    cell_pellet = schema.kinds["cell_pellet"]
    assert cell_pellet.entity_kinds == ["cell_line", "plasmid"]
    assert set(cell_pellet.fields) == {
        "cell_count",
        "passage",
        "transfection",
        "harvested_on",
    }


def test_library_book_and_copy() -> None:
    schema = load_repo_preset("library")

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
        LedgerConfig(
            name="demo",
            presets=[str(repo_preset("base")), str(repo_preset("molbio"))],
        )
    )
    alone = load_repo_preset("molbio")
    assert combined.fingerprint() == alone.fingerprint()
    assert combined.preset_versions == {"base": 1, "molbio": 2}


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
    assert versions[name] == EXPECTED_VERSIONS[name]

    tables = table_names(ledger)
    expected: set[str] = set()
    for kind_name, kind in schema.kinds.items():
        expected.add(f"{kind.object.value}_{kind_name}")
        expected.add(f"v_{kind.object.value}_{kind_name}")
    missing = expected - tables
    assert not missing, f"missing generated objects: {sorted(missing)}"

    # Dolt init made one commit; tm init made exactly one more.
    assert commit_count(ledger) == 2
    assert ledger.driver.query("SELECT * FROM dolt_status") == []

    # Every repo-preset rule is valid SQL and holds on the empty ledger.
    for rule in schema.rules:
        rows = ledger.driver.query(rule.sql)
        assert rows, rule.id
        value = next(iter(rows[0].values()))
        assert rule.expect.kind == "single_value", rule.id
        assert int(value) == rule.expect.value, rule.id


def test_molbio_v2_fresh_init_has_the_renamed_and_cell_objects(
    ledger_factory,
) -> None:
    """Verify bullet 1: ``tm init --preset molbio`` produces the v2 shape."""
    ledger = ledger_factory(["molbio"], name="molbio_tables")
    tables = table_names(ledger)
    for table in (
        "entity_protein_construct",
        "entity_bacterial_strain",
        "entity_cell_line",
        "stock_bacterial_glycerol",
        "stock_bacterial_pellet",
        "stock_cell_vial",
        "stock_cell_pellet",
    ):
        assert table in tables, table
    for gone in (
        "aliquot_bacterial",
        "entity_construct",
        "entity_strain",
        "stock_bacterial",
        "stock_pellet",
    ):
        assert gone not in tables, gone
    # An aliquot is a generic tube: no per-stock-kind extension tables.
    assert {name for name in tables if name.startswith("aliquot_")} == {
        "aliquot_event"
    }

    columns = {
        str(row["name"])
        for row in ledger.driver.query(
            "SELECT column_name AS name FROM information_schema.columns "
            "WHERE table_schema = DATABASE() "
            "AND table_name = 'stock_bacterial_glycerol'"
        )
    }
    assert {"strain", "medium", "batch", "glycerol_pct"} <= columns
    construct_columns = {
        str(row["name"])
        for row in ledger.driver.query(
            "SELECT column_name AS name FROM information_schema.columns "
            "WHERE table_schema = DATABASE() "
            "AND table_name = 'entity_protein_construct'"
        )
    }
    assert {
        "length_bp",
        "extinction_coefficient_reduced_0p1pct",
        "extinction_coefficient_oxidized_0p1pct",
        "sequence_sha256",
    } <= construct_columns

    # The generated tests come from v2 and hold on the fresh ledger.
    report = ledger.check()
    assert report.passed, [result.name for result in report.failures]
    assert {"rule_MB01", "rule_MB02", "rule_MB05"} <= {
        result.name for result in report.results
    }
    checked = CliRunner().invoke(
        app,
        [
            "--workspace",
            str(ledger.workspace.data_dir),
            "--ledger",
            ledger.name,
            "check",
        ],
    )
    assert checked.exit_code == 0, checked.output
    assert "FAIL" not in checked.output
    assert "OK:" in checked.output


def test_molbio_cell_line_parental_self_reference_is_usable(
    ledger_factory,
) -> None:
    """``cell_line.parental`` is a self-ref: the loader and the FK accept it."""
    ledger = ledger_factory(["molbio"], name="molbio_cell_line")
    parent = ledger.entities.add(
        "cell_line",
        name="HEK293T",
        values={"species": "Homo sapiens", "cell_type": "epithelial"},
    )
    child = ledger.entities.add(
        "cell_line",
        name="HEK293T-EGFR",
        values={"parental": parent["id"], "genotype": "EGFR overexpression"},
    )
    links = {
        row["id"]: row["parental"]
        for row in ledger.driver.query(
            "SELECT `id`, `parental` FROM `entity_cell_line`"
        )
    }
    assert links == {parent["id"]: None, child["id"]: parent["id"]}

    voucher = ledger.stocks.add(
        "cell_vial",
        entity=child["id"],
        values={
            "passage": 7,
            "medium": "DMEM + 10% FBS",
            "freezing_medium": "90% FBS + 10% DMSO",
            "cells_per_vial": 1_000_000,
            "dmso_pct": 10,
        },
        aliquots=1,
        quantity=1,
        unit="vial",
    )
    row = ledger.driver.query(
        "SELECT `passage`, `dmso_pct`, `cells_per_vial` "
        "FROM `stock_cell_vial` WHERE `id` = %s",
        (voucher["id"],),
    )[0]
    assert (row["passage"], float(row["dmso_pct"]), row["cells_per_vial"]) == (
        7,
        10.0,
        1_000_000,
    )
    report = ledger.check()
    assert report.passed, [result.name for result in report.failures]


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


# ---------------------------------------------------------------------------
# Ticket 20: presets are copied into the ledger, which resolves only its copy
# ---------------------------------------------------------------------------


def make_workspace(tmp_path: Path, dolt_env: dict[str, str], **overrides: object):
    return Workspace.create(tmp_path / "data", env=dolt_env, **overrides)


def test_init_name_copies_the_closure_byte_identical_and_reopens_without_source(
    tmp_path: Path, dolt_env: dict[str, str]
) -> None:
    """``tm init --preset molbio`` copies molbio + base, byte for byte."""
    source = tmp_path / "presets-src"
    shutil.copytree(REPO_PRESETS, source)
    workspace = make_workspace(tmp_path, dolt_env, presets_dir=str(source))

    ledger = init_ledger(workspace, "copied", ["molbio"], author=FACTORY_AUTHOR)
    copies = ledger.path / "presets"
    assert (copies / "molbio.yaml").read_bytes() == (source / "molbio.yaml").read_bytes()
    assert (copies / "base.yaml").read_bytes() == (source / "base.yaml").read_bytes()
    fingerprint = ledger.fingerprint
    assert ledger.schema.preset_versions == {"base": 1, "molbio": 2}
    assert yaml.safe_load((ledger.path / "ledger.yaml").read_text())["presets"] == [
        "molbio"
    ]
    ledger.close()

    # The ledger is self-contained: deleting the source directory changes nothing.
    shutil.rmtree(source)
    reopened = open_ledger(workspace, "copied")
    try:
        assert reopened.fingerprint == fingerprint
        assert {"plasmid", "item"} <= set(reopened.schema.kinds)
        assert reopened.schema.preset_versions == {"base": 1, "molbio": 2}
    finally:
        reopened.close()


def test_init_path_needs_no_presets_dir_and_copies_its_includes(
    tmp_path: Path, dolt_env: dict[str, str]
) -> None:
    """A custom preset path works without ``presets_dir``; its includes are copied."""
    custom_dir = tmp_path / "custom"
    custom_dir.mkdir()
    shutil.copyfile(REPO_PRESETS / "base.yaml", custom_dir / "base.yaml")
    (custom_dir / "custom.yaml").write_text(
        """preset: custom
version: 1
description: A custom test preset.
include: [base]
kinds:
  widget:
    object: entity
    fields:
      size: {type: int}
""",
        encoding="utf-8",
    )
    workspace = make_workspace(tmp_path, dolt_env)

    ledger = init_ledger(
        workspace, "customled", [custom_dir / "custom.yaml"], author=FACTORY_AUTHOR
    )
    try:
        copies = ledger.path / "presets"
        assert (copies / "custom.yaml").read_bytes() == (
            custom_dir / "custom.yaml"
        ).read_bytes()
        assert (copies / "base.yaml").read_bytes() == (
            custom_dir / "base.yaml"
        ).read_bytes()
        assert yaml.safe_load((ledger.path / "ledger.yaml").read_text())["presets"] == [
            "custom"
        ]
        assert "widget" in ledger.schema.kinds and "item" in ledger.schema.kinds
        fingerprint = ledger.fingerprint
    finally:
        ledger.close()

    shutil.rmtree(custom_dir)
    reopened = open_ledger(workspace, "customled")
    try:
        assert reopened.fingerprint == fingerprint
        assert reopened.schema.preset_versions == {"base": 1, "custom": 1}
    finally:
        reopened.close()


def test_init_copies_a_directory_preset_with_its_kinds_and_rules(
    tmp_path: Path, dolt_env: dict[str, str]
) -> None:
    workspace = make_workspace(tmp_path, dolt_env)
    ledger = init_ledger(workspace, "dirled", [PRESETS / "extras"], author=FACTORY_AUTHOR)
    try:
        copies = ledger.path / "presets"
        assert (copies / "extras" / "preset.yaml").read_bytes() == (
            PRESETS / "extras" / "preset.yaml"
        ).read_bytes()
        assert (copies / "extras" / "kinds" / "more.yaml").is_file()
        assert (copies / "extras" / "rules" / "more.yaml").is_file()
        # The included directory preset is copied too.
        assert (copies / "base" / "preset.yaml").is_file()
        assert {"local_entity", "extra_box", "item"} <= set(ledger.schema.kinds)
        fingerprint = ledger.fingerprint
        assert ledger.schema.preset_versions == {"base": 2, "extras": 1}
    finally:
        ledger.close()

    reopened = open_ledger(workspace, "dirled")
    try:
        assert reopened.fingerprint == fingerprint
        assert {"local_entity", "extra_box"} <= set(reopened.schema.kinds)
    finally:
        reopened.close()


def test_open_names_the_expected_file_when_the_ledger_copy_is_missing(
    tmp_path: Path, dolt_env: dict[str, str]
) -> None:
    workspace = make_workspace(tmp_path, dolt_env, presets_dir=str(REPO_PRESETS))
    ledger = init_ledger(workspace, "deleted", ["molbio"], author=FACTORY_AUTHOR)
    ledger.close()

    (ledger.path / "presets" / "molbio.yaml").unlink()
    with pytest.raises(SchemaError) as caught:
        open_ledger(workspace, "deleted")
    message = str(caught.value)
    assert "presets/molbio.yaml" in message
    assert "migration/" in message
    assert "manual copy" in message
    # The workspace's presets_dir is not a fallback.
    assert "never from a search path" in message


def test_changing_the_source_preset_after_init_does_not_change_the_ledger(
    tmp_path: Path, dolt_env: dict[str, str]
) -> None:
    source = tmp_path / "presets-src"
    shutil.copytree(REPO_PRESETS, source)
    workspace = make_workspace(tmp_path, dolt_env, presets_dir=str(source))
    ledger = init_ledger(workspace, "frozen", ["molbio"], author=FACTORY_AUTHOR)
    fingerprint = ledger.fingerprint
    ledger.close()

    document = yaml.safe_load((source / "molbio.yaml").read_text(encoding="utf-8"))
    document["kinds"]["late_entity"] = {"object": "entity"}
    (source / "molbio.yaml").write_text(
        yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
    )

    reopened = open_ledger(workspace, "frozen")
    try:
        assert "late_entity" not in reopened.schema.kinds
        assert reopened.fingerprint == fingerprint
        assert reopened.check_fingerprint(warn=False)
    finally:
        reopened.close()


def test_init_name_without_presets_dir_is_a_usage_error_naming_the_key(
    tmp_path: Path, dolt_env: dict[str, str]
) -> None:
    workspace = make_workspace(tmp_path, dolt_env)
    with pytest.raises(UsageError, match="presets_dir"):
        init_ledger(workspace, "nokey", ["molbio"], author=FACTORY_AUTHOR)
    assert not workspace.ledger_path("nokey").exists()


def test_init_path_must_exist(tmp_path: Path, dolt_env: dict[str, str]) -> None:
    workspace = make_workspace(tmp_path, dolt_env)
    with pytest.raises((SchemaError, LedgerError), match="not found"):
        init_ledger(
            workspace, "nofile", [tmp_path / "missing.yaml"], author=FACTORY_AUTHOR
        )
    assert not workspace.ledger_path("nofile").exists()


def test_preset_helpers_resolve_names_and_paths(tmp_path: Path) -> None:
    spec = resolve_preset_spec("molbio", presets_dir=REPO_PRESETS)
    assert spec == PresetFile(
        name="molbio", path=(REPO_PRESETS / "molbio.yaml").resolve(), is_file=True
    )
    closure = expand_presets([spec.path])
    assert [preset.name for preset in closure] == ["base", "molbio"]
    assert [copy.destination for preset in closure for copy in preset_copy_files(preset)] == [
        Path("base.yaml"),
        Path("molbio.yaml"),
    ]

    custom = tmp_path / "custom.yaml"
    custom.write_text("preset: custom\nversion: 1\n", encoding="utf-8")
    path_spec = resolve_preset_spec(str(custom), presets_dir=None)
    assert path_spec.name == "custom" and path_spec.path == custom.resolve()


# ---------------------------------------------------------------------------
# Ticket-20 review fixes: preset names, self-contained includes, copy reload
# ---------------------------------------------------------------------------


def write_preset(path: Path, payload: dict[str, object]) -> Path:
    path.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize(
    "evil_name",
    ["../../lab1/presets/molbio", "/tmp/abs/presets/molbio", "..", "a/b", "1nope"],
)
def test_init_rejects_an_invalid_preset_name_and_writes_nothing_outside(
    tmp_path: Path, dolt_env: dict[str, str], evil_name: str
) -> None:
    """F1: a path-like ``preset:`` must not escape ``<ledger>/presets/``."""
    workspace = make_workspace(tmp_path, dolt_env)
    victim = workspace.ledger_path("lab1") / "presets"
    victim.mkdir(parents=True)
    (victim / "molbio.yaml").write_bytes(b"victim preset\n")
    evil = write_preset(
        tmp_path / "evil.yaml",
        {"preset": evil_name, "version": 1, "kinds": {"evil": {"object": "entity"}}},
    )

    with pytest.raises(UsageError, match="invalid preset name"):
        init_ledger(workspace, "labC", [evil], author=FACTORY_AUTHOR)

    assert not workspace.ledger_path("labC").exists()
    # Nothing outside the failed ledger was written or overwritten.
    assert (victim / "molbio.yaml").read_bytes() == b"victim preset\n"
    assert sorted(item.name for item in victim.iterdir()) == ["molbio.yaml"]


@pytest.mark.parametrize("bad_name", ["../evil", "..", "/tmp/abs/evil", "a/b"])
def test_preset_copy_destinations_cannot_escape_the_presets_dir(
    tmp_path: Path, bad_name: str
) -> None:
    """F1 defence in depth: the copy destination itself is checked too."""
    source = write_preset(tmp_path / "p.yaml", {"preset": "p", "version": 1})
    preset = PresetFile(name=bad_name, path=source, is_file=True)
    with pytest.raises(
        UsageError, match="outside <ledger>/presets/|invalid preset name"
    ):
        preset_copy_files(preset)


def test_init_rejects_an_include_given_by_path(
    tmp_path: Path, dolt_env: dict[str, str]
) -> None:
    """F2: a path include cannot be reproduced by the flattened copy."""
    shared = tmp_path / "shared"
    shared.mkdir()
    shutil.copyfile(REPO_PRESETS / "base.yaml", shared / "base.yaml")
    preset = write_preset(
        tmp_path / "custom.yaml",
        {
            "preset": "custom",
            "version": 1,
            "include": ["./shared/base.yaml"],
            "kinds": {"widget": {"object": "entity"}},
        },
    )
    workspace = make_workspace(tmp_path, dolt_env)

    with pytest.raises(SchemaError, match="is a path") as caught:
        init_ledger(workspace, "pathinclude", [preset], author=FACTORY_AUTHOR)

    message = str(caught.value)
    assert "bare preset names" in message
    assert "own 'preset:' field" in message
    assert caught.value.path == "include"
    assert not workspace.ledger_path("pathinclude").exists()


def test_init_rejects_an_include_name_not_matching_the_preset_name(
    tmp_path: Path, dolt_env: dict[str, str]
) -> None:
    """F2: including ``mybase`` while the target calls itself ``base`` would not reopen."""
    (tmp_path / "mybase.yaml").write_text(
        (REPO_PRESETS / "base.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    preset = write_preset(
        tmp_path / "custom.yaml",
        {
            "preset": "custom",
            "version": 1,
            "include": ["mybase"],
            "kinds": {"widget": {"object": "entity"}},
        },
    )
    workspace = make_workspace(tmp_path, dolt_env)

    with pytest.raises(SchemaError, match="own name is 'base'") as caught:
        init_ledger(workspace, "nameinclude", [preset], author=FACTORY_AUTHOR)

    assert "bare preset names" in str(caught.value)
    assert not workspace.ledger_path("nameinclude").exists()


def test_init_verifies_the_copy_reloads_and_rolls_back_when_it_cannot(
    tmp_path: Path, dolt_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """F2: init reloads the ledger from its own copy before doing any DB work."""
    workspace = make_workspace(tmp_path, dolt_env, presets_dir=str(REPO_PRESETS))

    def boom(path: object) -> None:
        raise SchemaError("injected copy verification failure")

    monkeypatch.setattr("tetramark.core.ledger.load_ledger_schema", boom)
    with pytest.raises(LedgerError, match="do not reopen from the ledger itself") as caught:
        init_ledger(workspace, "unverifiable", ["molbio"], author=FACTORY_AUTHOR)

    assert "injected copy verification failure" in str(caught.value)
    assert "self-contained" in str(caught.value)
    # The failed init left nothing behind (no half-initialised ledger).
    assert not workspace.ledger_path("unverifiable").exists()


def test_a_missing_include_copy_keeps_the_documented_hint(
    tmp_path: Path, dolt_env: dict[str, str]
) -> None:
    """F7: deleting ``presets/base.yaml`` still points at migration/ + manual copy."""
    workspace = make_workspace(tmp_path, dolt_env, presets_dir=str(REPO_PRESETS))
    ledger = init_ledger(workspace, "falling_apart", ["molbio"], author=FACTORY_AUTHOR)
    fingerprint = ledger.fingerprint
    ledger.close()

    # The supported copy reloads exactly (this is what init now verifies).
    _config, reloaded = load_ledger_schema(ledger.path)
    assert reloaded.fingerprint() == fingerprint

    (ledger.path / "presets" / "base.yaml").unlink()
    with pytest.raises(SchemaError) as caught:
        open_ledger(workspace, "falling_apart")
    message = str(caught.value)
    assert "presets/base.yaml" in message
    assert "migration/" in message
    assert "manual copy" in message
    assert caught.value.path == "include"


# This module intentionally keeps the embedded-driver path covered.
pytestmark = pytest.mark.embedded
