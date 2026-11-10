"""``tm parent`` / ``tm child``: relations over the existing reference columns.

Tetramark-ka8.1, the implementation ticket of wave Tetramark-ka8 (settled
product design, user-approved 2026-11-08).  Everything runs against the real
``dolt`` binary:

* a bare call reads and never writes;
* two refs plus ``--set`` makes the first (parent form) or second (child form)
  argument the child, in one UPDATE inside one lock + one Dolt commit;
* ``--clear`` removes a relation (parent form only);
* a pair served by more than one field needs ``--via`` (the shipped error text
  is asserted with the mouse ``dam``/``sire``/``source_aliquot`` case);
* cycles over boxes, over the stock graph (``parent_stock`` +
  ``source_aliquot``) and along one kind ref field are rejected;
* for these two commands **both** ``--dry-run`` and ``--no-commit`` preview the
  intended SQL and write nothing, while every other command keeps the global
  ``--no-commit`` behaviour.
"""

from __future__ import annotations

import json
import pytest
from typer.testing import CliRunner

from tetramark.cli.app import app
from tetramark.core.ledger import open as open_ledger
from tetramark.errors import LedgerError

from conftest import REPO_PRESETS

MOLBIO = REPO_PRESETS / "molbio.yaml"
MOUSE = REPO_PRESETS / "mouse.yaml"

runner = CliRunner()


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, dolt_env: dict[str, str]) -> None:
    """Run the CLI with the same isolated Dolt/Git environment as the ledger."""
    for key, value in dolt_env.items():
        monkeypatch.setenv(key, value)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def invoke(ledger, *args: str, json_output: bool = False):
    argv = ["--ledger", str(ledger.path), *args]
    if json_output:
        argv.append("--json")
    return runner.invoke(app, argv)


def ok(ledger, *args: str) -> str:
    result = invoke(ledger, *args)
    assert result.exit_code == 0, (
        f"tm {' '.join(args)} failed ({result.exit_code}):\n{result.output}\n"
        f"{result.exception!r}"
    )
    return result.output


def ok_json(ledger, *args: str):
    return json.loads(ok(ledger, *args, "--json"))


def fails(ledger, *args: str, code: int) -> str:
    result = invoke(ledger, *args)
    # The in-process runner maps an uncaught LedgerError/UsageError to exit 1;
    # the real process exit code is the exception's own exit_code (see
    # tm main()).  Both are asserted here.
    actual = getattr(result.exception, "exit_code", None)
    if actual is None:
        actual = result.exit_code
    assert actual == code, (
        f"tm {' '.join(args)} exited {actual} (expected {code}):\n"
        f"{result.output}\n{result.exception!r}"
    )
    # The in-process Typer runner reports LedgerError/UsageError as the
    # exception, not on stdout (main() is the process entry point that prints
    # it).  Every assertion below looks at the message, so join the two.
    text = result.output
    exception = result.exception
    if exception is not None and not isinstance(exception, SystemExit):
        text += f"\n{exception}"
    return text


def commit_count(ledger) -> int:
    return int(ledger.driver.query("SELECT COUNT(*) AS n FROM dolt_log")[0]["n"])


def dirty(ledger) -> list[dict[str, object]]:
    return ledger.driver.query("SELECT * FROM dolt_status")


def row(ledger, table: str, object_id: str) -> dict[str, object]:
    rows = ledger.driver.query(
        f"SELECT * FROM `{table}` WHERE `id` = %s", (object_id,)
    )
    assert len(rows) == 1, f"{table} {object_id} not found"
    return rows[0]


def aliases(ledger, object_type: str, object_id: str) -> list[str]:
    return [
        str(item["alias"])
        for item in ledger.driver.query(
            "SELECT `alias` FROM `alias` WHERE `object_type` = %s "
            "AND `object_id` = %s ORDER BY `alias`",
            (object_type, object_id),
        )
    ]


def add_item(ledger, name: str) -> dict:
    return ok_json(ledger, "entity", "add", "item", "--name", name)


def add_lot(ledger, entity_id: str) -> dict:
    return ok_json(
        ledger, "stock", "add", "lot", "--entity", entity_id, "--set",
        "supplier=acme",
    )


def add_box(ledger, kind: str, name: str) -> dict:
    return ok_json(ledger, "box", "add", kind, name)


def last_message(ledger) -> str:
    rows = ledger.driver.query("SELECT `message` FROM dolt_log LIMIT 1")
    return str(rows[0]["message"])


