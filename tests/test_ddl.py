"""DDL generation: pure statements, then execution against a real Dolt repo.

Ticket 05: generate for the test presets from ticket 03, execute the script
with plain ``dolt sql`` subprocesses (the embedded driver is not required
here) and assert that the tables/views exist and the constraints bite.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tetramark.driver.base import literal
from tetramark.schema.ddl import (
    CORE_TABLES,
    CORE_VIEWS,
    STOCK_VIEW_COLUMNS,
    Statement,
    core_statements,
    kind_statements,
    schema_statements,
    slot_rows,
    table_name,
    to_sql,
    view_statements,
)
from tetramark.schema.loader import load_schema
from tetramark.schema.model import (
    EffectiveSchema,
    Field,
    FieldType,
    Kind,
    LedgerConfig,
    ObjectType,
    SchemaError,
)

DATA = Path(__file__).parent / "data"
PRESETS = DATA / "presets"
GOLDEN = DATA / "golden" / "ddl_full.sql"
REPO_PRESETS = Path(__file__).resolve().parents[1] / "presets"
MOLBIO = REPO_PRESETS / "molbio.yaml"


# ---------------------------------------------------------------------------
# Pure generation
# ---------------------------------------------------------------------------


def full_schema(**overrides: Any) -> EffectiveSchema:
    config = LedgerConfig(
        name="ddl",
        presets=[str(PRESETS / "full")],
        **overrides,
    )
    return load_schema(config)


def molbio_schema() -> EffectiveSchema:
    return load_schema(LedgerConfig(name="ddl-molbio", presets=[str(MOLBIO)]))


def test_statements_are_tagged_and_ordered() -> None:
    schema = full_schema()
    statements = schema_statements(schema)

    assert isinstance(statements[0], Statement)
    assert statements[0].name == "ledger_meta"
    assert {statement.object for statement in statements} == {"table", "fk", "view"}

    # Every core table comes before any view; views are generated last.
    table_names = [s.name for s in statements if s.object == "table"]
    for core in CORE_TABLES:
        assert core in table_names
    last_table = max(i for i, s in enumerate(statements) if s.object == "table")
    first_view = min(i for i, s in enumerate(statements) if s.object == "view")
    assert last_table < first_view

    # FKs are separate ALTER statements that point at their owning table.
    for statement in statements:
        if statement.object == "fk":
            assert statement.table is not None
            assert statement.name.startswith("fk_")
            assert "ADD CONSTRAINT" in statement.sql

    # Every kind has a table and a convenience view.
    for kind_name, kind in schema.kinds.items():
        table = table_name(kind.object, kind_name)
        assert table in table_names
        assert f"v_{kind.object.value}_{kind_name}" in {
            s.name for s in statements if s.object == "view"
        }

    # An aliquot is a generic tube: no per-stock-kind extension tables exist.
    assert [name for name in table_names if name.startswith("aliquot_")] == [
        "aliquot_event"
    ]


def test_generation_is_deterministic() -> None:
    first = schema_statements(full_schema())
    second = schema_statements(full_schema())
    assert [(s.object, s.name, s.table, s.sql) for s in first] == [
        (s.object, s.name, s.table, s.sql) for s in second
    ]
    assert to_sql(first) == to_sql(second)


def test_statement_helpers_return_composable_lists() -> None:
    schema = full_schema()
    core = core_statements(schema.statuses)
    kinds = kind_statements(schema)
    views = view_statements(schema)

    assert all(isinstance(statement, Statement) for statement in [*core, *kinds, *views])
    assert [s.name for s in core[: len(CORE_TABLES)]] == list(CORE_TABLES)
    assert all(name in {s.name for s in views} for name in CORE_VIEWS)
    assert to_sql([]) == ""


def test_every_generated_view_exposes_aliases() -> None:
    """spec v2: each view ends with its object's aggregated aliases."""
    statements = {s.name: s.sql for s in view_statements(full_schema())}
    for name, sql in statements.items():
        expected = (
            "`occupant_alias_list`" if name == "v_box_slots" else "`alias_list`"
        )
        assert f"AS {expected}" in sql, name


def test_alias_list_avoids_the_construct_aliases_field() -> None:
    """``alias_list`` is a view column; the domain field ``aliases`` stays one.

    The bundled ``protein_construct`` kind has a field called ``aliases``;
    naming the generated column ``aliases`` would produce duplicate view
    columns.
    """
    schema = load_schema(
        LedgerConfig(name="molbio", presets=[str(REPO_PRESETS / "molbio.yaml")])
    )
    construct = next(
        s.sql
        for s in view_statements(schema)
        if s.name == "v_entity_protein_construct"
    )
    assert "`aliases`" in construct
    assert "AS `alias_list`" in construct


