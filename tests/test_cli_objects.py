"""CLI coverage for the object command groups (ticket 10)."""

from __future__ import annotations

import json
import sys
from decimal import Decimal

import pytest
from typer.testing import CliRunner

from tetramark.cli.app import app, main
from tetramark.core.ledger import open as open_ledger
from tetramark.errors import LedgerError, UsageError

from conftest import PRESETS

BASE = PRESETS / "base"
BOXFIELDS = PRESETS / "boxfields"
EXTRAS = PRESETS / "extras"
FULL = PRESETS / "full"


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, dolt_env: dict[str, str]) -> None:
    """Run the CLI with the same isolated Dolt/Git environment as the ledger."""
    for key, value in dolt_env.items():
        monkeypatch.setenv(key, value)


def commit_count(ledger) -> int:
    return int(ledger.driver.query("SELECT COUNT(*) AS n FROM dolt_log")[0]["n"])


def dirty(ledger) -> list[dict[str, object]]:
    return ledger.driver.query("SELECT * FROM dolt_status")


def base_args(ledger) -> list[str]:
    return [
        "--ledger",
        str(ledger.path),
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

    listed = json.loads(read(ledger, "entity", "list", "item", "--json").output)
    assert [row["id"] for row in listed] == [entity_id]
    filtered = json.loads(
        read(ledger, "entity", "list", "item", "--where", "category=lab", "--json").output
    )
    assert [row["id"] for row in filtered] == [entity_id]

    box = json.loads(write(ledger, "box", "add", "grid96", "BOX", "--json").output)
    assert box["slots"] == 96
    shelves = json.loads(
        write(
            ledger,
            "box",
            "add",
            "shelf",
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
            "--json",
        ).output
    )
    assert moved_box["parent_box_id"] == shelves["id"]
    boxes = json.loads(read(ledger, "box", "list", "--json").output)
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
            "-m",
            "moved",
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
            "-m",
            "prep",
            "--json",
        ).output
    )
    assert used["quantity"] == "60.000000"
    assert used["status"] == "stored"
    assert used["used_amount"] == "40"
    assert used["used_unit"] == "ul"
    assert used["message"] == "prep"

    status = json.loads(
        write(ledger, "aliquot", "status", first, "missing", "--json").output
    )
    assert status["status"] == "missing"

    alias = json.loads(
        write(ledger, "alias", "add", "entity", entity_id, "E1", "--json").output
    )
    assert alias["alias"] == "E1"
    assert json.loads(read(ledger, "entity", "show", "E1", "--json").output)["id"] == entity_id
    write(ledger, "alias", "remove", "entity", "E1")

    purged = json.loads(
        write(ledger, "aliquot", "purge", second, "--json").output
    )
    assert purged["purged"] is True
    assert dirty(ledger) == []


def test_cli_alias_add_accepts_a_unique_incomplete_id(
    cli_env: None, ledger_factory
) -> None:
    """``tm alias add entity <short-id> <alias>`` works when the prefix is unique."""
    ledger = ledger_factory([BASE], name="cli-prefix", id_prefix="77")
    first = json.loads(
        write(
            ledger,
            "entity",
            "add",
            "item",
            "--name",
            "first",
            "--id",
            "aaaaaaaa-1111-4111-8111-111111111111",
            "--json",
        ).output
    )
    second = json.loads(
        write(
            ledger,
            "entity",
            "add",
            "item",
            "--name",
            "second",
            "--id",
            "aaaaaaaa-1111-4111-8111-222222222222",
            "--json",
        ).output
    )

    added = json.loads(
        write(
            ledger,
            "alias",
            "add",
            "entity",
            second["id"][:30],
            "second-old",
            "--json",
        ).output
    )
    assert added["object_id"] == second["id"]
    assert (
        json.loads(read(ledger, "entity", "show", first["id"][:26], "--json").output)["id"]
        == first["id"]
    )

    # An ambiguous prefix is a usage error (main() exits 2), names its
    # candidates and commits nothing.
    from tetramark.errors import UsageError

    before = commit_count(ledger)
    ambiguous = invoke(ledger, "alias", "add", "entity", "aaaaaaaa", "clash")
    assert isinstance(ambiguous.exception, UsageError)
    assert ambiguous.exception.exit_code == 2
    message = str(ambiguous.exception)
    assert first["id"] in message and second["id"] in message
    assert "ambiguous" in message
    assert commit_count(ledger) == before

    # An exact alias still wins over that same text as an id prefix.
    write(ledger, "alias", "add", "entity", first["id"], "aaaaaaaa")
    assert (
        json.loads(read(ledger, "entity", "show", "aaaaaaaa", "--json").output)["id"]
        == first["id"]
    )


def test_cli_alias_add_accepts_a_box_name(cli_env: None, ledger_factory) -> None:
    """``tm alias add box <box name> <alias>`` resolves the box's human name."""
    ledger = ledger_factory([BASE], name="cli-box-name", id_prefix="77")
    box = json.loads(
        write(ledger, "box", "add", "rack", "rack-a", "--json").output
    )

    added = json.loads(
        write(ledger, "alias", "add", "box", "rack-a", "ra", "--json").output
    )
    assert added["object_type"] == "box"
    assert added["object_id"] == box["id"]

    shown = json.loads(read(ledger, "box", "show", "ra", "--json").output)
    assert shown["id"] == box["id"]
    assert shown["aliases"] == ["ra"]


def test_cli_show_prints_sorted_aliases_in_json_and_table(
    cli_env: None, ledger_factory
) -> None:
    """spec v2: entity/stock/box show expose the object's aliases (§6)."""
    ledger = ledger_factory([BASE], name="cli-show-aliases", id_prefix="77")

    entity = json.loads(
        write(
            ledger,
            "entity",
            "add",
            "item",
            "--name",
            "alpha",
            "--alias",
            "zeta",
            "--alias",
            "alpha-old",
            "--json",
        ).output
    )
    entity_id = entity["id"]
    shown_entity = json.loads(
        read(ledger, "entity", "show", entity_id, "--json").output
    )
    assert shown_entity["aliases"] == ["alpha-old", "zeta"]
    entity_table = read(ledger, "entity", "show", entity_id)
    assert "aliases" in entity_table.output
    assert "alpha-old, zeta" in entity_table.output

    stock = json.loads(
        write(ledger, "stock", "add", "lot", "--entity", entity_id, "--json").output
    )
    write(ledger, "alias", "add", "stock", stock["id"], "old-lot")
    write(ledger, "alias", "add", "stock", stock["id"], "lot-old")
    shown_stock = json.loads(
        read(ledger, "stock", "show", stock["id"], "--json").output
    )
    assert shown_stock["aliases"] == ["lot-old", "old-lot"]
    stock_table = read(ledger, "stock", "show", stock["id"])
    assert "lot-old, old-lot" in stock_table.output

    box = json.loads(write(ledger, "box", "add", "grid96", "BOX", "--json").output)
    write(ledger, "alias", "add", "box", box["id"], "legacy-box")
    write(ledger, "alias", "add", "box", box["id"], "AAA-box")
    shown_box = json.loads(read(ledger, "box", "show", "BOX", "--json").output)
    assert shown_box["aliases"] == ["AAA-box", "legacy-box"]
    box_table = read(ledger, "box", "show", "BOX")
    assert "aliases: AAA-box, legacy-box" in box_table.output

    # No aliases: the key is present (empty) and the table row is blank.
    plain = json.loads(write(ledger, "box", "add", "grid96", "PLAIN", "--json").output)
    assert plain["slots"] == 96
    plain_box = json.loads(read(ledger, "box", "show", "PLAIN", "--json").output)
    assert plain_box["aliases"] == []
    assert "aliases:" not in read(ledger, "box", "show", "PLAIN").output


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
    listed = json.loads(invoke(ledger, "--json", "entity", "list", "item").output)
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
    branch = open_ledger(ledger.path, branch="feature")
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


def test_cli_branch_list_create_and_delete_roundtrip(
    cli_env: None, ledger_factory
) -> None:
    ledger = ledger_factory([BASE], name="cli-branch-cmd", id_prefix="77")

    listed = invoke(ledger, "branch", "list")
    assert listed.exit_code == 0, listed.output
    assert "main" in listed.output and "* main" in listed.output

    created = invoke(ledger, "branch", "create", "feature", "--from", "main")
    assert created.exit_code == 0, created.output
    names = [
        entry["name"]
        for entry in json.loads(invoke(ledger, "--json", "branch", "list").output)
    ]
    assert {"main", "feature"} <= set(names)

    deleted = invoke(ledger, "branch", "delete", "feature")
    assert deleted.exit_code == 0, deleted.output
    names = [
        entry["name"]
        for entry in json.loads(invoke(ledger, "--json", "branch", "list").output)
    ]
    assert "feature" not in names

    # An unmerged branch is refused by Dolt, then deletable once merged.
    assert invoke(ledger, "branch", "create", "unmerged").exit_code == 0
    branch = open_ledger(ledger.path, branch="unmerged")
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


# ---------------------------------------------------------------------------
# Status CLI: creation status, transition rules and --force (spec 4)
# ---------------------------------------------------------------------------


def test_cli_box_name_and_grid_rules(cli_env: None, ledger_factory) -> None:
    """Acceptance through the CLI: unsafe names and nesting in a grid box."""
    from tetramark.errors import LedgerError, UsageError

    ledger = ledger_factory([BASE], name="cli-box-rules", id_prefix="77")
    write(ledger, "box", "add", "grid96", "G")
    write(ledger, "box", "add", "rack", "R")

    for name, match in (
        ("slots", "core view"),
        ("x" * 59, "58"),
        ("g", "differs only in case"),
    ):
        refused = invoke(ledger, "box", "add", "rack", name)
        assert isinstance(refused.exception, UsageError)
        assert refused.exception.exit_code == 2
        assert match in str(refused.exception)

    # Grid boxes hold tubes only, so a box cannot be added or moved into one.
    added_inside = invoke(ledger, "box", "add", "rack", "INNER", "--in", "G", "--slot", "A1")
    assert isinstance(added_inside.exception, LedgerError)
    assert added_inside.exception.exit_code == 1
    assert "grid boxes hold tubes only" in str(added_inside.exception)
    moved_inside = invoke(ledger, "box", "move", "R", "--in", "G", "--slot", "A1")
    assert isinstance(moved_inside.exception, LedgerError)
    assert "grid boxes hold tubes only" in str(moved_inside.exception)