# ---------------------------------------------------------------------------
# Matrix rows 1-3: the core columns (stock and box)
# ---------------------------------------------------------------------------


def test_core_edges_set_and_read(cli_env: None, ledger_factory) -> None:
    """stock.parent_stock, stock.source_aliquot and box.parent_box (+ --slot)."""
    ledger = ledger_factory(name="relations-core")
    entity = add_item(ledger, "E1")
    alpha = add_lot(ledger, entity["id"])
    beta = add_lot(ledger, entity["id"])
    delta = add_lot(ledger, entity["id"])

    before = commit_count(ledger)
    result = ok_json(ledger, "parent", alpha["id"], beta["id"], "--set")
    assert result["action"] == "set"
    assert result["via"] == "parent_stock"
    assert result["child"] == {
        "object": "stock", "kind": "lot", "id": alpha["id"],
    }
    assert result["parent"] == {
        "object": "stock", "kind": "lot", "id": beta["id"],
    }
    assert result["previous"] is None
    assert row(ledger, "stock", alpha["id"])["parent_stock_id"] == beta["id"]
    assert commit_count(ledger) == before + 1
    assert dirty(ledger) == []

    # source_aliquot: the parent is the tube (its kind is the stock kind).
    result = ok_json(ledger, "parent", delta["id"], beta["aliquots"][0], "--set")
    assert result["via"] == "source_aliquot"
    assert result["parent"]["object"] == "aliquot"
    assert result["parent"]["id"] == beta["aliquots"][0]
    stored = row(ledger, "stock", delta["id"])
    assert stored["source_aliquot_id"] == beta["aliquots"][0]

    # Boxes: a slotless parent takes no slot, a slotted rack requires one and
    # the slot travels with the relation.
    freezer_1 = add_box(ledger, "shelf", "FR-1")
    freezer_2 = add_box(ledger, "shelf", "FR-2")
    rack = add_box(ledger, "rack", "RACK-1")
    result = ok_json(ledger, "parent", freezer_2["id"], freezer_1["id"], "--set")
    assert result["via"] == "parent_box"
    assert result["parent"]["object"] == "box"
    assert result["slot"] is None and result["previous_slot"] is None
    stored_box = row(ledger, "box", freezer_2["id"])
    assert stored_box["parent_box_id"] == freezer_1["id"]
    assert stored_box["parent_slot"] is None

    result = ok_json(
        ledger, "parent", freezer_1["id"], rack["id"], "--set", "--slot", "top"
    )
    assert result["slot"] == "top"
    stored_box = row(ledger, "box", freezer_1["id"])
    assert stored_box["parent_box_id"] == rack["id"]
    assert stored_box["parent_slot"] == "top"

    # Reads: the parent form lists (field, parent); the child form the inverse.
    listed = ok(ledger, "parent", freezer_1["id"])
    assert "parent_box" in listed and "slot top" in listed
    children = ok(ledger, "child", rack["id"])
    assert freezer_1["id"] in children
    parent_list = ok(ledger, "parent", alpha["id"])
    assert "parent_stock" in parent_list and beta["id"] in parent_list
    filtered_parent = ok(ledger, "parent", alpha["id"], "--via", "parent_stock")
    assert "parent_stock" in filtered_parent and beta["id"] in filtered_parent
    filtered_source = ok(ledger, "parent", delta["id"], "--via", "source_aliquot")
    assert "source_aliquot" in filtered_source and beta["aliquots"][0] in filtered_source
    filtered_children = ok(ledger, "child", rack["id"], "--via", "parent_box")
    assert freezer_1["id"] in filtered_children
    assert "choose from" in fails(
        ledger, "parent", alpha["id"], "--via", "nope", code=2
    )
    assert "can point at" in fails(
        ledger, "child", rack["id"], "--via", "nope", code=2
    )