def test_kind_field_named_alias_list_is_rejected() -> None:
    """A kind cannot declare the generated column's own name."""
    schema = full_schema()
    schema.kinds["everything"].fields["alias_list"] = Field(type=FieldType.string)
    with pytest.raises(SchemaError, match="alias_list"):
        view_statements(schema)


def test_stock_view_generated_columns_are_reserved() -> None:
    """A stock kind cannot shadow ``entity_kind``/``status``/… (spec §3)."""
    for name in STOCK_VIEW_COLUMNS:
        schema = full_schema()
        schema.kinds["lot"].fields[name] = Field(type=FieldType.string)
        with pytest.raises(SchemaError, match=name):
            view_statements(schema)


def test_stock_views_share_the_status_fragment_and_column_order() -> None:
    """The derived status and the aggregate block are generated once (spec §3)."""
    statements = {s.name: s.sql for s in view_statements(full_schema())}
    status = statements["v_stock_status"]
    stock_view = statements["v_stock_bacterial"]
    # The whole six-branch CASE is literally the same SQL in both views.
    case = status.split("CASE\n", 1)[1].split("END AS `status`", 1)[0]
    assert case.count("WHEN") == 5
    assert case in stock_view
    # … and so is the per-stock aggregate block.
    aggregate = status.split("LEFT JOIN (\n", 1)[1].split(") agg ", 1)[0]
    assert aggregate in stock_view
    # The new stock columns sit after the kind fields and before alias_list.
    select = stock_view.split("\nFROM ", 1)[0]
    ordered = [
        "`entity_kind`",
        "`entity_name`",
        "`aliquots_stored`",
        "`aliquots_total`",
        "`quantity_stored`",
        "`unit`",
        "`status`",
        "`alias_list`",
    ]
    positions = [select.index(name) for name in ordered]
    assert positions == sorted(positions), select
    assert positions.index(select.index("`alias_list`")) == len(ordered) - 1


def test_slot_rows_for_every_geometry() -> None:
    schema = full_schema()

    grid = slot_rows("box-grid", schema.kinds["grid9x9"].geometry)
    assert len(grid) == 81
    assert grid[0] == {
        "box_id": "box-grid",
        "label": "A1",
        "row_label": "A",
        "col_label": "1",
        "ordinal": 1,
    }
    assert grid[-1]["label"] == "J9"
    assert grid[-1]["ordinal"] == 81
    assert "I" not in {row["row_label"] for row in grid}

    named = slot_rows("box-named", schema.kinds["namedbox"].geometry)
    assert [row["label"] for row in named] == ["top", "middle", "bottom"]
    assert all(row["row_label"] is None and row["col_label"] is None for row in named)

    assert slot_rows("box-slotless", schema.kinds["slotlessbox"].geometry) == []


def test_golden_ddl_snapshot() -> None:
    rendered = to_sql(schema_statements(full_schema()))
    assert GOLDEN.is_file(), f"missing golden file: {GOLDEN}"
    assert rendered == GOLDEN.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Real Dolt helpers
# ---------------------------------------------------------------------------


def install(run: Any, repo: Path, statements: list[Statement]) -> None:
    result = run("sql", "-q", to_sql(statements), cwd=repo, check=False)
    assert not _failed(result), _output(result)


def execute(run: Any, repo: Path, sql: str) -> None:
    result = run("sql", "-q", sql, cwd=repo, check=False)
    assert not _failed(result), _output(result)


def query(run: Any, repo: Path, sql: str) -> list[dict[str, Any]]:
    result = run("sql", "-r", "json", "-q", sql, cwd=repo, check=False)
    assert not _failed(result), _output(result)
    rows: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if line:
            rows.extend(json.loads(line)["rows"])
    return rows


def reject(run: Any, repo: Path, sql: str, match: str) -> None:
    result = run("sql", "-q", sql, cwd=repo, check=False)
    text = _output(result)
    assert _failed(result), f"statement unexpectedly succeeded: {sql}\n{text}"
    assert match.lower() in text.lower(), text


def _failed(result: subprocess.CompletedProcess[str]) -> bool:
    # Dolt 2.3.5 can exit 0 on an SQL error, so inspect the output too.
    text = _output(result)
    return result.returncode != 0 or "error" in text.lower()


def _output(result: subprocess.CompletedProcess[str]) -> str:
    return "\n".join(part for part in (result.stdout, result.stderr) if part)


@pytest.fixture
def full_ledger(dolt_repo: Path, dolt_run: Any) -> Path:
    """A real Dolt repo with the ``full`` preset's complete DDL installed."""
    install(dolt_run, dolt_repo, schema_statements(full_schema()))
    return dolt_repo


