"""CLI coverage for the object command groups (ticket 10)."""

from __future__ import annotations

import json
import sys

import pytest
from typer.testing import CliRunner

from tetramark.cli.app import app, main
from tetramark.core.ledger import open as open_ledger

from conftest import PRESETS

BASE = PRESETS / "base"


@pytest.fixture
def ledger_factory(fast_ledger_factory):
    """Use the template-copying factory for this module (faster tests)."""
    return fast_ledger_factory


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, dolt_env: dict[str, str]) -> None:
    """Run the CLI with the same isolated Dolt/Git environment as the ledger."""
    for key, value in dolt_env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("TETRAMARK_WORKSPACE", raising=False)


def commit_count(ledger) -> int:
    return int(ledger.driver.query("SELECT COUNT(*) AS n FROM dolt_log")[0]["n"])


def dirty(ledger) -> list[dict[str, object]]:
    return ledger.driver.query("SELECT * FROM dolt_status")


def base_args(ledger) -> list[str]:
    return [
        "--workspace",
        str(ledger.workspace.data_dir),
        "--ledger",
        ledger.name,
    ]


def invoke(ledger, *args: str):
    return CliRunner().invoke(app, [*base_args(ledger), *args])


def write(ledger, *args: str):
    before = commit_count(ledger)
    result = invoke(ledger, *args)
    assert result.exit_code == 0, result.output
    assert commit_count(ledger) == before + 1
    return result


def read(ledger, *args: str):
    result = invoke(ledger, *args)
    assert result.exit_code == 0, result.output
    return result


def test_cli_entity_stock_aliquot_box_alias_flow(
    cli_env: None, ledger_factory
) -> None:
    ledger = ledger_factory([BASE], name="cli", id_prefix="77")

    created = json.loads(
        write(ledger, "entity", "add", "item", "--name", "alpha", "--json").output
    )
    entity_id = created["id"]
    assert created["kind"] == "item" and created["name"] == "alpha"

    shown = json.loads(read(ledger, "entity", "show", entity_id, "--json").output)
    assert shown["name"] == "alpha"
    assert shown["id"] == entity_id

    edited = json.loads(
        write(
            ledger, "entity", "edit", entity_id, "--set", "category=lab", "--json"
        ).output
    )
    assert edited["category"] == "lab"

    listed = json.loads(read(ledger, "entity", "ls", "item", "--json").output)
    assert [row["id"] for row in listed] == [entity_id]
    filtered = json.loads(
        read(ledger, "entity", "ls", "item", "--where", "category=lab", "--json").output
    )
    assert [row["id"] for row in filtered] == [entity_id]

    box = json.loads(write(ledger, "box", "add", "grid96", "BOX", "--json").output)
    assert box["slots"] == 96
    shelves = json.loads(
        write(
            ledger,
            "box",
            "add",
            "grid96",
            "FREEZER",
            "--json",
        ).output
    )
    moved_box = json.loads(
        write(
            ledger,
            "box",
            "move",
            "BOX",
            "--in",
            "FREEZER",
            "--slot",
            "A1",
            "--json",
        ).output
    )
    assert moved_box["parent_box_id"] == shelves["id"]
    boxes = json.loads(read(ledger, "box", "ls", "--json").output)
    paths = {row["name"]: row["path"] for row in boxes}
    assert paths["BOX"] == "FREEZER/BOX"

    stock = json.loads(
        write(
            ledger,
            "stock",
            "add",
            "lot",
            "--entity",
            entity_id,
            "--aliquots",
            "2",
            "--box",
            "BOX",
            "--slot",
            "A1",
            "--slot",
            "A2",
            "--quantity",
            "100",
            "--unit",
            "ul",
            "--json",
        ).output
    )
    assert len(stock["aliquots"]) == 2
    first, second = stock["aliquots"]

    shown_stock = json.loads(
        read(ledger, "stock", "show", stock["id"], "--json").output
    )
    assert len(shown_stock["aliquots"]) == 2

    moved = json.loads(
        write(
            ledger,
            "aliquot",
            "move",
            first,
            "--box",
            "BOX",
            "--slot",
            "B1",
            "--json",
        ).output
    )
    assert moved["slot"] == "B1"

    used = json.loads(
        write(
            ledger,
            "aliquot",
            "use",
            second,
            "40",
            "ul",
            "--purpose",
            "prep",
            "--json",
        ).output
    )
    assert used["quantity"] == "60.000000"
    assert used["status"] == "stored"

    status = json.loads(
        write(ledger, "aliquot", "status", first, "missing", "--json").output
    )
    assert status["status"] == "missing"

    alias = json.loads(
        write(ledger, "alias", "add", "entity", entity_id, "E1", "--json").output
    )
    assert alias["alias"] == "E1"
    assert json.loads(read(ledger, "entity", "show", "E1", "--json").output)["id"] == entity_id
    write(ledger, "alias", "rm", "entity", "E1")

    purged = json.loads(
        write(ledger, "aliquot", "purge", second, "--json").output
    )
    assert purged["purged"] is True
    assert dirty(ledger) == []