def test_parent_and_child_forms_store_the_same_relation(
    cli_env: None, ledger_factory
) -> None:
    """tm parent A B --set and tm child B A --set are one and the same write."""
    ledger = ledger_factory(name="relations-forms")
    entity = add_item(ledger, "E1")
    alpha = add_lot(ledger, entity["id"])
    beta = add_lot(ledger, entity["id"])
    gamma = add_lot(ledger, entity["id"])
    delta = add_lot(ledger, entity["id"])

    before = commit_count(ledger)
    parent_form = ok_json(ledger, "parent", alpha["id"], beta["id"], "--set")
    child_form = ok_json(ledger, "child", delta["id"], gamma["id"], "--set")
    assert parent_form["via"] == child_form["via"] == "parent_stock"
    assert parent_form["action"] == child_form["action"] == "set"
    stored_alpha = row(ledger, "stock", alpha["id"])
    stored_gamma = row(ledger, "stock", gamma["id"])
    assert stored_alpha["parent_stock_id"] == beta["id"]
    assert stored_gamma["parent_stock_id"] == delta["id"]
    assert stored_alpha["source_aliquot_id"] is None
    assert stored_gamma["source_aliquot_id"] is None
    assert commit_count(ledger) == before + 2
    assert dirty(ledger) == []


# ---------------------------------------------------------------------------
# Reference resolution and the selector
# ---------------------------------------------------------------------------


def test_ambiguous_reference_names_the_candidates(cli_env: None, ledger_factory) -> None:
    """An id prefix matching several objects is a usage error, never a pick."""
    ledger = ledger_factory(name="relations-ambiguous")
    entity = add_item(ledger, "E1")
    alpha = add_lot(ledger, entity["id"])
    beta = add_lot(ledger, entity["id"])
    prefix = "77"
    assert alpha["id"].startswith(prefix) and beta["id"].startswith(prefix)
    before = commit_count(ledger)
    output = fails(ledger, "parent", prefix, code=2)
    assert "ambiguous" in output
    assert alpha["id"] in output and beta["id"] in output
    assert commit_count(ledger) == before
    assert dirty(ledger) == []


def test_kind_ref_inferred_and_explicit_via_with_ref_cycle(
    cli_env: None, ledger_factory
) -> None:
    """A lineage ref is inferred when it is the only candidate; --via works too."""
    ledger = ledger_factory([MOLBIO], name="relations-kind")
    parent_line = ok_json(ledger, "entity", "add", "cell_line", "--name", "L1")
    child_line = ok_json(ledger, "entity", "add", "cell_line", "--name", "L2")
    third_line = ok_json(ledger, "entity", "add", "cell_line", "--name", "L3")

    # cell_line -> cell_line has exactly one relation field: parental.
    result = ok_json(ledger, "parent", child_line["id"], parent_line["id"], "--set")
    assert result["via"] == "parental"
    assert result["child"] == {
        "object": "entity", "kind": "cell_line", "id": child_line["id"],
    }
    assert result["parent"]["kind"] == "cell_line"
    assert row(ledger, "entity_cell_line", child_line["id"])["parental"] == parent_line["id"]

    result = ok_json(
        ledger, "parent", third_line["id"], parent_line["id"],
        "--set", "--via", "parental",
    )
    assert result["via"] == "parental"
    assert row(ledger, "entity_cell_line", third_line["id"])["parental"] == parent_line["id"]

    listed = ok(ledger, "parent", child_line["id"])
    assert "parental" in listed and parent_line["id"] in listed
    descendants = ok(ledger, "child", parent_line["id"])
    assert child_line["id"] in descendants and third_line["id"] in descendants

    # A loop along that same field is rejected; nothing is committed.
    before = commit_count(ledger)
    output = fails(
        ledger, "parent", parent_line["id"], child_line["id"], "--set", code=1
    )
    assert "cycle" in output
    assert parent_line["id"] in output and child_line["id"] in output
    assert commit_count(ledger) == before
    assert dirty(ledger) == []


def test_mouse_pair_with_three_candidates_needs_via(
    cli_env: None, ledger_factory
) -> None:
    """mouse_cohort -> aliquot is dam, sire and source_aliquot: --via is required."""
    ledger = ledger_factory([MOUSE], name="relations-mouse")
    strain = ok_json(ledger, "entity", "add", "mouse_strain", "--name", "C57BL/6J")
    cohort = ok_json(
        ledger, "stock", "add", "mouse_cohort", "--entity", strain["id"]
    )
    other = ok_json(
        ledger, "stock", "add", "mouse_cohort", "--entity", strain["id"]
    )
    before = commit_count(ledger)

    output = fails(
        ledger, "parent", cohort["id"], other["aliquots"][0], "--set", code=2
    )
    assert "more than one relation field" in output
    assert "dam, sire, source_aliquot" in output
    assert "pass --via FIELD" in output
    assert commit_count(ledger) == before

    result = ok_json(
        ledger, "parent", cohort["id"], other["aliquots"][0], "--set", "--via", "dam"
    )
    assert result["via"] == "dam"
    assert row(ledger, "stock_mouse_cohort", cohort["id"])["dam"] == other["aliquots"][0]
    assert commit_count(ledger) == before + 1

    result = ok_json(
        ledger, "parent", other["id"], cohort["aliquots"][0], "--set", "--via", "sire"
    )
    assert result["via"] == "sire"
    assert row(ledger, "stock_mouse_cohort", other["id"])["sire"] == cohort["aliquots"][0]
    listed = ok(ledger, "parent", cohort["id"])
    assert "dam" in listed and other["aliquots"][0] in listed