def _insert_entity(run: Any, repo: Path, entity_id: str, name: str) -> None:
    execute(
        run,
        repo,
        f"INSERT INTO entity (id, kind, name) VALUES "
        f"({literal(entity_id)}, 'item', {literal(name)});"
        f"INSERT INTO entity_item (id, name) VALUES "
        f"({literal(entity_id)}, {literal(name)});",
    )


def _insert_stock(run: Any, repo: Path, stock_id: str, entity_id: str) -> None:
    execute(
        run,
        repo,
        f"INSERT INTO stock (id, kind, entity_id) VALUES "
        f"({literal(stock_id)}, 'bacterial', {literal(entity_id)});"
        f"INSERT INTO stock_bacterial (id, strain) VALUES "
        f"({literal(stock_id)}, 'MG1655');",
    )


# ---------------------------------------------------------------------------
# Real Dolt: objects exist
# ---------------------------------------------------------------------------


def test_core_tables_views_and_kind_objects_exist(
    full_ledger: Path, dolt_run: Any
) -> None:
    rows = query(
        dolt_run,
        full_ledger,
        "SELECT table_name AS name, table_type AS kind "
        "FROM information_schema.tables WHERE table_schema = DATABASE()",
    )
    objects = {row["name"]: row["kind"] for row in rows}

    for table in CORE_TABLES:
        assert objects.get(table) == "BASE TABLE", table
    for view in CORE_VIEWS:
        assert objects.get(view) == "VIEW", view

    schema = full_schema()
    for kind_name, kind in schema.kinds.items():
        assert objects.get(table_name(kind.object, kind_name)) == "BASE TABLE"
        assert objects.get(f"v_{kind.object.value}_{kind_name}") == "VIEW"

    # An aliquot is a generic tube: no per-stock-kind extension tables exist.
    assert [
        name for name in objects if name.startswith("aliquot_")
    ] == ["aliquot_event"]

    # Core DDL exposes the columns the spec names.
    columns = {
        row["name"]
        for row in query(
            dolt_run,
            full_ledger,
            "SELECT column_name AS name FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = 'aliquot_event'",
        )
    }
    assert {
        "id",
        "aliquot_id",
        "event",
        "amount",
        "unit",
        "from_box_id",
        "from_slot",
        "to_box_id",
        "to_slot",
        "old_status",
        "new_status",
        "actor",
        "purpose",
        "note",
        "at",
    } <= columns


def test_kind_ref_field_gets_a_foreign_key(
    dolt_repo: Path, dolt_run: Any
) -> None:
    """A ``ref`` field (now only on a kind table) gets a real FK."""
    schema = EffectiveSchema(
        kinds={
            "item": Kind(
                object=ObjectType.entity,
                fields={"name": Field(type=FieldType.string)},
            ),
            "prep": Kind(
                object=ObjectType.stock,
                entity_kinds=["item"],
                fields={
                    "batch": Field(type=FieldType.string),
                    "source": Field(type=FieldType.ref, to="item"),
                },
            ),
        }
    )
    statements = schema_statements(schema)
    assert any(
        s.object == "fk" and s.name == "fk_stock_prep_source" for s in statements
    )
    install(dolt_run, dolt_repo, statements)

    execute(
        dolt_run,
        dolt_repo,
        "INSERT INTO entity (id, kind, name) VALUES ('ENT', 'item', 'Item');"
        "INSERT INTO entity_item (id, name) VALUES ('ENT', 'Item');"
        "INSERT INTO stock (id, kind, entity_id) VALUES ('STK', 'prep', 'ENT');"
        "INSERT INTO stock_prep (id, batch, source) VALUES ('STK', 'B1', 'ENT');",
    )
    reject(
        dolt_run,
        dolt_repo,
        "INSERT INTO stock (id, kind, entity_id) VALUES ('STK2', 'prep', 'ENT');"
        "INSERT INTO stock_prep (id, source) VALUES ('STK2', 'MISSING')",
        "foreign key",
    )


# ---------------------------------------------------------------------------
# Real Dolt: constraints
# ---------------------------------------------------------------------------


def test_discriminator_fk_rejects_id_in_two_kind_tables(
    full_ledger: Path, dolt_run: Any
) -> None:
    _insert_entity(dolt_run, full_ledger, "ENT-1", "Shared")

    # The same id cannot also be a plasmid: (id, kind) must exist in entity.
    reject(
        dolt_run,
        full_ledger,
        "INSERT INTO entity_plasmid (id, name) VALUES ('ENT-1', 'Shared plasmid')",
        "foreign key",
    )
    # The discriminator itself rejects a hand-written wrong kind.
    execute(
        dolt_run,
        full_ledger,
        "INSERT INTO entity (id, kind, name) VALUES ('ENT-2', 'plasmid', 'P')",
    )
    reject(
        dolt_run,
        full_ledger,
        "INSERT INTO entity_plasmid (id, kind, name) VALUES ('ENT-2', 'item', 'P')",
        "violat",
    )