def test_cli_status_creation_transition_and_force(cli_env: None, ledger_factory) -> None:
    from tetramark.errors import UsageError

    ledger = ledger_factory([BASE], name="cli-status", id_prefix="77")
    entity = json.loads(
        write(ledger, "entity", "add", "item", "--name", "a", "--json").output
    )
    # Creation accepts any status of the stock kind's set.
    stock = json.loads(
        write(
            ledger,
            "stock",
            "add",
            "lot",
            "--entity",
            entity["id"],
            "--aliquots",
            "1",
            "--status",
            "consumed",
            "--json",
        ).output
    )
    alq = stock["aliquots"][0]

    # An unforced illegal change is a business error (exit 1 via main()).
    from tetramark.errors import LedgerError

    refused = invoke(ledger, "aliquot", "status", alq, "stored")
    assert isinstance(refused.exception, LedgerError)
    assert refused.exception.exit_code == 1
    assert "final status" in str(refused.exception)

    # --force without -m/--message is a usage error (main() exits 2).
    usage = invoke(ledger, "aliquot", "status", alq, "stored", "--force")
    assert isinstance(usage.exception, UsageError)
    assert usage.exception.exit_code == 2
    assert "--force requires -m/--message" in str(usage.exception)

    # A forced change into a status that is not in stock is allowed and
    # recorded (the position is already absent).
    forced = json.loads(
        write(
            ledger,
            "aliquot",
            "status",
            alq,
            "missing",
            "--force",
            "-m",
            "found in the back of the shelf",
            "--json",
        ).output
    )
    assert forced["forced"] is True
    assert forced["status"] == "missing"

    # An in-stock status needs a position: refused with the move hint.
    placed = invoke(ledger, "aliquot", "status", alq, "stored")
    assert isinstance(placed.exception, LedgerError)
    assert placed.exception.exit_code == 1
    assert "tm aliquot move" in str(placed.exception)

    # Placing the tube restores the in-stock status through the move command.
    box = json.loads(
        write(ledger, "box", "add", "grid96", "BOX", "--json").output
    )
    moved = json.loads(
        write(
            ledger,
            "aliquot",
            "move",
            "-m",
            "moved",
            alq,
            "--box",
            "BOX",
            "--slot",
            "A1",
            "--json",
        ).output
    )
    assert moved["status"] == "stored"
    assert moved["old_status"] == "missing"
    assert moved["slot"] == "A1"
    row = ledger.driver.query(
        "SELECT `status`, `box_id`, `slot` FROM `aliquot` WHERE `id` = %s", (alq,)
    )[0]
    assert row == {"status": "stored", "box_id": box["id"], "slot": "A1"}


def test_cli_aliquot_status_clears_position(cli_env: None, ledger_factory) -> None:
    """Acceptance through the CLI: consumed clears, the event keeps the old one."""
    ledger = ledger_factory([BASE], name="cli-clear", id_prefix="77")
    entity = json.loads(
        write(ledger, "entity", "add", "item", "--name", "a", "--json").output
    )
    box = json.loads(
        write(ledger, "box", "add", "grid96", "G", "--json").output
    )
    stock = json.loads(
        write(
            ledger,
            "stock",
            "add",
            "lot",
            "--entity",
            entity["id"],
            "--aliquots",
            "1",
            "--box",
            "G",
            "--slot",
            "A1",
            "--json",
        ).output
    )
    alq = stock["aliquots"][0]

    changed = json.loads(
        write(ledger, "aliquot", "status", alq, "consumed", "--json").output
    )
    assert changed["status"] == "consumed"
    assert ledger.driver.query(
        "SELECT `box_id`, `slot` FROM `aliquot` WHERE `id` = %s", (alq,)
    ) == [{"box_id": None, "slot": None}]
    event = ledger.driver.query(
        "SELECT `event`, `from_box_id`, `from_slot`, `to_box_id`, `to_slot` "
        "FROM `aliquot_event` WHERE `aliquot_id` = %s ORDER BY `id`",
        (alq,),
    )[-1]
    assert event == {
        "event": "status",
        "from_box_id": box["id"],
        "from_slot": "A1",
        "to_box_id": None,
        "to_slot": None,
    }

    # ``consumed`` is final: a forced change to ``missing`` (not in stock) is
    # recorded without a position...
    forced = json.loads(
        write(
            ledger,
            "aliquot",
            "status",
            alq,
            "missing",
            "--force",
            "-m",
            "tube found",
            "--json",
        ).output
    )
    assert forced["status"] == "missing"
    # ...and restoring ``stored`` (a legal missing -> stored transition) needs
    # the tube placed again.
    refused = invoke(ledger, "aliquot", "status", alq, "stored")
    assert refused.exit_code == 1
    assert "tm aliquot move" in str(refused.exception)
    restored = json.loads(
        write(
            ledger,
            "aliquot",
            "move",
            "-m",
            "moved",
            alq,
            "--box",
            "G",
            "--slot",
            "A1",
            "--json",
        ).output
    )
    assert restored["status"] == "stored"


def test_cli_aliquot_status_message_option_replaces_the_old_note(
    cli_env: None, ledger_factory
) -> None:
    """--note is gone; both message spellings work and validation writes nothing."""
    ledger = ledger_factory([BASE], name="cli-status-message", id_prefix="77")
    entity = json.loads(
        write(ledger, "entity", "add", "item", "--name", "a", "--json").output
    )
    write(ledger, "box", "add", "grid96", "BOX")
    stock = json.loads(
        write(
            ledger,
            "stock",
            "add",
            "lot",
            "--entity",
            entity["id"],
            "--aliquots",
            "2",
            "--box",
            "BOX",
            "--slot",
            "A1",
            "--slot",
            "A2",
            "--json",
        ).output
    )
    first, second = stock["aliquots"]

    # The former option is gone: Click refuses it before anything is written.
    before = commit_count(ledger)
    statuses = ledger.driver.query(
        "SELECT `id`, `status` FROM `aliquot` ORDER BY `id`"
    )
    removed = invoke(ledger, "aliquot", "status", first, "missing", "--note", "x")
    assert removed.exit_code == 2
    assert "No such option" in removed.output
    assert "--note" in removed.output
    assert commit_count(ledger) == before
    assert ledger.driver.query(
        "SELECT `id`, `status` FROM `aliquot` ORDER BY `id`"
    ) == statuses

    # Both spellings work and store the trimmed text on the status event.
    short = json.loads(
        write(
            ledger,
            "aliquot",
            "status",
            first,
            "missing",
            "-m",
            "  gone  ",
            "--json",
        ).output
    )
    assert short["status"] == "missing"
    event = ledger.driver.query(
        "SELECT `message`, `forced` FROM `aliquot_event` "
        "WHERE `aliquot_id` = %s ORDER BY `id` DESC LIMIT 1",
        (first,),
    )[0]
    assert event == {"message": "gone", "forced": 0}
    long = json.loads(
        write(
            ledger,
            "aliquot",
            "status",
            second,
            "missing",
            "--message",
            "  absent  ",
            "--json",
        ).output
    )
    assert long["status"] == "missing"
    event = ledger.driver.query(
        "SELECT `message`, `forced` FROM `aliquot_event` "
        "WHERE `aliquot_id` = %s ORDER BY `id` DESC LIMIT 1",
        (second,),
    )[0]
    assert event == {"message": "absent", "forced": 0}

    # --force needs a message; a blank one is refused, and nothing is written.
    before = commit_count(ledger)
    missing = invoke(ledger, "aliquot", "status", first, "stored", "--force")
    assert isinstance(missing.exception, UsageError)
    assert missing.exception.exit_code == 2
    assert (
        "--force requires -m/--message: record why the transition rules are "
        "overridden" in str(missing.exception)
    )
    blank_force = invoke(
        ledger, "aliquot", "status", first, "stored", "--force", "-m", "   "
    )
    assert isinstance(blank_force.exception, UsageError)
    assert blank_force.exception.exit_code == 2
    assert "--force requires -m/--message" in str(blank_force.exception)
    blank = invoke(ledger, "aliquot", "status", second, "stored", "-m", "   ")
    assert isinstance(blank.exception, UsageError)
    assert blank.exception.exit_code == 2
    assert "a status message must not be empty" in str(blank.exception)
    assert commit_count(ledger) == before

    # A forced event stores the trimmed text in aliquot_event.message.
    write(ledger, "aliquot", "status", first, "consumed", "-m", "used up")
    forced = json.loads(
        write(
            ledger,
            "aliquot",
            "status",
            first,
            "missing",
            "--force",
            "-m",
            "  found again  ",
            "--json",
        ).output
    )
    assert forced["forced"] is True
    row = ledger.driver.query(
        "SELECT `message`, `forced` FROM `aliquot_event` "
        "WHERE `aliquot_id` = %s ORDER BY `id` DESC LIMIT 1",
        (first,),
    )[0]
    assert row == {"message": "found again", "forced": 1}
    record = json.loads(read(ledger, "aliquot", "show", first, "--json").output)
    last = record["events"][-1]
    assert last["message"] == "found again"
    assert last["forced"] is True


def test_cli_aliquot_move_status_option(cli_env: None, ledger_factory) -> None:
    from tetramark.errors import LedgerError

    # A legacy closure: two in-stock statuses only exist in a spec-4/5 preset.
    from conftest import PRESETS

    ledger = ledger_factory(
        [PRESETS / "legacy"],
        name="cli-move-status",
        id_prefix="77",
    )
    strain = json.loads(
        write(ledger, "entity", "add", "worm_strain", "--name", "N2", "--json").output
    )
    write(ledger, "box", "add", "grid8x12", "G")
    stock = json.loads(
        write(
            ledger,
            "stock",
            "add",
            "worm_culture",
            "--entity",
            strain["id"],
            "--aliquots",
            "1",
            "--status",
            "contaminated",
            "--json",
        ).output
    )
    alq = stock["aliquots"][0]

    # Two in-stock statuses: the bare move lists them and asks for --status.
    ambiguous = invoke(
        ledger, "aliquot", "move", alq, "--box", "G", "--slot", "A1", "-m", "moved"
    )
    assert isinstance(ambiguous.exception, LedgerError)
    assert "several in-stock statuses" in str(ambiguous.exception)
    assert "growing" in str(ambiguous.exception)
    assert "starved" in str(ambiguous.exception)

    # --status must name an in-stock status of the set.
    bad = invoke(
        ledger, "aliquot", "move", alq, "--box", "G", "--slot", "A1", "-m", "moved",
        "--status", "contaminated",
    )
    assert bad.exception.exit_code == 2
    assert "not in stock" in str(bad.exception)
    unknown = invoke(
        ledger, "aliquot", "move", alq, "--box", "G", "--slot", "A1", "-m", "moved",
        "--status", "nope",
    )
    assert unknown.exception.exit_code == 2
    assert "unknown status" in str(unknown.exception)

    restored = json.loads(
        write(
            ledger,
            "aliquot",
            "move",
            "-m",
            "moved",
            alq,
            "--box",
            "G",
            "--slot",
            "A1",
            "--status",
            "growing",
            "--json",
        ).output
    )
    assert restored["old_status"] == "contaminated"
    assert restored["status"] == "growing"


def test_cli_aliquot_move_requires_message_and_records_it(
    cli_env: None, ledger_factory
) -> None:
    """Missing/blank -m is a usage error; a normal move keeps its old line."""
    ledger = ledger_factory([BASE], name="cli-move-message", id_prefix="77")
    entity = json.loads(
        write(ledger, "entity", "add", "item", "--name", "alpha", "--json").output
    )
    write(ledger, "box", "add", "grid96", "BOX")
    stock = json.loads(
        write(
            ledger,
            "stock",
            "add",
            "lot",
            "--entity",
            entity["id"],
            "--aliquots",
            "1",
            "--json",
        ).output
    )
    alq = stock["aliquots"][0]

    missing = invoke(ledger, "aliquot", "move", alq, "--box", "BOX", "--slot", "A1")
    assert missing.exit_code == 2
    assert "--message" in missing.output

    before = commit_count(ledger)
    blank = invoke(
        ledger,
        "aliquot",
        "move",
        alq,
        "--box",
        "BOX",
        "--slot",
        "A1",
        "--message",
        " \t ",
    )
    assert blank.exception.exit_code == 2
    assert "message" in str(blank.exception)
    assert commit_count(ledger) == before

    moved = write(
        ledger,
        "aliquot",
        "move",
        alq,
        "--box",
        "BOX",
        "--slot",
        "A1",
        "-m",
        "  moved to the bench  ",
    )
    assert moved.output.strip() == (
        f"Moved aliquot {alq} to BOX:A1 (status pending -> stored)"
    )
    row = ledger.driver.query(
        "SELECT `status`, `box_id`, `slot` FROM `aliquot` WHERE `id` = %s", (alq,)
    )[0]
    assert row["status"] == "stored" and row["slot"] == "A1"

    history = json.loads(read(ledger, "history", alq, "--json").output)
    event = history["events"][-1]
    assert event["event"] == "move"
    assert event["message"] == "moved to the bench"
    assert event["to_slot"] == "A1"