# ---------------------------------------------------------------------------
# Reads are read-only; writes are one lock + one commit
# ---------------------------------------------------------------------------


def test_reads_never_write(cli_env: None, ledger_factory) -> None:
    """Every read form leaves the row, the worktree set and dolt_log untouched."""
    ledger = ledger_factory(name="relations-reads")
    entity = add_item(ledger, "E1")
    alpha = add_lot(ledger, entity["id"])
    beta = add_lot(ledger, entity["id"])
    gamma = add_lot(ledger, entity["id"])
    orphan = add_lot(ledger, entity["id"])
    ok_json(ledger, "parent", alpha["id"], beta["id"], "--set")
    before = commit_count(ledger)
    assert dirty(ledger) == []

    ok(ledger, "parent", alpha["id"])
    ok(ledger, "child", beta["id"])
    ok(ledger, "parent", alpha["id"], beta["id"])
    ok(ledger, "child", beta["id"], alpha["id"])
    checked = ok(ledger, "parent", alpha["id"], beta["id"])
    assert "parent_stock" in checked and beta["id"] in checked

    no_relation = fails(ledger, "parent", alpha["id"], gamma["id"], code=1)
    assert "no relation between" in no_relation
    no_parent = ok(ledger, "parent", orphan["id"])
    assert "no parent relations" in no_parent
    no_children = ok(ledger, "child", gamma["id"])
    assert "no child relations" in no_children
    empty_json = ok_json(ledger, "parent", orphan["id"])
    assert empty_json == {"relations": []}

    assert commit_count(ledger) == before
    assert dirty(ledger) == []


def test_one_write_is_one_commit_with_an_ascii_parent_message(
    cli_env: None, ledger_factory
) -> None:
    ledger = ledger_factory(name="relations-commit")
    entity = add_item(ledger, "E1")
    alpha = add_lot(ledger, entity["id"])
    beta = add_lot(ledger, entity["id"])

    before = commit_count(ledger)
    ok_json(ledger, "parent", alpha["id"], beta["id"], "--set")
    assert commit_count(ledger) == before + 1
    message = last_message(ledger)
    assert message.startswith("parent(stock): ")
    assert message.isascii()
    assert alpha["id"] in message and beta["id"] in message
    assert "parent_stock" in message
    assert dirty(ledger) == []