def test_enum_check_rejects_bad_value(full_ledger: Path, dolt_run: Any) -> None:
    execute(
        dolt_run,
        full_ledger,
        "INSERT INTO entity (id, kind, name) VALUES ('E-1', 'everything', 'E')",
    )
    execute(
        dolt_run,
        full_ledger,
        "INSERT INTO entity_everything (id, name, color) VALUES ('E-1', 'E', 'blue')",
    )
    reject(
        dolt_run,
        full_ledger,
        "INSERT INTO entity_everything (id, name, color) VALUES ('E-2', 'E2', 'purple')",
        "check constraint",
    )


def test_sequence_unique_rejects_duplicate(full_ledger: Path, dolt_run: Any) -> None:
    for entity_id in ("E-1", "E-2"):
        execute(
            dolt_run,
            full_ledger,
            f"INSERT INTO entity (id, kind, name) VALUES ({literal(entity_id)}, "
            f"'everything', {literal(entity_id)})",
        )

    execute(
        dolt_run,
        full_ledger,
        "INSERT INTO entity_everything (id, name, seq) VALUES ('E-1', 'E1', 'ACGT')",
    )
    reject(
        dolt_run,
        full_ledger,
        "INSERT INTO entity_everything (id, name, seq) VALUES ('E-2', 'E2', 'ACGT')",
        "duplicate unique key",
    )

    rows = query(
        dolt_run,
        full_ledger,
        "SELECT seq_sha256 FROM entity_everything WHERE id = 'E-1'",
    )
    assert rows[0]["seq_sha256"] == hashlib.sha256(b"ACGT").hexdigest()


def test_slot_fk_rejects_slot_not_in_geometry(
    full_ledger: Path, dolt_run: Any
) -> None:
    _insert_entity(dolt_run, full_ledger, "ENT-1", "Item")
    _insert_stock(dolt_run, full_ledger, "STK-1", "ENT-1")
    execute(
        dolt_run,
        full_ledger,
        "INSERT INTO box (id, kind, name) VALUES ('BOX-1', 'grid9x9', 'WFL-08-01');"
        "INSERT INTO box_grid9x9 (id) VALUES ('BOX-1');"
        "INSERT INTO slot (box_id, label, row_label, col_label, ordinal) VALUES "
        "('BOX-1', 'A1', 'A', '1', 1);",
    )

    execute(
        dolt_run,
        full_ledger,
        "INSERT INTO aliquot (id, stock_id, seq, box_id, slot, status) VALUES "
        "('STK-1.1', 'STK-1', 1, 'BOX-1', 'A1', 'stored')",
    )
    reject(
        dolt_run,
        full_ledger,
        "INSERT INTO aliquot (id, stock_id, seq, box_id, slot, status) VALUES "
        "('STK-1.2', 'STK-1', 2, 'BOX-1', 'Z9', 'stored')",
        "foreign key",
    )
    # The status CHECK comes from the schema vocabulary.
    reject(
        dolt_run,
        full_ledger,
        "INSERT INTO aliquot (id, stock_id, seq, status) VALUES "
        "('STK-1.3', 'STK-1', 3, 'lost')",
        "check constraint",
    )


def test_slot_rows_insert_into_the_materialised_geometry(
    dolt_repo: Path, dolt_run: Any
) -> None:
    install(dolt_run, dolt_repo, schema_statements(full_schema()))
    execute(
        dolt_run,
        dolt_repo,
        "INSERT INTO box (id, kind, name) VALUES ('B', 'grid9x9', 'B');"
        "INSERT INTO box_grid9x9 (id) VALUES ('B');",
    )
    rows = slot_rows("B", full_schema().kinds["grid9x9"].geometry)
    values = ", ".join(
        "("
        + ", ".join(
            literal(row[column])
            for column in ("box_id", "label", "row_label", "col_label", "ordinal")
        )
        + ")"
        for row in rows
    )
    execute(
        dolt_run,
        dolt_repo,
        "INSERT INTO slot (box_id, label, row_label, col_label, ordinal) "
        f"VALUES {values}",
    )
    assert query(dolt_run, dolt_repo, "SELECT COUNT(*) AS n FROM slot")[0]["n"] == 81
    assert (
        query(
            dolt_run,
            dolt_repo,
            "SELECT COUNT(*) AS n FROM slot WHERE row_label = 'I'",
        )[0]["n"]
        == 0
    )
    assert (
        query(
            dolt_run,
            dolt_repo,
            "SELECT ordinal FROM slot WHERE label = 'J9'",
        )[0]["ordinal"]
        == 81
    )


