"""CLI coverage for the object command groups (ticket 10)."""

from __future__ import annotations

import json
import sys

import pytest
from typer.testing import CliRunner

from tetramark.cli.app import app, main
from tetramark.core.ledger import open as open_ledger
from tetramark.errors import LedgerError, UsageError

from conftest import PRESETS

BASE = PRESETS / "base"


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

    # --force without --note is a usage error (main() exits 2).
    usage = invoke(ledger, "aliquot", "status", alq, "stored", "--force")
    assert isinstance(usage.exception, UsageError)
    assert usage.exception.exit_code == 2
    assert "--force requires --note" in str(usage.exception)

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
            "--note",
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
            "--note",
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
    assert event["note"] == "moved to the bench"
    assert event["purpose"] is None
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
    assert event["note"] == "used it all"
    assert event["purpose"] is None
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
#: its own output line and ``object`` is not printed in the human table.
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

    # One row per record field, in the record's order: no ``path`` row (it
    # prints on its own line) and no ``object`` row (human noise).
    rows = [line for line in lines if line and not line.startswith("path:")]
    assert [row.split()[0] for row in rows] == ALIQUOT_SHOW_ROWS

    # The path closes the output as its own line, in its human shape.
    assert lines[-1] == "path: Freezer-1/Shelf-2/Box:A3"
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

    # --format csv keeps the full ids and carries the path as a column.
    csv_output = read(ledger, "--format", "csv", "aliquot", "show", alq).output
    header = csv_output.splitlines()[0].split(",")
    assert header == sorted(record)
    assert "Freezer-1/Shelf-2/Box:A3" in csv_output
    assert made["box"]["id"] in csv_output


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
    write(ledger, "aliquot", "status", second, "missing", "--note", "gone")
    write(ledger, "alias", "add", "aliquot", first, "tube-A")

    before = commit_count(ledger)
    listed = json.loads(read(ledger, "aliquot", "list", "--json").output)
    assert [row["id"] for row in listed] == [first]
    assert listed[0]["aliases"] == ["tube-A"]
    # A list row is the show record, and the read writes nothing.
    shown = json.loads(read(ledger, "aliquot", "show", first, "--json").output)
    assert listed[0] == shown
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