def test_clear_rules(cli_env: None, ledger_factory) -> None:
    """--clear removes the single set field, needs --via when several are set."""
    ledger = ledger_factory(name="relations-clear")
    entity = add_item(ledger, "E1")
    alpha = add_lot(ledger, entity["id"])
    beta = add_lot(ledger, entity["id"])
    gamma = add_lot(ledger, entity["id"])

    ok_json(ledger, "parent", alpha["id"], beta["id"], "--set")
    ok_json(ledger, "parent", alpha["id"], gamma["aliquots"][0], "--set")
    before = commit_count(ledger)
    output = fails(ledger, "parent", alpha["id"], "--clear", code=2)
    assert "more than one parent relation set" in output
    assert "parent_stock, source_aliquot" in output
    assert "--via FIELD" in output
    assert commit_count(ledger) == before

    result = ok_json(ledger, "parent", alpha["id"], "--clear", "--via", "parent_stock")
    assert result["action"] == "clear"
    assert result["via"] == "parent_stock"
    assert result["parent"] is None
    assert result["previous"] == {
        "object": "stock", "kind": "lot", "id": beta["id"],
    }
    assert row(ledger, "stock", alpha["id"])["parent_stock_id"] is None
    assert row(ledger, "stock", alpha["id"])["source_aliquot_id"] == gamma["aliquots"][0]
    assert commit_count(ledger) == before + 1

    # Two refs: --clear only when that ref is the current parent.
    ok_json(ledger, "parent", alpha["id"], beta["id"], "--set")
    before = commit_count(ledger)
    output = fails(ledger, "parent", alpha["id"], gamma["id"], "--clear", code=1)
    assert "no relation between" in output
    assert commit_count(ledger) == before
    result = ok_json(ledger, "parent", alpha["id"], beta["id"], "--clear")
    assert result["previous"]["id"] == beta["id"]
    assert row(ledger, "stock", alpha["id"])["parent_stock_id"] is None

    # A box clear resets parent_box_id AND parent_slot in the same UPDATE.
    freezer_1 = add_box(ledger, "shelf", "FR-1")
    freezer_2 = add_box(ledger, "shelf", "FR-2")
    rack = add_box(ledger, "rack", "RACK-1")
    ok_json(ledger, "parent", freezer_2["id"], freezer_1["id"], "--set")
    ok_json(
        ledger, "parent", freezer_1["id"], rack["id"], "--set", "--slot", "bottom"
    )
    before = commit_count(ledger)
    result = ok_json(ledger, "parent", freezer_1["id"], "--clear")
    assert result["via"] == "parent_box"
    assert result["previous"]["id"] == rack["id"]
    assert result["previous_slot"] == "bottom"
    assert result["parent"] is None and result["slot"] is None
    stored = row(ledger, "box", freezer_1["id"])
    assert stored["parent_box_id"] is None and stored["parent_slot"] is None
    assert commit_count(ledger) == before + 1

    # Clearing nothing is a read-only no-op: exit 0 and no commit.
    before = commit_count(ledger)
    out = ok(ledger, "parent", alpha["id"], "--clear", "--via", "parent_stock")
    assert "no parent relations" in out
    assert commit_count(ledger) == before
    assert dirty(ledger) == []


# ---------------------------------------------------------------------------
# Usage errors and structural violations
# ---------------------------------------------------------------------------


def test_usage_errors(cli_env: None, ledger_factory) -> None:
    ledger = ledger_factory(name="relations-usage")
    entity = add_item(ledger, "E1")
    alpha = add_lot(ledger, entity["id"])
    beta = add_lot(ledger, entity["id"])

    assert "--set and --clear cannot be combined" in fails(
        ledger, "parent", alpha["id"], beta["id"], "--set", "--clear", code=2
    )
    child_clear = fails(ledger, "child", beta["id"], "--clear", code=2)
    assert "tm parent" in child_clear and "--clear" in child_clear
    child_clear_named = fails(ledger, "child", beta["id"], alpha["id"], "--clear", code=2)
    assert alpha["id"] in child_clear_named
    assert "two refs" in fails(ledger, "parent", alpha["id"], "--set", code=2)
    assert "two refs" in fails(ledger, "child", beta["id"], "--set", code=2)
    assert "--slot" in fails(
        ledger, "parent", alpha["id"], beta["id"], "--set", "--slot", "1", code=2
    )
    assert "--slot" in fails(ledger, "parent", alpha["id"], beta["id"], "--slot", "1", code=2)
    assert "--slot" in fails(
        ledger, "parent", alpha["id"], "--clear", "--slot", "1", code=2
    )
    unknown_via = fails(ledger, "parent", alpha["id"], beta["id"], "--set", "--via", "nope", code=2)
    assert "choose from" in unknown_via and "parent_stock" in unknown_via
    qualified = fails(
        ledger, "parent", alpha["id"], beta["id"], "--set", "--via",
        "stock.parent_stock", code=2,
    )
    assert "use --via parent_stock" in qualified


def test_structural_violations(cli_env: None, ledger_factory) -> None:
    ledger = ledger_factory(name="relations-structural")
    entity = add_item(ledger, "E1")
    alpha = add_lot(ledger, entity["id"])
    grid = add_box(ledger, "grid96", "GRID-1")
    freezer = add_box(ledger, "shelf", "FR-1")
    rack = add_box(ledger, "rack", "RACK-1")
    child_box = add_box(ledger, "shelf", "FR-2")
    other_box = add_box(ledger, "shelf", "FR-3")
    before = commit_count(ledger)

    no_field = fails(ledger, "parent", alpha["id"], entity["id"], "--set", code=1)
    assert "no relation field connects" in no_field
    mismatch = fails(
        ledger, "parent", alpha["id"], entity["id"], "--set", "--via",
        "parent_stock", code=1,
    )
    assert "do not match" in mismatch or "points at" in mismatch
    grid_parent = fails(ledger, "parent", freezer["id"], grid["id"], "--set", code=1)
    assert "grid box" in grid_parent
    missing_slot = fails(ledger, "parent", child_box["id"], rack["id"], "--set", code=1)
    assert "--slot is required" in missing_slot
    ok_json(ledger, "parent", child_box["id"], rack["id"], "--set", "--slot", "middle")
    occupied = fails(
        ledger, "parent", other_box["id"], rack["id"], "--set", "--slot", "middle", code=1
    )
    assert "already holds child box" in occupied
    assert commit_count(ledger) == before + 1, "only the successful set committed"
    assert dirty(ledger) == []