# ---------------------------------------------------------------------------
# Real Dolt: views
# ---------------------------------------------------------------------------


def _seed_boxes(run: Any, repo: Path) -> None:
    execute(
        run,
        repo,
        "INSERT INTO box (id, kind, name) VALUES "
        "('B-FREEZER', 'shelf', 'Freezer-1'),"
        "('B-SHELF', 'rack', 'Shelf-2'),"
        "('B-BOX', 'grid9x9', 'WFL-08-01');"
        "INSERT INTO box_shelf (id) VALUES ('B-FREEZER');"
        "INSERT INTO box_rack (id) VALUES ('B-SHELF');"
        "INSERT INTO box_grid9x9 (id) VALUES ('B-BOX');"
        "UPDATE box SET parent_box_id = 'B-FREEZER' WHERE id = 'B-SHELF';"
        "UPDATE box SET parent_box_id = 'B-SHELF', parent_slot = 'top' "
        "WHERE id = 'B-BOX';",
    )
    schema = full_schema()
    for box_id, kind in (("B-SHELF", "rack"), ("B-BOX", "grid9x9")):
        rows = slot_rows(box_id, schema.kinds[kind].geometry)
        values = ", ".join(
            "("
            + ", ".join(
                literal(row[column])
                for column in ("box_id", "label", "row_label", "col_label", "ordinal")
            )
            + ")"
            for row in rows
        )
        execute(
            run,
            repo,
            "INSERT INTO slot (box_id, label, row_label, col_label, ordinal) "
            f"VALUES {values}",
        )


def test_views_report_location_status_and_slots(
    full_ledger: Path, dolt_run: Any
) -> None:
    _insert_entity(dolt_run, full_ledger, "ENT-1", "Item")
    _seed_boxes(dolt_run, full_ledger)
    for stock_id in ("STK-1", "STK-2", "STK-3", "STK-4", "STK-5"):
        _insert_stock(dolt_run, full_ledger, stock_id, "ENT-1")
    execute(
        dolt_run,
        full_ledger,
        "INSERT INTO aliquot (id, stock_id, seq, box_id, slot, status, quantity, unit) "
        "VALUES ('STK-1.1', 'STK-1', 1, 'B-BOX', 'A1', 'stored', 500, 'ul');"
        "INSERT INTO aliquot (id, stock_id, seq, status) VALUES "
        "('STK-2.1', 'STK-2', 1, 'pending');"
        "INSERT INTO aliquot (id, stock_id, seq, status) VALUES "
        "('STK-3.1', 'STK-3', 1, 'consumed');"
        "INSERT INTO aliquot (id, stock_id, seq, status) VALUES "
        "('STK-4.1', 'STK-4', 1, 'missing'),"
        "('STK-4.2', 'STK-4', 2, 'consumed');",
    )

    location = query(
        dolt_run,
        full_ledger,
        "SELECT aliquot_id, box_path, location FROM v_aliquot_location "
        "WHERE aliquot_id = 'STK-1.1'",
    )[0]
    assert location["box_path"] == "Freezer-1/Shelf-2/WFL-08-01"
    assert location["location"] == "Freezer-1/Shelf-2/WFL-08-01:A1"
    # JSON output omits NULL columns, so ask the question explicitly.
    assert query(
        dolt_run,
        full_ledger,
        "SELECT location IS NULL AS missing FROM v_aliquot_location "
        "WHERE aliquot_id = 'STK-2.1'",
    )[0]["missing"] in (1, True)

    statuses = {
        row["stock_id"]: row
        for row in query(
            dolt_run,
            full_ledger,
            "SELECT stock_id, status, aliquot_count, total_quantity, unit "
            "FROM v_stock_status",
        )
    }
    assert statuses["STK-1"]["status"] == "active"
    assert statuses["STK-2"]["status"] == "pending"
    assert statuses["STK-3"]["status"] == "depleted"
    assert statuses["STK-4"]["status"] == "missing"
    assert statuses["STK-5"]["status"] == "empty"
    assert statuses["STK-4"]["aliquot_count"] == 2
    assert float(statuses["STK-1"]["total_quantity"]) == 500.0
    assert statuses["STK-1"]["unit"] == "ul"

    slots = query(
        dolt_run,
        full_ledger,
        "SELECT slot, occupant_type, occupant_id, aliquot_id, entity_id, "
        "occupant_type IS NULL AS is_empty "
        "FROM v_box_slots WHERE box_id = 'B-BOX' ORDER BY ordinal",
    )
    assert len(slots) == 81  # every slot of the geometry, empties included
    assert slots[0]["slot"] == "A1"
    assert slots[0]["occupant_type"] == "aliquot"
    assert slots[0]["aliquot_id"] == "STK-1.1"
    assert slots[0]["entity_id"] == "ENT-1"
    assert slots[1]["slot"] == "A2"
    assert slots[1]["is_empty"] in (1, True)
    assert "occupant_type" not in slots[1]  # NULL keys are omitted by Dolt JSON

    child = query(
        dolt_run,
        full_ledger,
        "SELECT slot, occupant_type, occupant_id, child_box_name "
        "FROM v_box_slots WHERE box_id = 'B-SHELF' AND slot = 'top'",
    )[0]
    assert child["occupant_type"] == "box"
    assert child["occupant_id"] == "B-BOX"
    assert child["child_box_name"] == "WFL-08-01"

    item = query(
        dolt_run,
        full_ledger,
        "SELECT id, kind, name FROM v_entity_item WHERE id = 'ENT-1'",
    )[0]
    assert item == {"id": "ENT-1", "kind": "item", "name": "Item"}