# ---------------------------------------------------------------------------
# tm aliquot use (Tetramark-c4g.2)
# ---------------------------------------------------------------------------


def test_cli_aliquot_use_amount_is_optional_and_message_required(
    cli_env: None, ledger_factory
) -> None:
    """AMOUNT/UNIT come together; -m is required; the line names the amount."""
    ledger = ledger_factory([BASE], name="cli-use", id_prefix="77")
    entity = json.loads(
        write(ledger, "entity", "add", "item", "--name", "alpha", "--json").output
    )
    write(ledger, "box", "add", "grid96", "BOX")
    stock = json.loads(
        write(
            ledger,
            "stock",
            "add",
            "lot",
            "--entity",
            entity["id"],
            "--aliquots",
            "3",
            "--box",
            "BOX",
            "--slot",
            "A1",
            "--slot",
            "A2",
            "--slot",
            "A3",
            "--quantity",
            "200",
            "--unit",
            "ml",
            "--json",
        ).output
    )
    first, second, third = stock["aliquots"]

    # The message is required.
    missing = invoke(ledger, "aliquot", "use", first)
    assert missing.exit_code == 2
    assert "--message" in missing.output

    # ... and it must not be blank.
    before = commit_count(ledger)
    blank = invoke(ledger, "aliquot", "use", first, "-m", " \t ")
    assert blank.exception.exit_code == 2
    assert "message" in str(blank.exception)
    assert commit_count(ledger) == before

    # AMOUNT and UNIT come together: passing only the amount is a usage error.
    only_amount = invoke(ledger, "aliquot", "use", first, "50", "-m", "prep")
    assert only_amount.exception.exit_code == 2
    assert "must be given together" in str(only_amount.exception)
    assert commit_count(ledger) == before

    # The old flags are gone outright (no aliases).
    removed = invoke(
        ledger, "aliquot", "use", first, "50", "ml", "--purpose", "prep"
    )
    assert removed.exit_code == 2
    assert "no such option" in removed.output.lower()

    # A partial use names the amount used and ends with the message.
    partial = json.loads(
        write(
            ledger,
            "aliquot",
            "use",
            first,
            "50",
            "ml",
            "-m",
            "  prep  ",
            "--json",
        ).output
    )
    assert partial["used_amount"] == "50"
    assert partial["used_unit"] == "ml"
    assert partial["message"] == "prep"
    assert partial["quantity"] == "150.000000"
    assert partial["status"] == "stored"

    human = write(ledger, "aliquot", "use", second, "50", "ml", "-m", "prep")
    assert human.output.strip() == (
        f"Used 50 ml from {second} (stored, 150 ml left): prep"
    )

    # Without an amount everything left is used: the line says "all".
    whole = write(ledger, "aliquot", "use", third, "-m", "used it all")
    assert whole.output.strip() == (
        f"Used all 200 ml from {third} (consumed): used it all"
    )
    row = ledger.driver.query(
        "SELECT `status`, `box_id`, `slot` FROM `aliquot` WHERE `id` = %s", (third,)
    )[0]
    assert row["status"] == "consumed"
    assert row["box_id"] is None and row["slot"] is None

    history = json.loads(read(ledger, "history", third, "--json").output)
    event = history["events"][-1]
    assert event["event"] == "use"
    assert event["message"] == "used it all"
    assert event["old_status"] == "stored"
    assert event["new_status"] == "consumed"

    # A tube without a quantity is used whole; the amount is unknown.
    bare = json.loads(
        write(ledger, "aliquot", "add", stock["id"], "--json").output
    )["ids"][0]
    unknown = write(ledger, "aliquot", "use", bare, "-m", "used it all")
    assert unknown.output.strip() == (
        f"Used all of {bare} (consumed): used it all"
    )
    unknown_json = json.loads(
        write(
            ledger,
            "aliquot",
            "use",
            json.loads(
                write(ledger, "aliquot", "add", stock["id"], "--json").output
            )["ids"][0],
            "-m",
            "used it all",
            "--json",
        ).output
    )
    assert unknown_json["used_amount"] is None
    assert unknown_json["used_unit"] is None
    assert unknown_json["quantity"] is None
    assert unknown_json["status"] == "consumed"


# ---------------------------------------------------------------------------
# tm aliquot show (Tetramark-i93.3)
# ---------------------------------------------------------------------------

#: The tube's record fields, in the order the core returns them.  ``path`` gets
#: its own output line, ``events`` its own section and ``object`` is not
#: printed in the human table.
ALIQUOT_SHOW_ROWS = [
    "id",
    "stock_id",
    "seq",
    "status",
    "quantity",
    "unit",
    "label",
    "notes",
    "created_at",
    "aliases",
    "stock_kind",
    "entity_id",
    "box_id",
    "slot",
    "in_stock",
]


def make_placed_tube(ledger) -> dict:
    """Create one item and one tube at Freezer-1/Shelf-2/Box:A3 via the CLI."""
    entity = json.loads(
        write(ledger, "entity", "add", "item", "--name", "alpha", "--json").output
    )
    write(ledger, "box", "add", "shelf", "Freezer-1")
    write(ledger, "box", "add", "shelf", "Shelf-2", "--in", "Freezer-1")
    box = json.loads(
        write(ledger, "box", "add", "grid96", "Box", "--in", "Shelf-2", "--json").output
    )
    stock = json.loads(
        write(
            ledger,
            "stock",
            "add",
            "lot",
            "--entity",
            entity["id"],
            "--aliquots",
            "1",
            "--box",
            "Box",
            "--slot",
            "A3",
            "--quantity",
            "100",
            "--unit",
            "ul",
            "--json",
        ).output
    )
    return {
        "entity": entity,
        "box": box,
        "stock": stock,
        "aliquot": stock["aliquots"][0],
    }


def test_cli_aliquot_show_prints_rows_and_the_path_on_its_own_line(
    cli_env: None, ledger_factory
) -> None:
    """Text output is the show key/value table plus one ``path:`` line."""
    ledger = ledger_factory([BASE], name="cli-aliquot-show", id_prefix="77")
    made = make_placed_tube(ledger)
    alq = made["aliquot"]

    result = read(ledger, "aliquot", "show", alq)
    lines = result.output.splitlines()

    # The events section closes the output: the field block is above it.
    events_at = next(
        index for index, line in enumerate(lines) if line.startswith("events  ")
    )
    field_lines = lines[:events_at]

    # One row per record field, in the record's order: no ``path`` row (it
    # prints on its own line) and no ``object`` row (human noise).
    rows = [line for line in field_lines if line and not line.startswith("path:")]
    assert [row.split()[0] for row in rows] == ALIQUOT_SHOW_ROWS

    # The path closes the field block as its own line, in its human shape.
    assert "path: Freezer-1/Shelf-2/Box:A3" in field_lines
    assert sum(line.startswith("path:") for line in lines) == 1

    # Values: the aliquot id is never shortened; uuid ids are (git style).
    assert alq in result.output
    assert "stored" in result.output
    assert "100.000000" in result.output
    assert "in_stock" in result.output and "true" in result.output
    assert made["stock"]["id"] in result.output  # datecode id: kept whole
    assert made["entity"]["id"][:8] in result.output
    assert made["entity"]["id"] not in result.output
    assert made["box"]["id"][:8] in result.output
    assert made["box"]["id"] not in result.output


def test_cli_aliquot_show_json_is_the_core_record(
    cli_env: None, ledger_factory
) -> None:
    """JSON (and CSV) are the machine formats: full ids, path as a field."""
    ledger = ledger_factory([BASE], name="cli-aliquot-show-json", id_prefix="77")
    made = make_placed_tube(ledger)
    alq = made["aliquot"]

    record = json.loads(read(ledger, "aliquot", "show", alq, "--json").output)
    assert list(record) == sorted(record)  # the JSON is key-sorted
    assert set(record) == {
        "id",
        "stock_id",
        "seq",
        "status",
        "quantity",
        "unit",
        "label",
        "notes",
        "created_at",
        "aliases",
        "stock_kind",
        "entity_id",
        "box_id",
        "slot",
        "path",
        "in_stock",
        "object",
        "events",
    }
    assert record["id"] == alq
    assert record["object"] == "aliquot"
    assert record["stock_id"] == made["stock"]["id"]
    assert record["entity_id"] == made["entity"]["id"]
    assert record["box_id"] == made["box"]["id"]
    assert record["slot"] == "A3"
    assert record["path"] == "Freezer-1/Shelf-2/Box:A3"
    assert record["status"] == "stored"
    assert record["quantity"] == "100.000000"
    assert record["in_stock"] is True

    # The global --json flag is honoured like on every other read command.
    global_json = read(ledger, "--json", "aliquot", "show", alq)
    assert json.loads(global_json.output) == record

    # --format csv keeps the full ids and carries the path as a column, but
    # not the events: they are rows, not a cell.
    csv_output = read(ledger, "--format", "csv", "aliquot", "show", alq).output
    header = csv_output.splitlines()[0].split(",")
    assert header == sorted(set(record) - {"events"})
    assert "events" not in header
    assert "Freezer-1/Shelf-2/Box:A3" in csv_output
    assert made["box"]["id"] in csv_output


def test_cli_aliquot_show_json_carries_the_tube_events(
    cli_env: None, ledger_factory
) -> None:
    """A fresh tube: its one create event, with the record's event keys."""
    ledger = ledger_factory([BASE], name="cli-aliquot-show-events", id_prefix="77")
    made = make_placed_tube(ledger)
    alq = made["aliquot"]

    record = json.loads(read(ledger, "aliquot", "show", alq, "--json").output)

    assert len(record["events"]) == 1
    event = record["events"][0]
    assert set(event) == {
        "id",
        "event",
        "at",
        "amount",
        "unit",
        "from_box_id",
        "from_box",
        "from_slot",
        "to_box_id",
        "to_box",
        "to_slot",
        "old_status",
        "new_status",
        "forced",
        "actor",
        "message",
    }
    assert event["event"] == "create"
    assert event["amount"] == "100.000000"
    assert event["unit"] == "ul"
    assert event["from_box"] is None
    assert event["to_box"] == "Box"
    assert event["to_slot"] == "A3"
    assert event["old_status"] is None
    assert event["new_status"] == "stored"
    assert event["forced"] is False