def test_stock_and_box_cycles_are_rejected(cli_env: None, ledger_factory) -> None:
    ledger = ledger_factory(name="relations-cycles")
    entity = add_item(ledger, "E1")
    first = add_lot(ledger, entity["id"])
    second = add_lot(ledger, entity["id"])
    third = add_lot(ledger, entity["id"])

    before = commit_count(ledger)
    self_parent = fails(ledger, "parent", first["id"], first["id"], "--set", code=1)
    assert "cycle" in self_parent and first["id"] in self_parent
    assert commit_count(ledger) == before

    ok_json(ledger, "parent", first["id"], second["id"], "--set")
    two_cycle_before = commit_count(ledger)
    output = fails(ledger, "parent", second["id"], first["id"], "--set", code=1)
    assert "cycle" in output
    assert first["id"] in output and second["id"] in output
    assert commit_count(ledger) == two_cycle_before
    assert dirty(ledger) == []

    # The mixed graph: third -> first via its source aliquot, then first -> third.
    ok_json(ledger, "parent", third["id"], first["aliquots"][0], "--set")
    mixed_before = commit_count(ledger)
    output = fails(ledger, "parent", first["id"], third["id"], "--set", code=1)
    assert "cycle" in output
    assert first["id"] in output and third["id"] in output
    assert commit_count(ledger) == mixed_before

    freezer_1 = add_box(ledger, "shelf", "FR-1")
    freezer_2 = add_box(ledger, "shelf", "FR-2")
    ok_json(ledger, "parent", freezer_1["id"], freezer_2["id"], "--set")
    before = commit_count(ledger)
    output = fails(ledger, "parent", freezer_2["id"], freezer_1["id"], "--set", code=1)
    assert "cycle" in output.lower() or "inside" in output
    assert commit_count(ledger) == before


# ---------------------------------------------------------------------------
# --dry-run / --no-commit: preview the SQL and write nothing (spec 6.7 (i))
# ---------------------------------------------------------------------------


def test_dry_run_and_no_commit_write_nothing(cli_env: None, ledger_factory) -> None:
    ledger = ledger_factory(name="relations-preview")
    entity = add_item(ledger, "E1")
    alpha = add_lot(ledger, entity["id"])
    beta = add_lot(ledger, entity["id"])
    gamma = add_lot(ledger, entity["id"])
    before = commit_count(ledger)
    assert dirty(ledger) == []

    # Each flag, both command forms, for a set and for a clear.
    for flag in ("--dry-run", "--no-commit"):
        for args in (
            ("parent", alpha["id"], beta["id"], "--set"),
            ("child", beta["id"], alpha["id"], "--set"),
        ):
            out = ok(ledger, flag, *args)
            assert "UPDATE" in out and "parent_stock_id" in out, out
            assert row(ledger, "stock", alpha["id"])["parent_stock_id"] is None
            assert commit_count(ledger) == before
            assert dirty(ledger) == []

    # A real write, then previews of an overwrite and of a clear.
    ok_json(ledger, "parent", alpha["id"], beta["id"], "--set")
    after_set = commit_count(ledger)
    for flag in ("--dry-run", "--no-commit"):
        out = ok(ledger, flag, "parent", alpha["id"], gamma["id"], "--set")
        assert "UPDATE" in out and gamma["id"] in out
        assert row(ledger, "stock", alpha["id"])["parent_stock_id"] == beta["id"]
        assert commit_count(ledger) == after_set
        out = ok(ledger, flag, "parent", alpha["id"], "--clear")
        assert "UPDATE" in out and "parent_stock_id" in out
        assert row(ledger, "stock", alpha["id"])["parent_stock_id"] == beta["id"]
        assert commit_count(ledger) == after_set
        assert dirty(ledger) == []

    # The preview JSON carries the intended result: previous and new parent.
    preview = ok_json(
        ledger, "--dry-run", "parent", alpha["id"], gamma["id"], "--set"
    )
    assert preview["dry_run"] is True
    assert preview["result"]["via"] == "parent_stock"
    assert preview["result"]["previous"]["id"] == beta["id"]
    assert preview["result"]["parent"]["id"] == gamma["id"]

    # The facade follows the same command-specific rule.
    preview_ledger = open_ledger(ledger.path, no_commit=True)
    try:
        facade_result = preview_ledger.relations.set_parent(alpha["id"], gamma["id"])
        assert facade_result["previous"]["id"] == beta["id"]
        assert preview_ledger.last_write is not None
        assert preview_ledger.last_write.dry_run is True
        assert row(ledger, "stock", alpha["id"])["parent_stock_id"] == beta["id"]
        assert commit_count(ledger) == after_set
        assert dirty(ledger) == []
    finally:
        preview_ledger.close()

    # The global --no-commit behaviour of every other command is unchanged.
    counts = {
        "select": int(
            ledger.driver.query("SELECT COUNT(*) AS n FROM `entity`")[0]["n"]
        )
    }
    ok(ledger, "--no-commit", "entity", "add", "item", "--name", "E-NOCOMMIT")
    assert int(
        ledger.driver.query("SELECT COUNT(*) AS n FROM `entity`")[0]["n"]
    ) == counts["select"] + 1
    assert commit_count(ledger) == after_set