@pytest.fixture
def molbio_ledger(dolt_repo: Path, dolt_run: Any) -> Path:
    """A real Dolt repo with the bundled molbio DDL installed."""
    install(dolt_run, dolt_repo, schema_statements(molbio_schema()))
    return dolt_repo


def test_stock_view_column_list_matches_spec_3(
    molbio_ledger: Path, dolt_run: Any
) -> None:
    """Verify bullet 4: the stock view ends with [kind fields…] + the new columns."""
    columns = [
        str(row["name"])
        for row in query(
            dolt_run,
            molbio_ledger,
            "SELECT column_name AS name FROM information_schema.columns "
            "WHERE table_schema = DATABASE() "
            "AND table_name = 'v_stock_bacterial_glycerol' "
            "ORDER BY ordinal_position",
        )
    ]
    assert columns[-8:] == [*STOCK_VIEW_COLUMNS, "alias_list"]


def test_stock_view_shows_entity_aliquots_quantity_and_status(
    molbio_ledger: Path, dolt_run: Any
) -> None:
    """Verify bullet 1: counts, stored quantity/unit, derived status, entity."""
    execute(
        dolt_run,
        molbio_ledger,
        "INSERT INTO entity (id, kind, name) VALUES ('E-1', 'bacterial_strain', 'DH5a');"
        "INSERT INTO entity_bacterial_strain (id, name) VALUES ('E-1', 'DH5a');"
        "INSERT INTO stock (id, kind, entity_id) VALUES "
        "('S1', 'bacterial_glycerol', 'E-1'),"
        "('S2', 'bacterial_glycerol', 'E-1'),"
        "('S3', 'bacterial_glycerol', 'E-1');"
        "INSERT INTO stock_bacterial_glycerol (id, strain) VALUES "
        "('S1', 'DH5a'), ('S2', 'BL21'), ('S3', 'TOP10');"
        "INSERT INTO aliquot (id, stock_id, seq, status, quantity, unit) VALUES "
        "('S1.1', 'S1', 1, 'stored', 1, 'ml'),"
        "('S1.2', 'S1', 2, 'stored', 0.5, 'ml'),"
        "('S1.3', 'S1', 3, 'consumed', NULL, NULL),"
        "('S2.1', 'S2', 1, 'stored', 1, 'ml'),"
        "('S2.2', 'S2', 2, 'stored', 2, 'ul');",
    )

    row = query(
        dolt_run,
        molbio_ledger,
        "SELECT id, entity_kind, entity_name, aliquots_stored, aliquots_total, "
        "quantity_stored, unit, status FROM v_stock_bacterial_glycerol "
        "WHERE id = 'S1'",
    )[0]
    assert row["entity_kind"] == "bacterial_strain"
    assert row["entity_name"] == "DH5a"
    assert row["aliquots_stored"] == 2
    assert row["aliquots_total"] == 3
    assert float(row["quantity_stored"]) == 1.5
    assert row["unit"] == "ml"
    assert row["status"] == "active"

    # Mixed units: both the sum and the unit are NULL, never a wrong total.
    mixed = query(
        dolt_run,
        molbio_ledger,
        "SELECT quantity_stored IS NULL AS q_null, unit IS NULL AS unit_null "
        "FROM v_stock_bacterial_glycerol WHERE id = 'S2'",
    )[0]
    assert mixed["q_null"] in (1, True)
    assert mixed["unit_null"] in (1, True)

    # A stock with no aliquots: counts 0, no quantity, derived status 'empty'.
    empty = query(
        dolt_run,
        molbio_ledger,
        "SELECT aliquots_stored, aliquots_total, quantity_stored, unit, status "
        "FROM v_stock_bacterial_glycerol WHERE id = 'S3'",
    )[0]
    assert empty["aliquots_stored"] == 0
    assert empty["aliquots_total"] == 0
    assert "quantity_stored" not in empty  # NULL keys are omitted by Dolt JSON
    assert "unit" not in empty
    assert empty["status"] == "empty"

    # v_stock_status keeps its own (all-aliquot) semantics.
    status = query(
        dolt_run,
        molbio_ledger,
        "SELECT stock_id, status, total_quantity, unit FROM v_stock_status "
        "ORDER BY stock_id",
    )
    assert status[0]["status"] == "active"
    assert float(status[0]["total_quantity"]) == 1.5
    assert status[0]["unit"] == "ml"