def test_cli_aliquot_show_json_events_are_the_tubes_trail(
    cli_env: None, ledger_factory
) -> None:
    """After a move, a use and a status change the events are in order."""
    ledger = ledger_factory([BASE], name="cli-aliquot-show-trail", id_prefix="77")
    made = make_placed_tube(ledger)
    alq = made["aliquot"]
    write(ledger, "box", "add", "grid96", "BOX2")
    write(ledger, "aliquot", "move", alq, "--box", "BOX2", "--slot", "B1", "-m", "m")
    write(ledger, "aliquot", "use", alq, "50", "ul", "-m", "assay")
    write(ledger, "aliquot", "status", alq, "missing", "-m", "not found")

    record = json.loads(read(ledger, "aliquot", "show", alq, "--json").output)

    assert [event["event"] for event in record["events"]] == [
        "create",
        "move",
        "use",
        "status",
    ]
    create, move, use, status = record["events"]
    assert create["to_box"] == "Box" and create["to_slot"] == "A3"
    assert move["from_box"] == "Box" and move["from_slot"] == "A3"
    assert move["to_box"] == "BOX2" and move["to_slot"] == "B1"
    assert move["old_status"] == "stored" and move["new_status"] == "stored"
    assert use["amount"] == "50.000000" and use["unit"] == "ul"
    assert use["old_status"] == "stored" and use["new_status"] == "stored"
    assert status["from_box"] == "BOX2" and status["from_slot"] == "B1"
    assert status["old_status"] == "stored" and status["new_status"] == "missing"
    assert status["forced"] is False


def test_cli_aliquot_show_text_prints_one_line_per_event(
    cli_env: None, ledger_factory
) -> None:
    """The text output closes with ``events  N`` and one line per event."""
    ledger = ledger_factory([BASE], name="cli-aliquot-show-events-text", id_prefix="77")
    made = make_placed_tube(ledger)
    alq = made["aliquot"]
    write(ledger, "box", "add", "grid96", "BOX2")
    write(ledger, "aliquot", "move", alq, "--box", "BOX2", "--slot", "B1", "-m", "m")

    result = read(ledger, "aliquot", "show", alq)
    lines = result.output.splitlines()

    events_at = next(
        index for index, line in enumerate(lines) if line.startswith("events  ")
    )
    assert lines[events_at] == "events  2"
    event_lines = lines[events_at + 1 :]
    assert len(event_lines) == 2
    # The lines are the history renderer's own event lines: the create and
    # the move, oldest first, each naming the boxes it names.
    assert " create " in event_lines[0]
    assert "to=Box:A3" in event_lines[0]
    assert " move " in event_lines[1]
    assert "from=Box:A3" in event_lines[1]
    assert "to=BOX2:B1" in event_lines[1]


def test_cli_aliquot_list_json_items_carry_no_events(
    cli_env: None, ledger_factory
) -> None:
    """A list never reads a tube's event trail: its items have no events."""
    ledger = ledger_factory([BASE], name="cli-aliquot-list-events", id_prefix="77")
    made = make_placed_tube(ledger)
    alq = made["aliquot"]

    listed = json.loads(read(ledger, "aliquot", "list", "--json").output)

    assert [row["id"] for row in listed] == [alq]
    assert "events" not in listed[0]
    # The show record of the same tube does carry them.
    shown = json.loads(read(ledger, "aliquot", "show", alq, "--json").output)
    assert shown["events"]
    assert listed[0] == {
        key: value for key, value in shown.items() if key != "events"
    }


def test_cli_aliquot_show_resolves_an_alias(cli_env: None, ledger_factory) -> None:
    """A tube's alias is a handle, like the id: same record, aliases listed."""
    ledger = ledger_factory([BASE], name="cli-aliquot-show-alias", id_prefix="77")
    made = make_placed_tube(ledger)
    alq = made["aliquot"]
    write(ledger, "alias", "add", "aliquot", alq, "tube-1")

    text = read(ledger, "aliquot", "show", "tube-1")
    assert alq in text.output
    assert "tube-1" in text.output

    record = json.loads(read(ledger, "aliquot", "show", "tube-1", "--json").output)
    assert record["id"] == alq
    assert record["aliases"] == ["tube-1"]


def test_cli_aliquot_show_of_a_tube_not_in_stock_has_no_path_line(
    cli_env: None, ledger_factory
) -> None:
    """Nothing is printed for ``path``; the JSON field stays ``null``."""
    ledger = ledger_factory([BASE], name="cli-aliquot-show-pending", id_prefix="77")
    entity = json.loads(
        write(ledger, "entity", "add", "item", "--name", "beta", "--json").output
    )
    stock = json.loads(
        write(
            ledger,
            "stock",
            "add",
            "lot",
            "--entity",
            entity["id"],
            "--aliquots",
            "1",
            "--json",
        ).output
    )
    alq = stock["aliquots"][0]

    result = read(ledger, "aliquot", "show", alq)
    assert "path:" not in result.output
    assert "in_stock" in result.output and "false" in result.output

    record = json.loads(read(ledger, "aliquot", "show", alq, "--json").output)
    assert record["path"] is None
    assert record["box_id"] is None
    assert record["slot"] is None
    assert record["in_stock"] is False


@pytest.mark.fast
def test_cli_aliquot_show_help_names_history_and_stock_show() -> None:
    """The help points at the two other reads instead of duplicating them."""
    result = CliRunner().invoke(app, ["aliquot", "show", "-h"])
    assert result.exit_code == 0, result.output
    help_text = " ".join(result.output.split())
    assert "tm history" in help_text
    assert "tm stock show" in help_text


# ---------------------------------------------------------------------------
# Wrong-type hints (Tetramark-i93.4): the error names the other show verb
# ---------------------------------------------------------------------------


def cli_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    ledger,
    *args: str,
) -> tuple[int, str]:
    """Run ``tm`` through ``main()`` and return (exit code, stderr)."""
    monkeypatch.setattr(sys, "argv", ["tm", *base_args(ledger), *args])
    with pytest.raises(SystemExit) as excinfo:
        main()
    captured = capsys.readouterr()
    return int(excinfo.value.code or 0), captured.err