# ---------------------------------------------------------------------------
# Re-parenting keeps the identity; the JSON shapes report previous + new
# ---------------------------------------------------------------------------


def test_reparenting_keeps_id_aliases_and_lineage(
    cli_env: None, ledger_factory
) -> None:
    ledger = ledger_factory(name="relations-reparent")
    entity = add_item(ledger, "E1")
    alpha = add_lot(ledger, entity["id"])
    beta = add_lot(ledger, entity["id"])
    gamma = add_lot(ledger, entity["id"])
    ok(ledger, "alias", "add", "stock", alpha["id"], "cultures")

    ok_json(ledger, "parent", alpha["id"], beta["id"], "--set")
    before = commit_count(ledger)
    result = ok_json(ledger, "parent", alpha["id"], gamma["id"], "--set")
    assert result["previous"] == {
        "object": "stock", "kind": "lot", "id": beta["id"],
    }
    assert result["parent"]["id"] == gamma["id"]
    assert row(ledger, "stock", alpha["id"])["id"] == alpha["id"]
    assert aliases(ledger, "stock", alpha["id"]) == ["cultures"]
    assert ok_json(ledger, "parent", "cultures")["relations"][0]["parent"]["id"] == gamma["id"]
    lineage = ok(ledger, "lineage", alpha["id"])
    assert gamma["id"] in lineage
    assert beta["id"] not in lineage
    assert commit_count(ledger) == before + 1


def test_facade_round_trip(cli_env: None, ledger_factory) -> None:
    """ledger.relations: get_parent / set_parent / clear_parent round trip."""
    ledger = ledger_factory(name="relations-facade")
    entity = add_item(ledger, "E1")
    alpha = add_lot(ledger, entity["id"])
    beta = add_lot(ledger, entity["id"])
    before = commit_count(ledger)

    result = ledger.relations.set_parent(alpha["id"], beta["id"])
    assert result["via"] == "parent_stock"
    assert ledger.relations.get_parent(alpha["id"])["relations"][0]["parent"]["id"] == beta["id"]
    assert ledger.relations.get_children(beta["id"])["relations"][0]["child"]["id"] == alpha["id"]
    assert ledger.relations.get_relation(alpha["id"], beta["id"])["relations"][0]["via"] == "parent_stock"

    cleared = ledger.relations.clear_parent(alpha["id"])
    assert cleared["action"] == "clear" and cleared["previous"]["id"] == beta["id"]
    assert ledger.relations.get_parent(alpha["id"])["relations"] == []
    assert ledger.relations.get_children(beta["id"])["relations"] == []
    with pytest.raises(LedgerError, match="no relation between"):
        ledger.relations.get_relation(alpha["id"], beta["id"])
    assert commit_count(ledger) == before + 2
    assert dirty(ledger) == []