def test_cli_dry_run_prints_sql_and_writes_nothing(
    cli_env: None, ledger_factory
) -> None:
    ledger = ledger_factory([BASE], name="cli-dry", id_prefix="77")
    before = commit_count(ledger)

    result = invoke(
        ledger, "--dry-run", "entity", "add", "item", "--name", "planned"
    )
    assert result.exit_code == 0, result.output
    assert "INSERT INTO `entity`" in result.output
    assert commit_count(ledger) == before
    assert dirty(ledger) == []

    as_json = invoke(
        ledger, "--dry-run", "entity", "add", "item", "--name", "planned2", "--json"
    )
    assert as_json.exit_code == 0, as_json.output
    payload = json.loads(as_json.output)
    assert payload["dry_run"] is True
    assert "INSERT INTO `entity`" in payload["sql"]
    assert commit_count(ledger) == before
    assert dirty(ledger) == []


def test_cli_no_commit_executes_without_committing(
    cli_env: None, ledger_factory
) -> None:
    ledger = ledger_factory([BASE], name="cli-nocommit", id_prefix="77")
    before = commit_count(ledger)
    result = invoke(
        ledger, "--no-commit", "entity", "add", "item", "--name", "uncommitted"
    )
    assert result.exit_code == 0, result.output
    assert commit_count(ledger) == before
    assert dirty(ledger)


def test_cli_business_error_exits_1(cli_env: None, ledger_factory) -> None:
    ledger = ledger_factory([BASE], name="cli-business", id_prefix="77")
    assert invoke(ledger, "box", "add", "grid96", "DUP").exit_code == 0
    duplicate = invoke(ledger, "box", "add", "grid96", "DUP")
    assert duplicate.exit_code == 1


def test_cli_global_json_flag_applies_to_writes_and_reads(
    cli_env: None, ledger_factory
) -> None:
    ledger = ledger_factory([BASE], name="cli-global-json", id_prefix="77")
    created = json.loads(
        invoke(ledger, "--json", "entity", "add", "item", "--name", "gamma").output
    )
    assert created["name"] == "gamma"
    listed = json.loads(invoke(ledger, "--json", "entity", "ls", "item").output)
    assert [row["id"] for row in listed] == [created["id"]]


# ---------------------------------------------------------------------------
# Branch support (TM-03)
# ---------------------------------------------------------------------------


def test_cli_global_branch_selects_the_session_branch(
    cli_env: None, ledger_factory
) -> None:
    ledger = ledger_factory([BASE], name="cli-branch", id_prefix="77")
    ledger.driver.execute("CALL DOLT_BRANCH(%s)", ("feature",))
    before = commit_count(ledger)

    result = invoke(
        ledger, "-b", "feature", "entity", "add", "item", "--name", "branch-item"
    )
    assert result.exit_code == 0, result.output

    # Nothing changed on the checked-out branch; the write and commit are on
    # the session branch only.
    assert ledger.entities.ls() == []
    assert commit_count(ledger) == before
    branch = open_ledger(ledger.workspace, ledger.name, branch="feature")
    try:
        assert [row["name"] for row in branch.entities.ls()] == ["branch-item"]
        assert ledger.driver.query("SELECT active_branch() AS `b`") == [
            {"b": "main"}
        ]
    finally:
        branch.close()