def test_cli_stock_show_of_an_aliquot_names_the_aliquot_verb(
    cli_env: None,
    ledger_factory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``tm stock show <aliquot>`` exits 1 and hints at ``tm aliquot show``."""
    ledger = ledger_factory([BASE], name="cli-hint-stock", id_prefix="77")
    item = json.loads(
        write(ledger, "entity", "add", "item", "--name", "alpha", "--json").output
    )
    stock = json.loads(
        write(
            ledger, "stock", "add", "lot", "--entity", item["id"], "--json"
        ).output
    )
    alq = stock["aliquots"][0]
    expected = (
        f"Error: unknown stock {alq!r}: {alq} is an aliquot; "
        f"try tm aliquot show {alq}"
    )

    code, err = cli_failure(monkeypatch, capsys, ledger, "stock", "show", alq)
    assert code == 1
    assert err.strip() == expected

    # --json does not change the error output: same message, same exit code.
    code, err = cli_failure(
        monkeypatch, capsys, ledger, "stock", "show", alq, "--json"
    )
    assert code == 1
    assert err.strip() == expected


def test_cli_stock_show_renders_aliquots_as_cards(
    cli_env: None, ledger_factory
) -> None:
    """Text `stock show` prints one card per tube; JSON keeps the record."""
    ledger = ledger_factory([BASE], name="cli-stock-cards", id_prefix="77")
    item = json.loads(
        write(ledger, "entity", "add", "item", "--name", "alpha", "--json").output
    )
    write(ledger, "box", "add", "grid96", "BOX")
    stock = json.loads(
        write(
            ledger,
            "stock",
            "add",
            "lot",
            "--entity",
            item["id"],
            "--aliquots",
            "1",
            "--box",
            "BOX",
            "--slot",
            "A1",
            "--quantity",
            "500",
            "--unit",
            "ul",
            "--json",
        ).output
    )
    first = stock["aliquots"][0]
    write(ledger, "aliquot", "add", stock["id"], "--count", "1")
    write(ledger, "alias", "add", "aliquot", first, "tube-A")
    ledger.driver.execute(
        "UPDATE `aliquot` SET `label` = 'first tube' WHERE `id` = %s", (first,)
    )

    text = read(ledger, "stock", "show", stock["id"]).output
    assert "aliquots  2" in text
    assert f"{first} · stored" in text
    assert "500.000000 ul" in text
    assert "label: first tube" in text
    assert "BOX:A1" in text
    assert "aliases: tube-A" in text
    assert "not in stock" in text
    # The old rendering put a JSON blob inside an `aliquots` field row.
    assert '{"id"' not in text
    assert "[{" not in text

    rows = json.loads(
        read(ledger, "stock", "show", stock["id"], "--json").output
    )["aliquots"]
    assert rows[0]["label"] == "first tube"
    assert rows[0]["path"] == "BOX:A1"
    assert rows[0]["aliases"] == ["tube-A"]
    assert rows[0]["in_stock"] is True
    assert rows[1]["in_stock"] is False
    assert rows[1]["path"] is None


def test_cli_aliquot_show_of_a_stock_names_the_stock_verb(
    cli_env: None,
    ledger_factory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``tm aliquot show <stock>`` exits 1 and hints at ``tm stock show``."""
    ledger = ledger_factory([BASE], name="cli-hint-aliquot", id_prefix="77")
    item = json.loads(
        write(ledger, "entity", "add", "item", "--name", "alpha", "--json").output
    )
    stock = json.loads(
        write(
            ledger, "stock", "add", "lot", "--entity", item["id"], "--json"
        ).output
    )
    expected = (
        f"Error: unknown aliquot {stock['id']!r}: {stock['id']} is a stock; "
        f"try tm stock show {stock['id']}"
    )

    code, err = cli_failure(
        monkeypatch, capsys, ledger, "aliquot", "show", stock["id"]
    )
    assert code == 1
    assert err.strip() == expected

    # --json does not change the error output: same message, same exit code.
    code, err = cli_failure(
        monkeypatch, capsys, ledger, "aliquot", "show", stock["id"], "--json"
    )
    assert code == 1
    assert err.strip() == expected


def test_cli_entity_and_box_rename(cli_env: None, ledger_factory) -> None:
    """`tm entity|box rename` is one commit and keeps the old name as an alias."""
    ledger = ledger_factory([BASE], name="cli-rename", id_prefix="77")
    write(ledger, "entity", "add", "item", "--name", "alpha")
    write(ledger, "box", "add", "grid96", "G1")

    # Entity refs resolve by id or alias (not name), so take the id from find.
    found = json.loads(read(ledger, "find", "alpha", "--json").output)
    entity_id = found[0]["id"]
    result = write(ledger, "entity", "rename", entity_id, "beta", "--json")
    payload = json.loads(result.output)
    assert payload["name"] == "beta"
    assert payload["old_name"] == "alpha"
    assert payload["aliases"] == ["alpha"]
    shown = json.loads(read(ledger, "entity", "show", "alpha", "--json").output)
    assert shown["name"] == "beta"

    result = write(ledger, "box", "rename", "G1", "G2")
    assert "Renamed box G1 -> G2" in result.output
    view = json.loads(read(ledger, "box", "show", "G1", "--json").output)
    assert view["box"] == "G2"
    assert view["aliases"] == ["G1"]

    refused = invoke(ledger, "box", "rename", "G2", "slots")
    assert refused.exception.exit_code == 2
    assert "core view" in str(refused.exception)


def test_cli_aliquot_list_scopes_filters_and_formats(
    cli_env: None, ledger_factory
) -> None:
    """`tm aliquot list` is the in-stock shelf view, filterable and read-only."""
    ledger = ledger_factory([BASE], name="cli-list", id_prefix="77")
    item = json.loads(
        write(ledger, "entity", "add", "item", "--name", "alpha", "--json").output
    )
    write(ledger, "box", "add", "shelf", "SH-1")
    write(ledger, "box", "add", "grid96", "BOX", "--in", "SH-1")
    stock = json.loads(
        write(
            ledger,
            "stock",
            "add",
            "lot",
            "--entity",
            item["id"],
            "--aliquots",
            "2",
            "--box",
            "BOX",
            "--slot",
            "A1",
            "--slot",
            "A2",
            "--quantity",
            "5",
            "--unit",
            "ml",
            "--json",
        ).output
    )
    first, second = stock["aliquots"]
    write(ledger, "aliquot", "status", second, "missing", "-m", "gone")
    write(ledger, "alias", "add", "aliquot", first, "tube-A")

    before = commit_count(ledger)
    listed = json.loads(read(ledger, "aliquot", "list", "--json").output)
    assert [row["id"] for row in listed] == [first]
    assert listed[0]["aliases"] == ["tube-A"]
    # A list row is the show record without its events, and the read writes
    # nothing.
    shown = json.loads(read(ledger, "aliquot", "show", first, "--json").output)
    assert listed[0] == {key: value for key, value in shown.items() if key != "events"}
    assert "events" not in listed[0]
    assert commit_count(ledger) == before

    table = read(ledger, "aliquot", "list")
    assert table.output.splitlines()[0].split() == [
        "id",
        "stock_kind",
        "status",
        "quantity",
        "unit",
        "label",
        "path",
    ]
    assert "SH-1/BOX:A1" in table.output

    csv_rows = read(ledger, "--format", "csv", "aliquot", "list").output.splitlines()
    assert csv_rows[0] == "id,stock_kind,status,quantity,unit,label,path"
    assert csv_rows[1].startswith(f"{first},lot,stored,")
    assert csv_rows[1].endswith("SH-1/BOX:A1")

    every = json.loads(read(ledger, "aliquot", "list", "--all", "--json").output)
    assert [row["id"] for row in every] == [first, second]
    left = json.loads(
        read(ledger, "aliquot", "list", "--not-in-stock", "--json").output
    )
    assert [row["id"] for row in left] == [second]
    missing = json.loads(
        read(ledger, "aliquot", "list", "--status", "missing", "--json").output
    )
    assert [row["id"] for row in missing] == [second]
    by_stock = json.loads(
        read(ledger, "aliquot", "list", stock["id"], "--json").output
    )
    assert [row["id"] for row in by_stock] == [first]
    by_kind = json.loads(
        read(ledger, "aliquot", "list", "--kind", "lot", "--json").output
    )
    assert [row["id"] for row in by_kind] == [first]
    by_box = json.loads(
        read(ledger, "aliquot", "list", "--box", "SH-1", "--json").output
    )
    assert [row["id"] for row in by_box] == [first]
    by_where = json.loads(
        read(ledger, "aliquot", "list", "--where", "seq=1", "--json").output
    )
    assert [row["id"] for row in by_where] == [first]
    assert read(ledger, "aliquot", "list", "--status", "consumed").output.strip() == (
        "(none)"
    )
    assert json.loads(
        read(ledger, "aliquot", "list", "--status", "consumed", "--json").output
    ) == []
    empty_csv = read(
        ledger, "--format", "csv", "aliquot", "list", "--status", "consumed"
    )
    assert empty_csv.output.splitlines() == [
        "id,stock_kind,status,quantity,unit,label,path"
    ]

    not_found = invoke(ledger, "aliquot", "list", "NOPE")
    assert isinstance(not_found.exception, LedgerError)
    assert not_found.exception.exit_code == 1
    # An empty ref is a miss, never a prefix of the ledger's only stock/box.
    empty_stock = invoke(ledger, "aliquot", "list", "")
    assert isinstance(empty_stock.exception, LedgerError)
    assert "unknown stock ''" in str(empty_stock.exception)
    empty_box = invoke(ledger, "aliquot", "list", "--box", "")
    assert isinstance(empty_box.exception, LedgerError)
    assert "unknown box ''" in str(empty_box.exception)
    wrong_type = invoke(ledger, "aliquot", "list", first)
    assert isinstance(wrong_type.exception, LedgerError)
    assert "is an aliquot; try tm aliquot show" in str(wrong_type.exception)
    unknown_box = invoke(ledger, "aliquot", "list", "--box", "NOPE")
    assert isinstance(unknown_box.exception, LedgerError)
    unknown_status = invoke(ledger, "aliquot", "list", "--status", "bogus")
    assert isinstance(unknown_status.exception, UsageError)
    assert unknown_status.exception.exit_code == 2
    assert "unknown status 'bogus'" in str(unknown_status.exception)
    where_status = invoke(ledger, "aliquot", "list", "--where", "status=stored")
    assert isinstance(where_status.exception, UsageError)
    assert "filter the status with --status" in str(where_status.exception)
    both_scopes = invoke(ledger, "aliquot", "list", "--all", "--not-in-stock")
    assert isinstance(both_scopes.exception, UsageError)
    assert "--all and --not-in-stock" in str(both_scopes.exception)


def _view_exists(ledger, name: str) -> bool:
    rows = ledger.driver.query(
        "SELECT table_name AS name FROM information_schema.views "
        "WHERE table_schema = DATABASE() AND LOWER(table_name) = %s",
        (name.lower(),),
    )
    return bool(rows)


def test_cli_entity_stock_box_purge(cli_env: None, ledger_factory) -> None:
    """The three purge commands: refusals, cascade, view drop and dry run."""
    ledger = ledger_factory([BASE], name="cli-purge", id_prefix="77")

    item = json.loads(
        write(ledger, "entity", "add", "item", "--name", "mistake", "--json").output
    )
    stock = json.loads(
        write(
            ledger,
            "stock",
            "add",
            "lot",
            "--entity",
            item["id"],
            "--aliquots",
            "2",
            "--json",
        ).output
    )
    write(ledger, "alias", "add", "stock", stock["id"], "old-lot")
    write(ledger, "alias", "add", "aliquot", stock["aliquots"][0], "old-tube")

    # A stock still belongs to the entity: refused, no commit, rows intact.
    before = commit_count(ledger)
    refused_entity = invoke(ledger, "entity", "purge", item["id"])
    assert isinstance(refused_entity.exception, LedgerError)
    assert refused_entity.exception.exit_code == 1
    assert "purge its stocks first" in str(refused_entity.exception)
    assert stock["id"] in str(refused_entity.exception)
    assert commit_count(ledger) == before
    assert ledger.driver.query(
        "SELECT `id` FROM `entity` WHERE `id` = %s", (item["id"],)
    )

    # A box with its view: the purge drops the view in the same commit.
    box = json.loads(write(ledger, "box", "add", "grid96", "BOX", "--json").output)
    write(ledger, "alias", "add", "box", box["id"], "b1")
    purged_box = json.loads(
        write(ledger, "box", "purge", "BOX", "--json").output
    )
    assert purged_box["object"] == "box"
    assert purged_box["kind"] == "grid96"
    assert purged_box["slots"] == 96
    assert purged_box["aliases"] == 1
    assert purged_box["purged"] is True
    assert ledger.driver.query(
        "SELECT `id` FROM `box` WHERE `id` = %s", (box["id"],)
    ) == []
    assert not _view_exists(ledger, "v_box_BOX")
    assert ledger.check().passed

    # A box that holds a tube is refused; nothing is written.
    box2 = json.loads(write(ledger, "box", "add", "rack", "RACK2", "--json").output)
    stock2 = json.loads(
        write(
            ledger,
            "stock",
            "add",
            "lot",
            "--entity",
            item["id"],
            "--box",
            "RACK2",
            "--slot",
            "top",
            "--json",
        ).output
    )
    before = commit_count(ledger)
    refused_box = invoke(ledger, "box", "purge", "RACK2")
    assert isinstance(refused_box.exception, LedgerError)
    assert "tm aliquot move" in str(refused_box.exception)
    assert commit_count(ledger) == before
    assert ledger.driver.query(
        "SELECT `id` FROM `box` WHERE `id` = %s", (box2["id"],)
    )

    # Stock purge cascades to its own tubes and their events/aliases.
    purged_stock = json.loads(
        write(ledger, "stock", "purge", stock["id"], "--json").output
    )
    assert purged_stock["object"] == "stock"
    assert purged_stock["kind"] == "lot"
    assert purged_stock["aliquots"] == 2
    assert purged_stock["events"] >= 2
    assert purged_stock["aliases"] == 1
    assert purged_stock["purged"] is True
    assert ledger.driver.query(
        "SELECT `id` FROM `aliquot` WHERE `stock_id` = %s", (stock["id"],)
    ) == []
    assert ledger.driver.query(
        "SELECT `alias` FROM `alias` WHERE "
        "(`object_type` = 'stock' AND `object_id` = %s) OR "
        "(`object_type` = 'aliquot' AND `object_id` IN (%s, %s))",
        (stock["id"], stock["aliquots"][0], stock["aliquots"][1]),
    ) == []

    text = write(ledger, "stock", "purge", stock2["id"]).output
    assert f"Purged stock {stock2['id']} (1 aliquot(s)," in text
    assert "event(s) removed)" in text

    text = write(ledger, "box", "purge", "RACK2").output
    assert text.strip() == (
        "Purged box RACK2 (3 slot(s), 0 alias(es) removed)"
    )

    # With no stock left the entity is purgeable; its alias goes with it.
    write(ledger, "alias", "add", "entity", item["id"], "old-item")
    purged_entity = json.loads(
        write(ledger, "entity", "purge", item["id"], "--json").output
    )
    assert purged_entity["object"] == "entity"
    assert purged_entity["kind"] == "item"
    assert purged_entity["aliases"] == 1
    assert purged_entity["purged"] is True
    assert ledger.driver.query(
        "SELECT `id` FROM `entity_item` WHERE `id` = %s", (item["id"],)
    ) == []

    # The entity and aliquot human lines carry their own counts.
    text_item = json.loads(
        write(ledger, "entity", "add", "item", "--name", "text-item", "--json").output
    )
    write(ledger, "alias", "add", "entity", text_item["id"], "text-item-alias")
    text = write(ledger, "entity", "purge", text_item["id"]).output
    assert text.strip() == (
        f"Purged entity item {text_item['id']} (1 alias(es) removed)"
    )

    tube_item = json.loads(
        write(ledger, "entity", "add", "item", "--name", "tube-item", "--json").output
    )
    tube_stock = json.loads(
        write(
            ledger, "stock", "add", "lot", "--entity", tube_item["id"], "--json"
        ).output
    )
    tube = tube_stock["aliquots"][0]
    write(ledger, "alias", "add", "aliquot", tube, "tube-alias")
    text = write(ledger, "aliquot", "purge", tube).output
    assert text.strip() == (
        f"Purged aliquot {tube} (1 event(s), 1 alias(es) removed)"
    )

    # Dry run prints the SQL and writes nothing.
    spare = json.loads(
        write(ledger, "entity", "add", "item", "--name", "spare", "--json").output
    )
    before = commit_count(ledger)
    dry = invoke(ledger, "--dry-run", "entity", "purge", spare["id"])
    assert dry.exit_code == 0, dry.output
    assert "DELETE FROM `entity`" in dry.output
    assert commit_count(ledger) == before
    assert ledger.driver.query(
        "SELECT `id` FROM `entity` WHERE `id` = %s", (spare["id"],)
    )


def test_cli_aliquot_label(cli_env: None, ledger_factory) -> None:
    """``tm aliquot label``: set, refuse an overwrite, --replace keeps an alias."""
    ledger = ledger_factory([BASE], name="cli-label", id_prefix="77")
    item = json.loads(
        write(ledger, "entity", "add", "item", "--name", "labelled", "--json").output
    )
    stock = json.loads(
        write(
            ledger, "stock", "add", "lot", "--entity", item["id"], "--json"
        ).output
    )
    tube = stock["aliquots"][0]

    # A tube without a label simply gets one.
    first = json.loads(write(ledger, "aliquot", "label", tube, "T-1", "--json").output)
    assert first["label"] == "T-1"
    assert first["old_label"] is None
    assert first["aliases"] == []
    assert json.loads(read(ledger, "aliquot", "show", tube, "--json").output)[
        "label"
    ] == "T-1"

    # A different label without --replace is refused; nothing is written.
    before = commit_count(ledger)
    refused = invoke(ledger, "aliquot", "label", tube, "T-2")
    assert isinstance(refused.exception, LedgerError)
    assert "--replace" in str(refused.exception)
    assert commit_count(ledger) == before

    # The same label again is a usage error.
    same = invoke(ledger, "aliquot", "label", tube, "T-1", "--replace")
    assert isinstance(same.exception, UsageError)
    assert commit_count(ledger) == before

    # --replace keeps the old label as an alias that still resolves the tube.
    text = write(ledger, "aliquot", "label", tube, "T-2", "--replace").output
    assert text.strip() == (
        f"Labelled aliquot {tube} 'T-2' (old label 'T-1' kept as an alias)"
    )
    shown = json.loads(read(ledger, "aliquot", "show", "T-1", "--json").output)
    assert shown["id"] == tube
    assert shown["label"] == "T-2"
    assert shown["aliases"] == ["T-1"]

    # Resolution by alias works for the command itself, too.
    third = json.loads(
        write(ledger, "aliquot", "label", "T-1", "T-3", "--replace", "--json").output
    )
    assert third["old_label"] == "T-2"
    assert third["aliases"] == ["T-1", "T-2"]


# ---------------------------------------------------------------------------
# tm stock aliquot (Tetramark-til)
# ---------------------------------------------------------------------------


def process_exit_code(result) -> int:
    """The exit status ``main()`` would produce for a CliRunner invocation.

    A core ``UsageError``/``LedgerError`` escapes the in-process runner as the
    exception itself, with its own ``exit_code``; Click's own usage errors
    already set ``result.exit_code``.  The command-level tests below want the
    process status, so they use this one helper.
    """
    return int(getattr(result.exception, "exit_code", result.exit_code))


def make_one_tube_stock(ledger, entity_id: str) -> dict:
    """Create a stock the same way in every case: one default tube."""
    return json.loads(
        write(ledger, "stock", "add", "lot", "--entity", entity_id, "--json").output
    )


def aliquot_count(ledger) -> int:
    return int(ledger.driver.query("SELECT COUNT(*) AS n FROM `aliquot`")[0]["n"])


def test_cli_stock_aliquot_is_the_same_write_as_aliquot_add(
    cli_env: None, ledger_factory
) -> None:
    """AMOUNT UNIT is sugar: same JSON, rows, create events and commits."""
    ledger = ledger_factory([BASE], name="cli-stock-aliquot-same", id_prefix="77")
    entity = json.loads(
        write(ledger, "entity", "add", "item", "--name", "alpha", "--json").output
    )
    write(ledger, "box", "add", "grid96", "B")
    direct_stock = make_one_tube_stock(ledger, entity["id"])
    sugared_stock = make_one_tube_stock(ledger, entity["id"])

    direct = json.loads(
        write(
            ledger,
            "aliquot",
            "add",
            direct_stock["id"],
            "-n",
            "2",
            "--quantity",
            "50",
            "--unit",
            "ul",
            "--box",
            "B",
            "--slot",
            "A2",
            "--slot",
            "A3",
            "--json",
        ).output
    )
    sugared = json.loads(
        write(
            ledger,
            "stock",
            "aliquot",
            sugared_stock["id"],
            "50",
            "ul",
            "-n",
            "2",
            "--box",
            "B",
            "--slot",
            "A4",
            "--slot",
            "A5",
            "--json",
        ).output
    )

    # The JSON documents have the same keys, and the ids the same suffixes.
    assert set(direct) == set(sugared) == {"object", "stock_id", "ids"}
    assert [alq.rsplit(".", 1)[1] for alq in direct["ids"]] == ["2", "3"]
    assert [alq.rsplit(".", 1)[1] for alq in sugared["ids"]] == ["2", "3"]

    # The rows are equal on every column except the ones that identify the
    # tube or its slot (and the timestamp).
    skip = {"id", "stock_id", "slot", "created_at"}

    def rows(ids: list[str]) -> list[dict[str, object]]:
        found = []
        for alq in ids:
            row = ledger.driver.query(
                "SELECT * FROM `aliquot` WHERE `id` = %s", (alq,)
            )[0]
            found.append({key: value for key, value in row.items() if key not in skip})
        return found

    assert rows(direct["ids"]) == rows(sugared["ids"])

    # The create events carry the same amount, unit and creation status.
    def create_events(ids: list[str]) -> list[dict[str, object]]:
        placeholders = ", ".join(["%s"] * len(ids))
        return [
            {
                "amount": row["amount"],
                "unit": row["unit"],
                "new_status": row["new_status"],
            }
            for row in ledger.driver.query(
                "SELECT `amount`, `unit`, `new_status` FROM `aliquot_event` "
                f"WHERE `aliquot_id` IN ({placeholders}) AND `event` = 'create' "
                "ORDER BY `aliquot_id`",
                tuple(ids),
            )
        ]

    assert create_events(direct["ids"]) == create_events(sugared["ids"])

    # Each command is one commit and the message names only its own stock.
    commits = [
        row["message"]
        for row in ledger.driver.query(
            "SELECT `message` FROM `dolt_log` "
            "ORDER BY `date` DESC, `commit_order` DESC LIMIT 2"
        )
    ]
    assert commits == [
        f"add(aliquot): {sugared_stock['id']} x2",
        f"add(aliquot): {direct_stock['id']} x2",
    ]


def test_cli_stock_aliquot_unplaced_is_pending_and_the_line_matches_aliquot_add(
    cli_env: None, ledger_factory
) -> None:
    """No --box: one pending tube, one commit, and add's exact human line."""
    ledger = ledger_factory([BASE], name="cli-stock-aliquot-line", id_prefix="77")
    entity = json.loads(
        write(ledger, "entity", "add", "item", "--name", "alpha", "--json").output
    )
    sugared_stock = make_one_tube_stock(ledger, entity["id"])
    direct_stock = make_one_tube_stock(ledger, entity["id"])

    before = commit_count(ledger)
    sugared = invoke(ledger, "stock", "aliquot", sugared_stock["id"], "50", "ul")
    assert sugared.exit_code == 0, sugared.output
    assert commit_count(ledger) == before + 1
    assert sugared.output.strip() == (
        f"Added 1 aliquot(s) to {sugared_stock['id']}: {sugared_stock['id']}.2"
    )

    row = ledger.driver.query(
        "SELECT `status`, `quantity`, `unit`, `box_id`, `slot` FROM `aliquot` "
        "WHERE `id` = %s",
        (f"{sugared_stock['id']}.2",),
    )[0]
    assert row["status"] == "pending"
    assert Decimal(str(row["quantity"])) == Decimal("50")
    assert row["unit"] == "ul"
    assert row["box_id"] is None and row["slot"] is None
    latest = ledger.driver.query(
        "SELECT `message` FROM `dolt_log` "
        "ORDER BY `date` DESC, `commit_order` DESC LIMIT 1"
    )[0]
    assert latest["message"] == f"add(aliquot): {sugared_stock['id']} x1"

    # The same write through `tm aliquot add` prints the same line template.
    direct = write(
        ledger,
        "aliquot",
        "add",
        direct_stock["id"],
        "--quantity",
        "50",
        "--unit",
        "ul",
    )
    assert direct.output.strip() == (
        f"Added 1 aliquot(s) to {direct_stock['id']}: {direct_stock['id']}.2"
    )
    assert sugared.output.strip().replace(sugared_stock["id"], "S") == (
        direct.output.strip().replace(direct_stock["id"], "S")
    )
    direct_row = ledger.driver.query(
        "SELECT `status`, `quantity`, `unit`, `box_id`, `slot` FROM `aliquot` "
        "WHERE `id` = %s",
        (f"{direct_stock['id']}.2",),
    )[0]
    assert direct_row == row


#: (case id, arguments after the stock): every usage error AC 4 names.
STOCK_ALIQUOT_USAGE_ERRORS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("missing-unit", ("50",)),
    ("missing-amount-and-unit", ()),
    ("amount-not-a-decimal", ("abc", "ul")),
    ("negative-amount", ("-5", "ul")),
    ("empty-unit", ("50", "")),
    ("unit-too-long", ("50", "x" * 33)),
    ("count-zero", ("50", "ul", "--count", "0")),
    ("slot-without-box", ("50", "ul", "--slot", "A1")),
)


