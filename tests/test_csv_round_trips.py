"""Round-trip budget and byte identity for the batched CSV reads.

``tm export`` used to run one ``dolt sql`` process per read: the alias table
(and again for ``aliases.csv``), one per entity kind, one per box kind, one per
stock kind, the two aliquot name maps and the aliquot rows -- each with a second
``-r csv`` process whenever it returned rows.  ``tm import`` planned against the
same shape: six identity reads, five placement reads, one read per unique field
and up to three more per unresolved reference, every one its own process.

Both paths are one ``Driver.query_many`` batch now (one ``-r json`` process for
every statement plus one ``-r csv`` pass for the column names).  These tests pin
the budget by counting ``EmbeddedDriver._invoke`` calls, and they pin the
*result* by exporting the same ledger through the batched path and through the
historical per-statement path (``Driver.query_many`` loops ``query``): the file
bytes have to be identical.  Real Dolt, never a mock, like the rest of the suite.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path
from typing import Any

import pytest

from conftest import PRESETS
from tetramark.core.ledger import Ledger
from tetramark.driver import Driver, EmbeddedDriver
from tetramark.errors import LedgerError
from tetramark.ops.io_csv import CsvImportError, export_csv, import_csv

#: These tests pin budgets by counting the embedded driver's ``dolt sql``
#: processes; the opt-in server backend has no such per-statement process, so
#: ``tests/conftest.py`` skips them when ``TETRAMARK_DRIVER=server``.
pytestmark = pytest.mark.embedded_driver

#: ``full`` has every field type and every geometry, so the export fans out
#: over a dozen views and the import over a dozen unique-field reads: the shape
#: that made the old per-read fan-out dominate.
FULL = [str(PRESETS / "full")]

#: Every table an import can write (the per-kind tables are added per schema).
CORE_TABLES = ["entity", "stock", "box", "slot", "aliquot", "alias", "aliquot_event"]


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def invoke_log(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every result format passed to ``EmbeddedDriver._invoke`` from now on."""
    calls: list[str] = []
    original = EmbeddedDriver._invoke

    def counting(self: EmbeddedDriver, sql: str, result_format: str) -> str:
        calls.append(result_format)
        return original(self, sql, result_format)

    monkeypatch.setattr(EmbeddedDriver, "_invoke", counting)
    return calls


@pytest.fixture
def write_boundary(
    monkeypatch: pytest.MonkeyPatch, invoke_log: list[str]
) -> list[int]:
    """The read count at the moment each write session was opened."""
    starts: list[int] = []
    original = Ledger.write

    def counting(self: Ledger, *args: Any, **kwargs: Any) -> Any:
        starts.append(len(invoke_log))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Ledger, "write", counting)
    return starts


def make_source(ledger: Ledger) -> None:
    """A small but broad data set covering every exported file and column."""
    first = ledger.entities.add(
        "everything",
        name="alpha",
        values={"code": "AAA", "seq": "ACGT", "count": "3"},
        alias=["Alpha", "A"],
    )
    second = ledger.entities.add(
        "everything",
        name="beta",
        values={"code": "BBB", "parent": first["id"]},
        alias="B",
    )
    item = ledger.entities.add(
        "item", name="widget", values={"category": "reagent"}, alias="W"
    )
    plate = ledger.boxes.add("grid96", "PLATE")
    shelf = ledger.boxes.add("shelf", "SHELF")
    named = ledger.boxes.add("namedbox", "NAMED")
    ledger.boxes.move("PLATE", parent="SHELF")
    ledger.aliases.add("box", shelf["id"], "S")
    prep = ledger.stocks.add(
        "prep",
        entity=first["id"],
        values={"batch": "B1"},
        aliquots=2,
        box="PLATE",
        slots=["A1", "A2"],
        quantity="500",
        unit="ul",
    )
    ledger.stocks.add(
        "lot", entity=item["id"], values={"supplier": "acme"}, aliquots=0
    )
    ledger.stocks.add(
        "prep",
        entity=second["id"],
        values={"batch": "B2"},
        aliquots=1,
        parent=prep["id"],
        source_aliquot=prep["aliquots"][0],
        box="NAMED",
        slots=["top"],
        quantity="1",
        unit="ml",
    )
    ledger.aliases.add("stock", prep["id"], "S1")
    ledger.aliases.add("aliquot", prep["aliquots"][1], "TUBE")
    assert plate["id"] and named["id"]