def test_cli_branch_dry_run_names_the_target_branch(
    cli_env: None, ledger_factory
) -> None:
    ledger = ledger_factory([BASE], name="cli-branch-dry", id_prefix="77")
    ledger.driver.execute("CALL DOLT_BRANCH(%s)", ("feature",))
    before = commit_count(ledger)

    result = invoke(
        ledger,
        "-b",
        "feature",
        "--dry-run",
        "entity",
        "add",
        "item",
        "--name",
        "planned",
    )
    assert result.exit_code == 0, result.output
    assert "-- target branch: feature" in result.output
    assert "INSERT INTO `entity`" in result.output
    assert commit_count(ledger) == before

    as_json = invoke(
        ledger,
        "-b",
        "feature",
        "--dry-run",
        "entity",
        "add",
        "item",
        "--name",
        "planned2",
        "--json",
    )
    assert as_json.exit_code == 0, as_json.output
    payload = json.loads(as_json.output)
    assert payload["branch"] == "feature"
    assert "INSERT INTO `entity`" in payload["sql"]


def test_cli_branch_ls_create_and_delete_roundtrip(
    cli_env: None, ledger_factory
) -> None:
    ledger = ledger_factory([BASE], name="cli-branch-cmd", id_prefix="77")

    listed = invoke(ledger, "branch", "ls")
    assert listed.exit_code == 0, listed.output
    assert "main" in listed.output and "* main" in listed.output

    created = invoke(ledger, "branch", "create", "feature", "--from", "main")
    assert created.exit_code == 0, created.output
    names = [
        entry["name"]
        for entry in json.loads(invoke(ledger, "--json", "branch", "ls").output)
    ]
    assert {"main", "feature"} <= set(names)

    deleted = invoke(ledger, "branch", "delete", "feature")
    assert deleted.exit_code == 0, deleted.output
    names = [
        entry["name"]
        for entry in json.loads(invoke(ledger, "--json", "branch", "ls").output)
    ]
    assert "feature" not in names

    # An unmerged branch is refused by Dolt, then deletable once merged.
    assert invoke(ledger, "branch", "create", "unmerged").exit_code == 0
    branch = open_ledger(ledger.workspace, ledger.name, branch="unmerged")
    try:
        branch.entities.add("item", name="unmerged-row")
    finally:
        branch.close()
    refused = invoke(ledger, "branch", "delete", "unmerged")
    assert refused.exit_code == 1
    assert "not fully merged" in str(refused.exception)

    ledger.driver.execute("CALL DOLT_MERGE(%s)", ("unmerged",))
    assert invoke(ledger, "branch", "delete", "unmerged").exit_code == 0


def test_remote_push_b_still_names_the_remote_branch(
    cli_env: None, ledger_factory, tmp_path
) -> None:
    """Q-08: the subcommand's -b wins over the global -b (different meanings)."""
    ledger = ledger_factory([BASE], name="cli-remote-b", id_prefix="77")
    ledger.driver.execute("CALL DOLT_BRANCH(%s)", ("feature",))
    added = invoke(ledger, "remote", "add", "origin", str(tmp_path / "remote"))
    assert added.exit_code == 0, added.output

    pushed = invoke(
        ledger,
        "-b",
        "feature",
        "--dry-run",
        "remote",
        "push",
        "-b",
        "remote-only",
        "origin",
    )
    assert pushed.exit_code == 0, pushed.output
    assert "DOLT_PUSH('origin', 'remote-only')" in pushed.output

    # Without the subcommand option the session branch is pushed.
    session = invoke(
        ledger, "-b", "feature", "--dry-run", "remote", "push", "origin"
    )
    assert session.exit_code == 0, session.output
    assert "DOLT_PUSH('origin', 'feature')" in session.output


def test_cli_unknown_field_exits_usage(
    cli_env: None,
    ledger_factory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ledger = ledger_factory([BASE], name="cli-usage", id_prefix="77")
    args = [
        "tm",
        *base_args(ledger),
        "entity",
        "add",
        "item",
        "--name",
        "probe",
        "--set",
        "nope=1",
    ]
    monkeypatch.setattr(sys, "argv", args)
    with pytest.raises(SystemExit) as excinfo:
        main()
    assert excinfo.value.code == 2
    assert "valid fields" in capsys.readouterr().err