@pytest.mark.parametrize(
    "case, args",
    STOCK_ALIQUOT_USAGE_ERRORS,
    ids=[case for case, _ in STOCK_ALIQUOT_USAGE_ERRORS],
)
def test_cli_stock_aliquot_usage_errors_exit_2_and_write_nothing(
    cli_env: None, ledger_factory, case: str, args: tuple[str, ...]
) -> None:
    """Every bad invocation is Click's/core's usage error (exit 2), no write."""
    ledger = ledger_factory(
        [BASE], name=f"cli-stock-aliquot-{case}", id_prefix="77"
    )
    entity = json.loads(
        write(ledger, "entity", "add", "item", "--name", "alpha", "--json").output
    )
    stock = make_one_tube_stock(ledger, entity["id"])
    before_commits = commit_count(ledger)
    before_aliquots = aliquot_count(ledger)

    result = invoke(ledger, "stock", "aliquot", stock["id"], *args)

    assert process_exit_code(result) == 2, result.output
    assert commit_count(ledger) == before_commits
    assert aliquot_count(ledger) == before_aliquots


def test_cli_stock_aliquot_unknown_stock_exits_1_and_writes_nothing(
    cli_env: None, ledger_factory
) -> None:
    """A stock that does not resolve is the same business error as add (exit 1)."""
    ledger = ledger_factory([BASE], name="cli-stock-aliquot-unknown", id_prefix="77")
    entity = json.loads(
        write(ledger, "entity", "add", "item", "--name", "alpha", "--json").output
    )
    make_one_tube_stock(ledger, entity["id"])
    before_commits = commit_count(ledger)
    before_aliquots = aliquot_count(ledger)

    result = invoke(ledger, "stock", "aliquot", "NOPE", "50", "ul")

    assert process_exit_code(result) == 1
    assert isinstance(result.exception, LedgerError)
    assert "NOPE" in str(result.exception)
    assert commit_count(ledger) == before_commits
    assert aliquot_count(ledger) == before_aliquots


def test_cli_stock_aliquot_accepts_amount_zero(
    cli_env: None, ledger_factory
) -> None:
    """Zero is a valid amount (core's non-negative rule stands)."""
    ledger = ledger_factory([BASE], name="cli-stock-aliquot-zero", id_prefix="77")
    entity = json.loads(
        write(ledger, "entity", "add", "item", "--name", "alpha", "--json").output
    )
    stock = make_one_tube_stock(ledger, entity["id"])

    created = json.loads(
        write(
            ledger, "stock", "aliquot", stock["id"], "0", "ul", "--json"
        ).output
    )
    assert created["ids"] == [f"{stock['id']}.2"]
    row = ledger.driver.query(
        "SELECT `quantity`, `unit` FROM `aliquot` WHERE `id` = %s",
        (created["ids"][0],),
    )[0]
    assert Decimal(str(row["quantity"])) == Decimal(0)
    assert row["unit"] == "ul"