def export_bytes(directory: Path) -> dict[str, str]:
    """The name and sha256 of every file an export wrote."""
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.iterdir())
    }


def use_per_query_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``query_many`` run one ``driver.query`` per statement (the old path)."""
    monkeypatch.setattr(EmbeddedDriver, "query_many", Driver.query_many)


def fail_first_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the next ``query_many`` fail, as one missing table in a batch does.

    The planner then re-reads one query at a time *and* keeps reference
    resolution on the live queries, which is the historical path.  The caller
    opens the ledger first: the planner's snapshot is the first batch after it.
    """
    original = EmbeddedDriver.query_many
    state = {"failed": False}

    def failing(self: EmbeddedDriver, statements: Any, **kwargs: Any) -> Any:
        if not state["failed"]:
            state["failed"] = True
            raise LedgerError("simulated: one read in the batch failed")
        return original(self, statements, **kwargs)

    monkeypatch.setattr(EmbeddedDriver, "query_many", failing)


def write_csv(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8", newline="")
    return path


def import_error(ledger: Ledger, path: Path) -> str:
    """The message a rejected import reports."""
    with pytest.raises(CsvImportError) as excinfo:
        import_csv(ledger, "everything", path)
    return str(excinfo.value)


# ---------------------------------------------------------------------------
# Budget: export
# ---------------------------------------------------------------------------


def test_export_all_reads_every_file_in_one_batch(
    ledger_factory: Any, tmp_path: Path, invoke_log: list[str]
) -> None:
    """An ``export all`` is one JSON read batch plus one CSV header batch."""
    ledger = ledger_factory(FULL, name="export-batch")
    make_source(ledger)
    invoke_log.clear()

    exported = export_csv(ledger, "all", out_dir=tmp_path / "export")

    assert invoke_log == ["json", "csv"], (
        f"export issued {len(invoke_log)} dolt processes: {invoke_log}"
    )
    # Every file the preset can write is still written, in the same shape.
    assert sorted(path.name for path in exported.files) == sorted(
        [
            "everything.csv",
            "item.csv",
            "plasmid.csv",
            "boxes.csv",
            "bacterial.csv",
            "lot.csv",
            "prep.csv",
            "aliquots.csv",
            "aliases.csv",
        ]
    )


def test_export_one_kind_is_one_batch(
    ledger_factory: Any, tmp_path: Path, invoke_log: list[str]
) -> None:
    """Even a single-kind export shares its one read with the alias map."""
    ledger = ledger_factory(FULL, name="export-one-batch")
    make_source(ledger)
    invoke_log.clear()

    export_csv(ledger, "item", out_dir=tmp_path / "one")

    assert len(invoke_log) <= 3, (
        f"export item issued {len(invoke_log)} dolt processes: {invoke_log}"
    )


# ---------------------------------------------------------------------------
# Budget: import
# ---------------------------------------------------------------------------


def test_planning_one_import_runs_one_read_batch(
    ledger_factory: Any,
    tmp_path: Path,
    invoke_log: list[str],
    write_boundary: list[int],
) -> None:
    """Every snapshot read the planner needs arrives in one ``query_many``.

    The planner resolves references against that snapshot instead of querying
    per row, so a clean import reads twice (JSON rows + CSV columns) and then
    writes.
    """
    source = ledger_factory(FULL, name="import-batch-source")
    make_source(source)
    exported = export_csv(source, "all", out_dir=tmp_path / "export")

    target = ledger_factory(FULL, name="import-batch-target")
    invoke_log.clear()
    write_boundary.clear()
    import_csv(target, "all", exported.directory)

    assert write_boundary, "the import must open exactly one write session"
    assert len(write_boundary) == 1, write_boundary
    planning = write_boundary[0]
    assert planning <= 2, (
        f"planning issued {planning} dolt processes before the write session: "
        f"{invoke_log[:planning]}"
    )
    # Two reads + the write session's own statements (DML, the box views, the
    # commit); the exact write count belongs to driver/ledger, not to io_csv.
    assert len(invoke_log) <= 6, (
        f"the import issued {len(invoke_log)} dolt processes: {invoke_log}"
    )


def test_dry_run_import_only_plans(
    ledger_factory: Any, tmp_path: Path, invoke_log: list[str]
) -> None:
    """A dry run writes nothing, so its whole cost is the planning batch."""
    source = ledger_factory(FULL, name="import-dry-source")
    make_source(source)
    exported = export_csv(source, "all", out_dir=tmp_path / "export")

    target = ledger_factory(FULL, name="import-dry-target")
    invoke_log.clear()
    import_csv(target, "all", exported.directory, dry_run=True)

    assert len(invoke_log) <= 2, (
        f"a dry-run import issued {len(invoke_log)} dolt processes: {invoke_log}"
    )


# ---------------------------------------------------------------------------
# Identity: the batched reads return what the per-query reads returned
# ---------------------------------------------------------------------------


def test_batched_export_writes_the_per_query_bytes(
    ledger_factory: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The export bytes must not depend on how the reads are batched.

    ``Driver.query_many`` is the historical one-``query``-per-statement path,
    i.e. exactly the reads ``export_csv`` made before it was batched.
    """
    ledger = ledger_factory(FULL, name="export-bytes")
    make_source(ledger)

    batched = tmp_path / "batched"
    export_csv(ledger, "all", out_dir=batched)

    with monkeypatch.context() as patched:
        use_per_query_path(patched)
        per_query = tmp_path / "per-query"
        export_csv(ledger, "all", out_dir=per_query)

    batched_digests = export_bytes(batched)
    assert batched_digests == export_bytes(per_query)
    # ... and the files carry the data, not just matching headers.
    aliases_csv = (batched / "aliases.csv").read_text(encoding="utf-8")
    assert "TUBE" in aliases_csv and "Alpha" in aliases_csv
    prep_csv = (batched / "prep.csv").read_text(encoding="utf-8")
    prep_rows = list(csv.DictReader(io.StringIO(prep_csv)))
    assert prep_rows and all(row["aliquots"] == "0" for row in prep_rows)
    boxes_csv = (batched / "boxes.csv").read_text(encoding="utf-8")
    box_rows = list(csv.DictReader(io.StringIO(boxes_csv)))
    shelf_rows = [row for row in box_rows if row["name"] == "SHELF"]
    assert shelf_rows and shelf_rows[0]["alias"] == "S"
    plate_rows = [row for row in box_rows if row["name"] == "PLATE"]
    # ``parent`` carries the parent box's id (the export writes the FK value).
    assert plate_rows and plate_rows[0]["parent"] == shelf_rows[0]["id"]


def test_batched_import_leaves_the_per_query_state(
    ledger_factory: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reference resolved from the snapshot resolves to the same row."""
    source = ledger_factory(FULL, name="import-state-source")
    make_source(source)
    exported = export_csv(source, "all", out_dir=tmp_path / "export")

    batched = ledger_factory(FULL, name="import-state-batched")
    import_csv(batched, "all", exported.directory)

    with monkeypatch.context() as patched:
        use_per_query_path(patched)
        per_query = ledger_factory(FULL, name="import-state-per-query")
        import_csv(per_query, "all", exported.directory)

    for table in CORE_TABLES:
        batched_rows = table_rows(batched, table)
        per_query_rows = table_rows(per_query, table)
        assert batched_rows == per_query_rows, table


def table_rows(ledger: Ledger, table: str) -> list[str]:
    """One table as sorted JSON, ignoring the events' wall-clock timestamp."""
    rows = []
    for row in ledger.driver.query(f"SELECT * FROM `{table}`"):
        record = dict(row)
        if table == "aliquot_event":
            record.pop("at", None)  # each import stamps its own CURRENT_TIMESTAMP
        rows.append(json.dumps(record, sort_keys=True, default=str))
    return sorted(rows)


def test_export_import_export_round_trips_the_same_bytes(
    ledger_factory: Any, tmp_path: Path
) -> None:
    """What the export writes, the import reads back byte for byte."""
    source = ledger_factory(FULL, name="round-trip-source")
    make_source(source)
    first = tmp_path / "first"
    export_csv(source, "all", out_dir=first)

    target = ledger_factory(FULL, name="round-trip-target")
    import_csv(target, "all", first)

    second = tmp_path / "second"
    export_csv(target, "all", out_dir=second)

    assert export_bytes(first) == export_bytes(second)


# ---------------------------------------------------------------------------
# Identity: the batch that could not run falls back to the per-query reads
# ---------------------------------------------------------------------------


def test_a_failing_batch_falls_back_to_the_per_query_reads(
    ledger_factory: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One failing read must not change the import, only its speed.

    The batch cannot skip a statement the way the individual reads skip one
    (a table the schema defines but the database does not have yet), so the
    planner re-reads everything one query at a time -- and resolves references
    against the live queries again instead of the snapshot.
    """
    source = ledger_factory(FULL, name="fallback-source")
    make_source(source)
    exported = export_csv(source, "all", out_dir=tmp_path / "export")

    batched = ledger_factory(FULL, name="fallback-batched")
    import_csv(batched, "all", exported.directory)

    # Open the target first: the planner's snapshot is then the first batch.
    fallback = ledger_factory(FULL, name="fallback-per-query")
    with monkeypatch.context() as patched:
        fail_first_batch(patched)
        import_csv(fallback, "all", exported.directory)

    for table in CORE_TABLES:
        assert table_rows(batched, table) == table_rows(fallback, table), table


def test_a_bad_reference_reports_the_same_message_either_way(
    ledger_factory: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The snapshot reports what ``core.objects._resolve_ref`` reported.

    Both messages of the live resolution are pinned: the id that exists as
    another kind ("is a 'item' entity, not a 'everything'") and the one that is
    not there at all.  The per-query path is the oracle.
    """
    ledger = ledger_factory(FULL, name="ref-messages")
    item = ledger.entities.add("item", name="widget")
    unknown = write_csv(
        tmp_path / "unknown.csv", "name,code,parent\nchild,AAA,no-such-thing\n"
    )
    wrong_kind = write_csv(
        tmp_path / "wrong-kind.csv", f"name,code,parent\nchild,AAA,{item['id']}\n"
    )

    for path in (unknown, wrong_kind):
        batched = import_error(ledger, path)
        with monkeypatch.context() as patched:
            fail_first_batch(patched)
            per_query = import_error(ledger, path)
        assert batched == per_query
        assert "row 1" in batched

    assert "no such id, and no alias for it" in import_error(ledger, unknown)
    assert (
        f"{item['id']!r} is a 'item' entity, not a 'everything'"
        in import_error(ledger, wrong_kind)
    )


def test_a_reference_to_a_live_row_costs_no_query(
    ledger_factory: Any,
    tmp_path: Path,
    invoke_log: list[str],
    write_boundary: list[int],
) -> None:
    """A ``ref`` field resolved from the snapshot must not hit Dolt at all."""
    ledger = ledger_factory(FULL, name="ref-live")
    parent = ledger.entities.add("everything", name="parent", values={"code": "AAA"})
    child = write_csv(
        tmp_path / "everything.csv",
        f"name,code,parent\nchild,BBB,{parent['id']}\n",
    )
    invoke_log.clear()
    write_boundary.clear()

    import_csv(ledger, "everything", child)

    assert write_boundary and write_boundary[0] <= 2, (
        f"planning issued {write_boundary[0]} dolt processes: {invoke_log}"
    )
    rows = ledger.driver.query(
        "SELECT `parent` FROM `entity_everything` WHERE `name` = 'child'"
    )
    assert rows == [{"parent": parent["id"]}]