def test_views_expose_aggregated_alias_lists(
    full_ledger: Path, dolt_run: Any
) -> None:
    """Every object view shows its aliases, sorted, ``;``-joined; NULL if none."""
    _insert_entity(dolt_run, full_ledger, "ENT-1", "Item")
    _insert_entity(dolt_run, full_ledger, "ENT-2", "Other")
    _seed_boxes(dolt_run, full_ledger)
    _insert_stock(dolt_run, full_ledger, "STK-1", "ENT-1")
    _insert_stock(dolt_run, full_ledger, "STK-2", "ENT-2")
    execute(
        dolt_run,
        full_ledger,
        "INSERT INTO aliquot (id, stock_id, seq, box_id, slot, status) "
        "VALUES ('STK-1.1', 'STK-1', 1, 'B-BOX', 'A1', 'stored');"
        "INSERT INTO aliquot (id, stock_id, seq, status) "
        "VALUES ('STK-2.1', 'STK-2', 1, 'pending');"
        # Insert aliases out of order: the views must sort them.
        "INSERT INTO alias (object_type, object_id, alias) VALUES "
        "('entity', 'ENT-1', 'E-b'), ('entity', 'ENT-1', 'E-a'),"
        "('stock', 'STK-1', 'S-2'), ('stock', 'STK-1', 'S-1'),"
        "('aliquot', 'STK-1.1', 'tube-2'), ('aliquot', 'STK-1.1', 'tube-1'),"
        "('box', 'B-BOX', 'box-b'), ('box', 'B-BOX', 'box-a');",
    )

    entity = query(
        dolt_run,
        full_ledger,
        "SELECT alias_list FROM v_entity_item WHERE id = 'ENT-1'",
    )[0]
    assert entity["alias_list"] == "E-a;E-b"

    # A stock's aliases are on its kind view and on the derived-status view.
    stock_view = query(
        dolt_run,
        full_ledger,
        "SELECT alias_list FROM v_stock_bacterial WHERE id = 'STK-1'",
    )[0]
    assert stock_view["alias_list"] == "S-1;S-2"
    status = query(
        dolt_run,
        full_ledger,
        "SELECT stock_id, alias_list FROM v_stock_status ORDER BY stock_id",
    )
    assert status[0]["alias_list"] == "S-1;S-2"
    # Dolt JSON omits NULL keys, so ask about the alias-less stock explicitly.
    missing = query(
        dolt_run,
        full_ledger,
        "SELECT alias_list IS NULL AS no_alias FROM v_stock_status "
        "WHERE stock_id = 'STK-2'",
    )[0]
    assert missing["no_alias"] in (1, True)
    missing_entity = query(
        dolt_run,
        full_ledger,
        "SELECT alias_list IS NULL AS no_alias FROM v_entity_item "
        "WHERE id = 'ENT-2'",
    )[0]
    assert missing_entity["no_alias"] in (1, True)

    # Aliquot aliases are the old tube labels after a renumber.
    location = query(
        dolt_run,
        full_ledger,
        "SELECT alias_list FROM v_aliquot_location WHERE aliquot_id = 'STK-1.1'",
    )[0]
    assert location["alias_list"] == "tube-1;tube-2"

    slots = {
        row["slot"]: row
        for row in query(
            dolt_run,
            full_ledger,
            "SELECT slot, occupant_type, occupant_alias_list "
            "FROM v_box_slots WHERE box_id = 'B-BOX' AND slot IN ('A1', 'A2')",
        )
    }
    assert slots["A1"]["occupant_alias_list"] == "tube-1;tube-2"
    # An empty slot has no occupant at all (NULL keys are omitted by Dolt JSON).
    assert "occupant_type" not in slots["A2"]
    assert "occupant_alias_list" not in slots["A2"]
    # The child box carries its own aliases in the parent's slot.
    child = query(
        dolt_run,
        full_ledger,
        "SELECT occupant_type, occupant_alias_list FROM v_box_slots "
        "WHERE box_id = 'B-SHELF' AND slot = 'top'",
    )[0]
    assert child["occupant_type"] == "box"
    assert child["occupant_alias_list"] == "box-a;box-b"
    empty = query(
        dolt_run,
        full_ledger,
        "SELECT occupant_alias_list IS NULL AS no_alias FROM v_box_slots "
        "WHERE box_id = 'B-BOX' AND slot = 'A2'",
    )[0]
    assert empty["no_alias"] in (1, True)

    columns = {
        row["view_name"]: row["alias_column"]
        for row in query(
            dolt_run,
            full_ledger,
            "SELECT table_name AS view_name, column_name AS alias_column "
            "FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name LIKE 'v\\_%' "
            "AND column_name LIKE '%alias%'",
        )
    }
    assert columns["v_entity_item"] == "alias_list"
    assert columns["v_stock_bacterial"] == "alias_list"
    assert columns["v_stock_status"] == "alias_list"
    assert columns["v_aliquot_location"] == "alias_list"
    assert columns["v_box_slots"] == "occupant_alias_list"