def test_cli_stock_aliquot_dry_run_and_too_few_slots_match_aliquot_add(
    cli_env: None, ledger_factory
) -> None:
    """--dry-run prints the same SQL and writes nothing; slot errors match."""
    ledger = ledger_factory([BASE], name="cli-stock-aliquot-dry", id_prefix="77")
    entity = json.loads(
        write(ledger, "entity", "add", "item", "--name", "alpha", "--json").output
    )
    stock = make_one_tube_stock(ledger, entity["id"])
    before_commits = commit_count(ledger)
    before_aliquots = aliquot_count(ledger)

    sugared = invoke(
        ledger, "--dry-run", "stock", "aliquot", stock["id"], "50", "ul"
    )
    direct = invoke(
        ledger,
        "--dry-run",
        "aliquot",
        "add",
        stock["id"],
        "--quantity",
        "50",
        "--unit",
        "ul",
    )
    assert sugared.exit_code == 0, sugared.output
    assert direct.exit_code == 0, direct.output
    assert sugared.output == direct.output
    assert commit_count(ledger) == before_commits
    assert aliquot_count(ledger) == before_aliquots

    # A slotted box needs one --slot per aliquot, whichever command is used.
    write(ledger, "box", "add", "grid96", "G")
    before_commits = commit_count(ledger)
    before_aliquots = aliquot_count(ledger)
    sugared = invoke(
        ledger,
        "stock",
        "aliquot",
        stock["id"],
        "50",
        "ul",
        "--count",
        "2",
        "--box",
        "G",
        "--slot",
        "A1",
    )
    direct = invoke(
        ledger,
        "aliquot",
        "add",
        stock["id"],
        "--count",
        "2",
        "--quantity",
        "50",
        "--unit",
        "ul",
        "--box",
        "G",
        "--slot",
        "A1",
    )
    assert process_exit_code(sugared) == process_exit_code(direct) == 1
    assert str(sugared.exception) == str(direct.exception)
    assert commit_count(ledger) == before_commits
    assert aliquot_count(ledger) == before_aliquots


@pytest.mark.fast
def test_cli_stock_aliquot_help_names_amount_unit_and_aliquot_add() -> None:
    """The command's help shows the two positionals and names the equivalence."""
    short = CliRunner().invoke(app, ["stock", "aliquot", "-h"])
    long = CliRunner().invoke(app, ["stock", "aliquot", "--help"])
    assert short.exit_code == 0, short.output
    assert long.exit_code == 0, long.output
    assert short.output == long.output
    text = " ".join(short.output.split())
    assert "STOCK AMOUNT UNIT" in text
    assert "AMOUNT" in text and "UNIT" in text
    assert "tm aliquot add" in text


# ---------------------------------------------------------------------------
# Add: box kind fields (Tetramark-026)
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_cli_box_add_help_lists_set_long_only_and_keeps_slot_short_flag() -> None:
    """AC11: ``--set`` has no short form; ``-s`` still means ``--slot``."""
    result = CliRunner().invoke(app, ["box", "add", "--help"])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    set_line = next(line for line in lines if "--set" in line)
    slot_line = next(line for line in lines if "--slot" in line)
    assert "-s" not in set_line.replace("--set", "")
    assert "-s" in slot_line


def test_cli_box_add_sets_kind_fields_and_reports_them(
    cli_env: None, ledger_factory
) -> None:
    """AC1: ``--set`` fills the kind row; the result reports the coerced fields."""
    ledger = ledger_factory([BOXFIELDS], name="cli-box-add-fields", id_prefix="77")
    before = commit_count(ledger)
    added = json.loads(
        write(
            ledger,
            "box",
            "add",
            "reqbox",
            "R1",
            "--set",
            "label=alpha",
            "--set",
            "count=3",
            "--json",
        ).output
    )

    assert commit_count(ledger) == before + 1
    assert set(added) == {
        "object",
        "kind",
        "id",
        "name",
        "parent_box_id",
        "parent_slot",
        "slots",
        "fields",
    }
    assert added["object"] == "box"
    assert added["kind"] == "reqbox"
    assert added["name"] == "R1"
    assert added["parent_box_id"] is None and added["parent_slot"] is None
    assert added["slots"] == 0
    assert added["fields"] == {"label": "alpha", "count": 3}

    # `box show --json` is the slot view and carries no kind fields (out of
    # scope here), so the stored row and the filtered list prove the values.
    shown = json.loads(read(ledger, "box", "show", "R1", "--json").output)
    assert shown["id"] == added["id"] and shown["box"] == "R1"
    assert ledger.driver.query(
        "SELECT `label`, `count` FROM `box_reqbox` WHERE `id` = %s",
        (added["id"],),
    ) == [{"label": "alpha", "count": 3}]
    listed = json.loads(
        read(ledger, "box", "list", "--where", "label=alpha", "--json").output
    )
    assert [(row["name"], row["count"]) for row in listed] == [("R1", 3)]


def test_cli_box_add_requires_a_required_kind_field(
    cli_env: None, ledger_factory
) -> None:
    """AC2: a required field without a default must be given."""
    ledger = ledger_factory(
        [BOXFIELDS], name="cli-box-add-required", id_prefix="77"
    )
    before = commit_count(ledger)
    refused = invoke(ledger, "box", "add", "reqbox", "R2")
    assert isinstance(refused.exception, UsageError)
    assert refused.exception.exit_code == 2
    assert "missing required field(s) for kind 'reqbox': label" in str(
        refused.exception
    )
    assert commit_count(ledger) == before
    assert dirty(ledger) == []


def test_cli_box_add_refuses_unknown_computed_and_uncoercible_fields(
    cli_env: None, ledger_factory
) -> None:
    """AC3/AC4/AC5: the edit coercion messages apply to add verbatim."""
    ledger = ledger_factory([BOXFIELDS], name="cli-box-add-refusals", id_prefix="77")
    before = commit_count(ledger)
    cases = [
        (
            ("--set", "label=a", "--set", "bogus=1"),
            "unknown field(s) 'bogus' for box kind 'reqbox'; valid fields:",
        ),
        (
            ("--set", "label=a", "--set", "label_len=1"),
            "field 'label_len' is computed and cannot be set",
        ),
        (
            ("--set", "label=a", "--set", "count="),
            "field 'count' must be an integer (got '')",
        ),
    ]
    for index, (args, message) in enumerate(cases):
        refused = invoke(ledger, "box", "add", "reqbox", f"R{index + 3}", *args)
        assert isinstance(refused.exception, UsageError)
        assert refused.exception.exit_code == 2
        assert message in str(refused.exception)
    assert commit_count(ledger) == before
    assert dirty(ledger) == []


def test_cli_box_add_refuses_name_and_structural_fields(
    cli_env: None, ledger_factory
) -> None:
    """AC6/AC7: the NAME argument and placement/identity columns are not --set."""
    ledger = ledger_factory([BOXFIELDS], name="cli-box-add-guards", id_prefix="77")
    before = commit_count(ledger)

    refused = invoke(ledger, "box", "add", "reqbox", "R6", "--set", "label=a", "--set", "name=X")
    assert isinstance(refused.exception, UsageError)
    assert refused.exception.exit_code == 2
    assert str(refused.exception) == "give the box name as the NAME argument"

    for field in ("parent_box_id", "parent_slot", "id", "kind", "created_at"):
        refused = invoke(
            ledger,
            "box",
            "add",
            "reqbox",
            "R7",
            "--set",
            "label=a",
            "--set",
            f"{field}=x",
        )
        assert isinstance(refused.exception, UsageError)
        assert refused.exception.exit_code == 2
        assert "is structural" in str(refused.exception), field
    assert commit_count(ledger) == before
    assert dirty(ledger) == []


def test_cli_box_add_notes_via_set_and_twice_is_refused(
    cli_env: None, ledger_factory
) -> None:
    """AC8: ``--set notes=`` fills the notes column; both spellings are refused."""
    ledger = ledger_factory([BOXFIELDS], name="cli-box-add-notes", id_prefix="77")
    added = json.loads(
        write(
            ledger,
            "box",
            "add",
            "reqbox",
            "R7",
            "--set",
            "label=a",
            "--set",
            "notes=hi",
            "--json",
        ).output
    )
    assert ledger.driver.query(
        "SELECT `notes` FROM `box` WHERE `id` = %s", (added["id"],)
    ) == [{"notes": "hi"}]
    listed = json.loads(
        read(ledger, "box", "list", "--where", "name=R7", "--json").output
    )
    assert [row["notes"] for row in listed] == ["hi"]

    before = commit_count(ledger)
    refused = invoke(
        ledger,
        "box",
        "add",
        "reqbox",
        "R8",
        "--set",
        "label=a",
        "--notes",
        "hi",
        "--set",
        "notes=hi",
    )
    assert isinstance(refused.exception, UsageError)
    assert refused.exception.exit_code == 2
    assert "given twice" in str(refused.exception)
    assert commit_count(ledger) == before
    assert dirty(ledger) == []


def test_cli_box_add_refuses_a_field_given_twice(
    cli_env: None, ledger_factory
) -> None:
    """AC9: ``--set label=a --set label=b`` is a duplicate-key usage error."""
    ledger = ledger_factory([BOXFIELDS], name="cli-box-add-twice", id_prefix="77")
    before = commit_count(ledger)
    refused = invoke(
        ledger,
        "box",
        "add",
        "reqbox",
        "R9",
        "--set",
        "label=a",
        "--set",
        "label=b",
    )
    assert isinstance(refused.exception, UsageError)
    assert refused.exception.exit_code == 2
    assert "appears twice in --set" in str(refused.exception)
    assert commit_count(ledger) == before
    assert dirty(ledger) == []


def test_cli_box_add_reports_defaults_and_an_empty_field_map(
    cli_env: None, ledger_factory
) -> None:
    """AC10: a schema default is reported; a field-less kind reports ``{}``."""
    ledger = ledger_factory([BOXFIELDS], name="cli-box-add-default", id_prefix="77")
    fieldbox = json.loads(
        write(ledger, "box", "add", "fieldbox", "F1", "--json").output
    )
    assert fieldbox["fields"] == {"label": "unnamed"}

    grid_ledger = ledger_factory([BASE], name="cli-box-add-nofields", id_prefix="77")
    grid = json.loads(
        write(grid_ledger, "box", "add", "grid96", "G1", "--json").output
    )
    assert grid["fields"] == {}
    assert grid["slots"] == 96


def test_cli_box_add_placement_and_slot_short_flag_still_work(
    cli_env: None, ledger_factory
) -> None:
    """AC11: ``--in``/``-s`` keep their meaning when ``--set`` is also given."""
    ledger = ledger_factory(
        [BASE, BOXFIELDS], name="cli-box-add-placement", id_prefix="77"
    )
    parent = json.loads(write(ledger, "box", "add", "rack", "RACK-1", "--json").output)
    assert parent["fields"] == {}
    child = json.loads(
        write(
            ledger,
            "box",
            "add",
            "reqbox",
            "R11",
            "--in",
            "RACK-1",
            "-s",
            "top",
            "--set",
            "label=a",
            "--json",
        ).output
    )
    assert child["parent_box_id"] == parent["id"]
    assert child["parent_slot"] == "top"
    assert child["fields"] == {"label": "a"}


def test_box_add_python_api_takes_strings_or_a_mapping(
    cli_env: None, ledger_factory
) -> None:
    """AC12: ``ledger.boxes.add(..., values=...)`` accepts both spellings."""
    ledger = ledger_factory([BOXFIELDS], name="box-add-api", id_prefix="77")
    before = commit_count(ledger)
    mapped = ledger.boxes.add("reqbox", "R10", values={"label": "a"})
    assert mapped["fields"] == {"label": "a"}
    listed = ledger.boxes.add("reqbox", "R11", values=["label=b", "count=2"])
    assert listed["fields"] == {"label": "b", "count": 2}
    assert commit_count(ledger) == before + 2
    assert ledger.driver.query(
        "SELECT `name`, `label`, `count` FROM `box` b "
        "JOIN `box_reqbox` k ON k.`id` = b.`id` ORDER BY b.`name`"
    ) == [
        {"name": "R10", "label": "a", "count": None},
        {"name": "R11", "label": "b", "count": 2},
    ]


# ---------------------------------------------------------------------------
# Edit: box kind fields, rename-only names, clear-empty (Tetramark-p1j)
# ---------------------------------------------------------------------------


def test_cli_box_edit_notes_kind_fields_and_empty_values(
    cli_env: None, ledger_factory
) -> None:
    """AC1/AC2/AC5: box edit reports the record, coerces fields and clears empties."""
    ledger = ledger_factory([BOXFIELDS], name="cli-box-edit", id_prefix="77")
    added = json.loads(
        write(ledger, "box", "add", "fieldbox", "BOX-1", "--json").output
    )

    edited = json.loads(
        write(
            ledger,
            "box",
            "edit",
            "BOX-1",
            "--set",
            "notes=first",
            "--set",
            "code=abc",
            "--set",
            "count=7",
            "--json",
        ).output
    )
    assert edited["object"] == "box"
    assert set(edited) == {
        "id",
        "kind",
        "name",
        "parent_box_id",
        "parent_slot",
        "created_at",
        "notes",
        "code",
        "count",
        "label",
        "object",
    }
    assert edited["id"] == added["id"]
    assert edited["kind"] == "fieldbox"
    assert edited["name"] == "BOX-1"
    assert edited["notes"] == "first"
    assert edited["code"] == "abc"
    assert edited["count"] == 7
    assert edited["label"] == "unnamed"

    human = write(ledger, "box", "edit", "BOX-1", "-s", "count=8")
    assert human.output.strip() == "Edited box BOX-1 (fieldbox)"

    cleared = json.loads(
        write(
            ledger,
            "box",
            "edit",
            "BOX-1",
            "--set",
            "notes=",
            "--set",
            "code=",
            "--set",
            "count=",
            "--json",
        ).output
    )
    assert cleared["notes"] is None
    assert cleared["code"] is None
    assert cleared["count"] is None

    # Invalid values are usage errors (exit 2) and write nothing.
    before = commit_count(ledger)
    for args, message in (
        (("--set", "code=ABC"), "pattern"),
        (("--set", "code=" + "a" * 33), "32"),
        (("--set", "count=not-an-int"), "integer"),
        (("--set", "label="), "required and cannot be cleared"),
    ):
        refused = invoke(ledger, "box", "edit", "BOX-1", *args)
        assert isinstance(refused.exception, UsageError)
        assert refused.exception.exit_code == 2
        assert message in str(refused.exception)
    assert commit_count(ledger) == before
    assert dirty(ledger) == []


def test_cli_edit_name_is_rename_only_and_rename_still_works(
    cli_env: None, ledger_factory
) -> None:
    """AC3/AC8: ``name`` is rename-only; add ``--name`` and rename still work."""
    ledger = ledger_factory([EXTRAS], name="cli-name-guard", id_prefix="77")
    # A kind with its own name field (item) and one without it (local_entity):
    # ``entity add --name`` fills the core name for both.
    item = json.loads(
        write(ledger, "entity", "add", "item", "--name", "alpha", "--json").output
    )
    local = json.loads(
        write(
            ledger, "entity", "add", "local_entity", "--name", "beta", "--json"
        ).output
    )
    assert item["name"] == "alpha"
    assert local["name"] == "beta"
    write(ledger, "box", "add", "grid96", "BOX")
    before = commit_count(ledger)

    for ref in (item["id"], local["id"]):
        refused = invoke(ledger, "entity", "edit", ref, "--set", "name=gamma")
        assert isinstance(refused.exception, UsageError)
        assert refused.exception.exit_code == 2
        assert str(refused.exception) == (
            f"name is rename-only; use tm entity rename {ref} NEW_NAME"
        )
    # The guard fires before resolution, naming the raw ref as typed.
    refused = invoke(ledger, "entity", "edit", "MISSING", "--set", "name=gamma")
    assert isinstance(refused.exception, UsageError)
    assert str(refused.exception) == (
        "name is rename-only; use tm entity rename MISSING NEW_NAME"
    )
    refused = invoke(ledger, "box", "edit", "BOX", "--set", "name=BOX-2")
    assert isinstance(refused.exception, UsageError)
    assert refused.exception.exit_code == 2
    assert str(refused.exception) == (
        "name is rename-only; use tm box rename BOX NEW_NAME"
    )
    refused = invoke(ledger, "entity", "edit", item["id"], "--set", "id=x")
    assert isinstance(refused.exception, UsageError)
    assert str(refused.exception) == "id is structural and cannot be edited"
    assert commit_count(ledger) == before

    # Both rename verbs still work and keep the old name as an alias.
    renamed = json.loads(
        write(ledger, "entity", "rename", item["id"], "gamma", "--json").output
    )
    assert renamed["name"] == "gamma"
    assert renamed["old_name"] == "alpha"
    assert json.loads(
        read(ledger, "entity", "show", "alpha", "--json").output
    )["name"] == "gamma"
    write(ledger, "box", "rename", "BOX", "BOX-2")
    shown = json.loads(read(ledger, "box", "show", "BOX", "--json").output)
    assert shown["box"] == "BOX-2"


def test_cli_edit_structural_and_unknown_field_refusals(
    cli_env: None, ledger_factory
) -> None:
    """AC3/AC4: structural fields name the remedy; unknown fields list valid ones."""
    ledger = ledger_factory([BOXFIELDS], name="cli-edit-refusals", id_prefix="77")
    write(ledger, "box", "add", "fieldbox", "BOX-1")
    before = commit_count(ledger)

    cases = [
        (
            ("box", "edit", "BOX-1", "--set", "parent_box_id=x"),
            "parent_box_id is structural; move a box with tm box move BOX-1 "
            "--in PARENT [--slot S]",
        ),
        (
            ("box", "edit", "BOX-1", "--set", "parent_slot=top"),
            "parent_slot is structural; move a box with tm box move BOX-1 "
            "--in PARENT [--slot S]",
        ),
        (
            ("box", "edit", "BOX-1", "--set", "id=x"),
            "id is structural and cannot be edited",
        ),
        (
            ("box", "edit", "BOX-1", "--set", "kind=x"),
            "kind is structural and cannot be edited",
        ),
        (
            ("box", "edit", "BOX-1", "--set", "created_at=x"),
            "created_at is structural and cannot be edited",
        ),
        (
            ("box", "edit", "BOX-1", "--set", "nope=1"),
            "unknown field(s) 'nope' for box kind 'fieldbox'; "
            "valid fields: code, count, label, notes",
        ),
    ]
    for args, expected in cases:
        refused = invoke(ledger, *args)
        assert isinstance(refused.exception, UsageError)
        assert refused.exception.exit_code == 2
        assert str(refused.exception) == expected
    assert commit_count(ledger) == before
    assert dirty(ledger) == []


def test_cli_stock_edit_clears_values_and_required_is_refused(
    cli_env: None, ledger_factory
) -> None:
    """AC5/AC6: stock edit clears empties; a required field cannot be cleared."""
    ledger = ledger_factory([FULL], name="cli-stock-edit", id_prefix="77")
    item = json.loads(
        write(ledger, "entity", "add", "item", "--name", "alpha", "--json").output
    )
    lot = json.loads(
        write(
            ledger,
            "stock",
            "add",
            "lot",
            "--entity",
            item["id"],
            "--set",
            "supplier=acme",
            "--set",
            "received_on=2024-01-02",
            "--json",
        ).output
    )
    edited = json.loads(
        write(
            ledger,
            "stock",
            "edit",
            lot["id"],
            "--set",
            "supplier=",
            "--set",
            "received_on=",
            "--json",
        ).output
    )
    assert edited["supplier"] is None
    assert edited["received_on"] is None

    bacterial = json.loads(
        write(
            ledger,
            "stock",
            "add",
            "bacterial",
            "--entity",
            item["id"],
            "--set",
            "strain=DH5a",
            "--json",
        ).output
    )
    before = commit_count(ledger)
    refused = invoke(ledger, "stock", "edit", bacterial["id"], "--set", "strain=")
    assert isinstance(refused.exception, UsageError)
    assert refused.exception.exit_code == 2
    assert str(refused.exception) == (
        "field 'strain' is required and cannot be cleared"
    )
    assert commit_count(ledger) == before
    assert dirty(ledger) == []


def test_cli_edit_dry_run_json_writes_nothing(cli_env: None, ledger_factory) -> None:
    """AC9: --dry-run reports the planned record and SQL, and writes nothing."""
    ledger = ledger_factory([BASE], name="cli-edit-dry", id_prefix="77")
    item = json.loads(
        write(ledger, "entity", "add", "item", "--name", "alpha", "--json").output
    )
    before = commit_count(ledger)
    result = invoke(
        ledger,
        "--dry-run",
        "entity",
        "edit",
        item["id"],
        "--set",
        "category=lab",
        "--json",
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["dry_run"] is True
    assert payload["result"]["category"] == "lab"
    assert "UPDATE `entity_item`" in payload["sql"]
    assert commit_count(ledger) == before
    assert dirty(ledger) == []
    assert ledger.driver.query("SELECT `category` FROM `entity_item`") == [
        {"category": None}
    ]


def test_cli_check_is_clean_after_a_sequence_of_edits(
    cli_env: None, ledger_factory
) -> None:
    """AC10: box/entity/stock edits leave the ledger passing ``tm check``."""
    ledger = ledger_factory([FULL], name="cli-check-edits", id_prefix="77")
    item = json.loads(
        write(ledger, "entity", "add", "item", "--name", "alpha", "--json").output
    )
    write(ledger, "box", "add", "grid96", "BOX")
    stock = json.loads(
        write(ledger, "stock", "add", "lot", "--entity", item["id"], "--json").output
    )

    write(ledger, "entity", "edit", item["id"], "--set", "category=lab")
    write(ledger, "box", "edit", "BOX", "--set", "notes=box note")
    write(ledger, "stock", "edit", stock["id"], "--set", "supplier=acme")
    # An exactly empty value clears on all three verbs (AC5).
    cleared_entity = json.loads(
        write(
            ledger, "entity", "edit", item["id"], "--set", "category=", "--json"
        ).output
    )
    cleared_box = json.loads(
        write(ledger, "box", "edit", "BOX", "--set", "notes=", "--json").output
    )
    cleared_stock = json.loads(
        write(
            ledger, "stock", "edit", stock["id"], "--set", "supplier=", "--json"
        ).output
    )
    assert cleared_entity["category"] is None
    assert cleared_box["notes"] is None
    assert cleared_stock["supplier"] is None

    report = json.loads(read(ledger, "check", "--json").output)
    assert report["ok"] is True
    assert report["failed"] == 0