def test_view_kind_projects_kind_fields_and_hides_shadowed_core_name(
    full_ledger: Path, dolt_run: Any
) -> None:
    execute(
        dolt_run,
        full_ledger,
        "INSERT INTO entity (id, kind, name) VALUES ('E-1', 'everything', 'core');"
        "INSERT INTO entity_everything (id, name, code, seq) VALUES "
        "('E-1', 'kind', 'ABC', 'acgt');",
    )
    row = query(
        dolt_run,
        full_ledger,
        "SELECT id, kind, name, code, display FROM v_entity_everything",
    )[0]
    # The kind table's `name` wins; the core `name` is not duplicated.
    assert row["name"] == "kind"
    assert row["display"] == "kind-ABC"
    columns = {
        item["name"]
        for item in query(
            dolt_run,
            full_ledger,
            "SELECT column_name AS name FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = 'v_entity_everything'",
        )
    }
    # sha256 columns are an internal detail, not a view column.
    assert "seq_sha256" not in columns
    assert "name" in columns


def test_construct_view_keeps_aliases_field_and_alias_list_apart(
    dolt_repo: Path, dolt_run: Any
) -> None:
    """A kind's own ``aliases`` field and the generated ``alias_list`` coexist.

    The bundled ``protein_construct`` kind has a domain field ``aliases``
    (alternative construct names); the aggregated alias-table column is called
    ``alias_list`` so both can be projected by one view.
    """
    schema = load_schema(
        LedgerConfig(name="molbio", presets=[str(REPO_PRESETS / "molbio.yaml")])
    )
    install(dolt_run, dolt_repo, schema_statements(schema))
    execute(
        dolt_run,
        dolt_repo,
        "INSERT INTO entity (id, kind, name) "
        "VALUES ('C-1', 'protein_construct', 'c1');"
        "INSERT INTO entity_protein_construct (id, name, aliases) "
        "VALUES ('C-1', 'c1', 'old-name');"
        "INSERT INTO alias (object_type, object_id, alias) "
        "VALUES ('entity', 'C-1', 'C-OLD');",
    )
    row = query(
        dolt_run,
        dolt_repo,
        "SELECT aliases, alias_list FROM v_entity_protein_construct "
        "WHERE id = 'C-1'",
    )[0]
    assert row["aliases"] == "old-name"
    assert row["alias_list"] == "C-OLD"


# ---------------------------------------------------------------------------
# Real Dolt: custom status vocabulary
# ---------------------------------------------------------------------------


def test_custom_status_vocabulary_is_used_by_the_check(
    dolt_repo: Path, dolt_run: Any
) -> None:
    schema = full_schema(
        statuses=["pending", "stored", "missing", "consumed", "quarantined"]
    )
    install(dolt_run, dolt_repo, schema_statements(schema))

    _insert_entity(dolt_run, dolt_repo, "ENT-1", "Item")
    _insert_stock(dolt_run, dolt_repo, "STK-1", "ENT-1")
    execute(
        dolt_run,
        dolt_repo,
        "INSERT INTO aliquot (id, stock_id, seq, status) "
        "VALUES ('STK-1.1', 'STK-1', 1, 'quarantined')",
    )
    reject(
        dolt_run,
        dolt_repo,
        "INSERT INTO aliquot (id, stock_id, seq, status) "
        "VALUES ('STK-1.2', 'STK-1', 2, 'lost')",
        "check constraint",
    )


# This module intentionally keeps the embedded-driver path covered.
pytestmark = pytest.mark.embedded
